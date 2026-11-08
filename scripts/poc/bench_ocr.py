"""OCR の速度・精度・メモリを計測する（Phase 0）。

使い方の例:
    # 合成サンプルで PP-OCRv6 small / medium と PP-OCRv5 mobile を比較
    python bench_ocr.py samples/synthetic

    # 実文書（画像・PDF）で、DPI と検出サイズを振って比較
    python bench_ocr.py samples/real --engines rapidocr-v6-small --dpi 150,200,300 --det-side 960,1600

    # 30 分の連続処理（温度とスロットリングの確認）
    python bench_ocr.py samples/real --engines rapidocr-v6-small --duration 1800

入力:
    画像（png, jpg, jpeg, webp, tif, tiff, bmp）と PDF。ディレクトリを渡すと再帰的に探す。
    正解テキストがあれば文字誤り率（CER）を計算する:
      画像  foo.png  → foo.gt.txt
      PDF   foo.pdf  → foo.p1.gt.txt, foo.p2.gt.txt, …（ページごと）
                     または foo.p1-2.gt.txt（複数ページ分をまとめた正解。ページを順に連結して比較）
      正解テキストの「!」または「#」で始まる行はコメントとして無視する

出力:
    results/<日時>-ocr.jsonl  … ページごとの記録と設定ごとの集計
    標準出力                   … 集計の Markdown 表
    --save-text DIR を付けると認識結果のテキストも保存する
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
MAX_SIDE = 4000  # 設計書 pipeline.md 4.2

ENGINES = [
    "rapidocr-v6-small",   # 採用（設計書 1.3: PP-OCRv6 small）
    "rapidocr-v6-medium",  # 精度重視の再処理用（重い）
    "rapidocr-v5-mobile",  # 参考: 前世代の PP-OCRv5 mobile
    "rapidocr-v5-server",  # 参考: PP-OCRv5 server
    "onnxocr-v5",          # 参考: PP-OCRv5 mobile を別ラッパー（OnnxOCR）で
    "tesseract",           # 参考: 従来型（要 apt install tesseract-ocr tesseract-ocr-jpn）
]
# PP-OCRv6 tiny は日本語に対応していないため対象外
DEFAULT_ENGINES = "rapidocr-v6-small,rapidocr-v6-medium,rapidocr-v5-mobile"


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------

@dataclass
class PageInput:
    file: Path
    page: int          # 1 始まり
    is_pdf: bool

    @property
    def gt_path(self) -> Path:
        stem = self.file.with_suffix("")
        return Path(f"{stem}.p{self.page}.gt.txt") if self.is_pdf else Path(f"{stem}.gt.txt")


def collect_inputs(paths: list[Path], max_pages: int | None) -> list[PageInput]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(x for x in p.rglob("*") if x.suffix.lower() in IMAGE_EXTS | {".pdf"})
        elif p.exists():
            files.append(p)
        else:
            raise SystemExit(f"見つかりません: {p}")
    pages: list[PageInput] = []
    for f in files:
        if f.suffix.lower() == ".pdf":
            import pypdfium2 as pdfium
            n = len(pdfium.PdfDocument(str(f)))
            pages += [PageInput(f, i + 1, True) for i in range(n)]
        else:
            pages.append(PageInput(f, 1, False))
    if max_pages:
        pages = pages[:max_pages]
    if not pages:
        raise SystemExit("入力ファイルがありません")
    return pages


def load_page(p: PageInput, dpi: int) -> Image.Image:
    if p.is_pdf:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p.file))
        img = doc[p.page - 1].render(scale=dpi / 72).to_pil()
    else:
        img = ImageOps.exif_transpose(Image.open(p.file))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

@dataclass
class Line:
    box: np.ndarray    # (4, 2)
    text: str
    score: float


OcrFn = Callable[[Image.Image], list[Line]]


def make_engine(name: str, det_side: int, threads: int, models_dir: Path | None, tess_lang: str) -> OcrFn:
    if name.startswith("rapidocr-"):
        from rapidocr import LangCls, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        version, mtype = {
            "rapidocr-v5-mobile": (OCRVersion.PPOCRV5, ModelType.MOBILE),
            "rapidocr-v5-server": (OCRVersion.PPOCRV5, ModelType.SERVER),
            "rapidocr-v6-small": (OCRVersion.PPOCRV6, ModelType.SMALL),
            "rapidocr-v6-medium": (OCRVersion.PPOCRV6, ModelType.MEDIUM),
        }[name]
        v6 = version == OCRVersion.PPOCRV6
        params: dict[str, Any] = {
            "Global.log_level": "warning",
            "Global.max_side_len": MAX_SIDE,
            "Det.ocr_version": version,
            "Det.model_type": mtype,
            "Det.lang_type": "japan" if v6 else LangDet.CH,
            "Det.limit_side_len": det_side,
            "Det.limit_type": "max",
            "Rec.ocr_version": version,
            "Rec.model_type": mtype,
            "Rec.lang_type": LangRec.CH,  # PP-OCRv5 の ch モデルが日本語を含む
            "Cls.ocr_version": OCRVersion.PPOCRV5,
            "Cls.model_type": ModelType.MOBILE,
            "Cls.lang_type": LangCls.CH,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        if v6:
            params["Rec.lang_type"] = "japan"
        if models_dir:
            params["Global.model_root_dir"] = str(models_dir / "rapidocr")
        eng = RapidOCR(params=params)

        def run(img: Image.Image) -> list[Line]:
            r = eng(np.asarray(img)[:, :, ::-1].copy())
            if r is None or r.txts is None:
                return []
            return [Line(np.asarray(b, dtype=float), t, float(s))
                    for b, t, s in zip(r.boxes, r.txts, r.scores)]
        return run

    if name == "onnxocr-v5":
        import onnxruntime as ort

        # OnnxOCR はスレッド数の指定を持たないため、SessionOptions を差し替える
        orig = ort.SessionOptions

        def session_options() -> Any:
            so = orig()
            so.intra_op_num_threads = threads
            so.inter_op_num_threads = 1
            return so
        ort.SessionOptions = session_options  # type: ignore[assignment]

        import logging
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr
        logger = logging.getLogger("onnxocr")
        logger.setLevel(logging.WARNING)
        eng = ONNXPaddleOcr(use_angle_cls=True, use_gpu=False, det_limit_side_len=det_side,
                            det_limit_type="max", logger=logger)

        def run(img: Image.Image) -> list[Line]:
            res = eng.ocr(np.asarray(img)[:, :, ::-1].copy())
            lines = res[0] if res else []
            return [Line(np.asarray(box, dtype=float), t, float(s)) for box, (t, s) in lines]
        return run

    if name == "tesseract":
        if not shutil.which("tesseract"):
            raise SystemExit("tesseract が見つかりません（sudo apt install tesseract-ocr tesseract-ocr-jpn）")

        def run(img: Image.Image) -> list[Line]:
            import io
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", tess_lang, "--psm", "3"],
                input=buf.getvalue(), capture_output=True,
                env={"OMP_THREAD_LIMIT": str(threads), "PATH": "/usr/bin:/bin"},
            ).stdout.decode("utf-8", "replace")
            return [Line(np.zeros((4, 2)), l, 1.0) for l in out.splitlines() if l.strip()]
        return run

    raise SystemExit(f"不明なエンジン: {name}（{', '.join(ENGINES)}）")


# ---------------------------------------------------------------------------
# 読み順（設計書 pipeline.md 4.4 の試作）
# ---------------------------------------------------------------------------

def reading_order(lines: list[Line], min_score: float) -> tuple[str, bool]:
    """行を読み順に並べてページ本文を作る。戻り値は (本文, 縦書きページか)。"""
    groups, vertical_page = order_groups(lines, min_score)
    sep = "" if vertical_page else " "
    return "\n".join(sep.join(x.text for x in g) for g in groups), vertical_page


def order_groups(lines: list[Line], min_score: float) -> tuple[list[list[Line]], bool]:
    """行を読み順の「行（縦書きなら列）」のまとまりに並べる。戻り値は (まとまりのリスト, 縦書きページか)。"""
    ls = [l for l in lines if l.score >= min_score and l.text.strip()]
    if not ls:
        return [], False
    if not np.any([l.box.any() for l in ls]):  # 座標なし（tesseract）
        return [[l] for l in ls], False

    def geom(l: Line) -> tuple[float, float, float, float]:
        xs, ys = l.box[:, 0], l.box[:, 1]
        return xs.mean(), ys.mean(), xs.max() - xs.min(), ys.max() - ys.min()

    vert = [geom(l)[3] >= geom(l)[2] * 1.5 and len(l.text) >= 2 for l in ls]
    vertical_page = bool(sum(vert) > len(ls) / 2)

    if vertical_page:
        # 列ごと（右 → 左）、列内は上 → 下
        items = sorted(ls, key=lambda l: -geom(l)[0])
        cols: list[list[Line]] = []
        for l in items:
            cx, _, w, _ = geom(l)
            if cols and abs(geom(cols[-1][0])[0] - cx) <= max(w, geom(cols[-1][0])[2]) / 2:
                cols[-1].append(l)
            else:
                cols.append([l])
        return [sorted(c, key=lambda l: geom(l)[1]) for c in cols], True
    else:
        # 行ごと（上 → 下）、行内は左 → 右
        items = sorted(ls, key=lambda l: geom(l)[1])
        rows: list[list[Line]] = []
        for l in items:
            _, cy, _, h = geom(l)
            if rows and abs(np.mean([geom(x)[1] for x in rows[-1]]) - cy) <= h / 2:
                rows[-1].append(l)
            else:
                rows.append([l])
        return [sorted(r, key=lambda l: geom(l)[0]) for r in rows], False


def read_gt(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(l for l in lines if not l.lstrip().startswith(("!", "#")))


def range_gt_files(pdf: Path) -> list[tuple[int, int, Path]]:
    """foo.p1-2.gt.txt 形式の正解ファイルを (開始, 終了, パス) で返す。"""
    out = []
    for f in pdf.parent.glob(f"{glob_escape(pdf.stem)}.p*-*.gt.txt"):
        m = re.fullmatch(re.escape(pdf.stem) + r"\.p(\d+)-(\d+)\.gt\.txt", f.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), f))
    return sorted(out)


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def normalize_for_cer(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if not ch.isspace())


def char_overlap(pred: str, gt: str) -> tuple[float, float]:
    """読み順を無視した文字単位の (再現率, 適合率)。

    正解と認識結果の文字の多重集合の共通部分で数えるので、段組みや表で読み順が
    正解と違っても、文字そのものを正しく読めていれば下がらない。
    再現率 = 正解の文字のうち認識できた割合、適合率 = 認識した文字のうち正しかった割合。
    """
    from collections import Counter
    p, g = Counter(normalize_for_cer(pred)), Counter(normalize_for_cer(gt))
    common = sum((p & g).values())
    return (common / max(1, sum(g.values())), common / max(1, sum(p.values())))


def cer(pred: str, gt: str) -> float:
    from rapidfuzz.distance import Levenshtein
    p, g = normalize_for_cer(pred), normalize_for_cer(gt)
    if not g:
        return 0.0 if not p else 1.0
    return Levenshtein.distance(p, g) / len(g)


# ---------------------------------------------------------------------------
# 1 設定分の計測（子プロセスで実行）
# ---------------------------------------------------------------------------

def run_single(args: argparse.Namespace) -> None:
    pages = collect_inputs(args.inputs, args.max_pages)
    cfg = {"engine": args.engine, "dpi": args.dpi, "det_side": args.det_side, "threads": args.threads}
    common.wait_for_cool(args.cool_to)
    throttled_before = common.throttled()

    with common.Timer() as t_load:
        ocr = make_engine(args.engine, args.det_side, args.threads, args.models_dir, args.tess_lang)
    rss_after_load = common.peak_rss_mb()

    # ウォームアップ（初回の推論はグラフ最適化などで遅いため除外）
    for p in pages[: args.warmup]:
        ocr(load_page(p, args.dpi))

    page_secs: list[float] = []
    cers: list[float] = []
    recalls: list[float] = []
    temps: list[float] = []
    page_texts: dict[tuple[Path, int], str] = {}
    started = time.monotonic()
    rounds = 0
    while True:
        for p in pages:
            with common.Timer() as t_render:
                img = load_page(p, args.dpi)
            with common.Timer() as t_ocr:
                lines = ocr(img)
            text, vertical_page = reading_order(lines, args.min_score)
            rec: dict[str, Any] = {
                "type": "page", **cfg, "round": rounds,
                "file": str(p.file), "page": p.page,
                "width": img.size[0], "height": img.size[1],
                "render_s": t_render.elapsed if p.is_pdf else 0.0,
                "ocr_s": t_ocr.elapsed,
                "lines": len(lines), "chars": len(normalize_for_cer(text)),
                "mean_score": float(np.mean([l.score for l in lines])) if lines else None,
                "vertical": vertical_page,
                "temp_c": common.cpu_temp_c(), "freq_mhz": common.cpu_freq_mhz(),
            }
            if p.gt_path.exists():
                gt_text = read_gt(p.gt_path)
                rec["cer"] = cer(text, gt_text)
                rec["char_recall"], rec["char_precision"] = char_overlap(text, gt_text)
                cers.append(rec["cer"])
                recalls.append(rec["char_recall"])
            if args.save_text and rounds == 0:
                out = args.save_text / args.engine / f"dpi{args.dpi}_det{args.det_side}"
                out.mkdir(parents=True, exist_ok=True)
                suffix = f".p{p.page}" if p.is_pdf else ""
                (out / f"{p.file.stem}{suffix}.txt").write_text(text + "\n", encoding="utf-8")
            if p.is_pdf and rounds == 0:
                page_texts[(p.file, p.page)] = text
            page_secs.append(t_ocr.elapsed)
            if rec["temp_c"] is not None:
                temps.append(rec["temp_c"])
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            print(f"  {args.engine} {p.file.name} p{p.page}: {t_ocr.elapsed:.2f}s"
                  + (f" CER={rec['cer']:.3f} 文字再現率={rec['char_recall']:.3f}" if "cer" in rec else ""),
                  file=sys.stderr)
        rounds += 1
        if not args.duration or time.monotonic() - started >= args.duration:
            break

    # 複数ページをまとめた正解（foo.p1-2.gt.txt）
    for pdf in sorted({p.file for p in pages if p.is_pdf}):
        for a, b, gt in range_gt_files(pdf):
            texts = [page_texts.get((pdf, n)) for n in range(a, b + 1)]
            if any(t is None for t in texts):
                continue  # --max-pages などで対象ページを処理していない
            joined, gt_text = "\n".join(texts), read_gt(gt)  # type: ignore[arg-type]
            value = cer(joined, gt_text)
            rc, pr = char_overlap(joined, gt_text)
            cers.append(value)
            recalls.append(rc)
            print(json.dumps({"type": "range_cer", **cfg, "file": str(pdf), "pages": f"{a}-{b}",
                              "cer": value, "char_recall": rc, "char_precision": pr},
                             ensure_ascii=False), flush=True)
            print(f"  {args.engine} {pdf.name} p{a}-{b}: CER={value:.3f} 文字再現率={rc:.3f} 適合率={pr:.3f}",
                  file=sys.stderr)

    s = common.summarize(page_secs)
    summary = {
        "type": "summary", **cfg,
        "pages": len(pages), "rounds": rounds,
        "load_s": t_load.elapsed,
        "sec_per_page_mean": s.get("mean"), "sec_per_page_p50": s.get("p50"),
        "sec_per_page_p95": s.get("p95"), "sec_per_page_max": s.get("max"),
        "cer_mean": float(np.mean(cers)) if cers else None,
        "char_recall_mean": float(np.mean(recalls)) if recalls else None,
        "cer_pages": len(cers),
        "rss_after_load_mb": rss_after_load,
        "peak_rss_mb": common.peak_rss_mb(),
        "temp_max_c": max(temps) if temps else None,
        "throttled_before": throttled_before, "throttled_after": common.throttled(),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# 全設定の実行（親プロセス）
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="画像・PDF・ディレクトリ")
    ap.add_argument("--engines", default=DEFAULT_ENGINES, help=f"カンマ区切り（{', '.join(ENGINES)}）")
    ap.add_argument("--dpi", default="200", help="PDF の画像化 DPI（カンマ区切りで複数可）")
    ap.add_argument("--det-side", default="1600", help="検出の入力サイズ上限（長辺 px, カンマ区切りで複数可）")
    ap.add_argument("--threads", type=int, default=3, help="ONNX Runtime の intra-op スレッド数")
    ap.add_argument("--min-score", type=float, default=0.5, help="本文に採用する行の最低信頼度")
    ap.add_argument("--warmup", type=int, default=1, help="計測前に処理するページ数")
    ap.add_argument("--duration", type=float, default=0, help="この秒数に達するまで入力を繰り返す（連続処理・発熱の確認）")
    ap.add_argument("--max-pages", type=int, help="処理するページ数の上限")
    ap.add_argument("--cool-to", type=float, default=60.0, help="各設定の開始前に CPU がこの温度（℃）以下になるまで待つ")
    ap.add_argument("--models-dir", type=Path, help="モデルの保存先（省略時はライブラリの既定）")
    ap.add_argument("--tess-lang", default="jpn", help="tesseract の言語（例: jpn, jpn_vert, jpn+eng）")
    ap.add_argument("--save-text", type=Path, help="認識結果のテキストを保存するディレクトリ")
    ap.add_argument("--out", type=Path, help="結果の保存先ディレクトリ（既定: scripts/poc/results）")
    ap.add_argument("--_single", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--engine", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._single:
        args.dpi, args.det_side = int(args.dpi), int(args.det_side)
        run_single(args)
        return

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    dpis = [int(x) for x in args.dpi.split(",")]
    sides = [int(x) for x in args.det_side.split(",")]
    has_pdf = any(p.is_pdf for p in collect_inputs(args.inputs, args.max_pages))
    if not has_pdf:
        dpis = dpis[:1]  # 画像だけなら DPI は無関係

    out_path = common.results_path("ocr", args.out)
    common.write_jsonl(out_path, {"type": "system", **common.system_info(), "argv": sys.argv[1:]})
    summaries = []
    for engine, dpi, side in itertools.product(engines, dpis, sides):
        if engine == "tesseract" and side != sides[0]:
            continue  # tesseract に検出サイズの設定は無い
        print(f"▶ {engine} dpi={dpi} det_side={side}", file=sys.stderr)
        argv = [__file__, "--_single", "--engine", engine, "--dpi", str(dpi), "--det-side", str(side),
                "--threads", str(args.threads), "--min-score", str(args.min_score),
                "--warmup", str(args.warmup), "--duration", str(args.duration),
                "--cool-to", str(args.cool_to), "--tess-lang", args.tess_lang]
        if args.max_pages:
            argv += ["--max-pages", str(args.max_pages)]
        if args.models_dir:
            argv += ["--models-dir", str(args.models_dir)]
        if args.save_text:
            argv += ["--save-text", str(args.save_text)]
        argv += [str(p) for p in args.inputs]
        for rec in common.run_isolated(argv):
            common.write_jsonl(out_path, rec)
            if rec.get("type") == "summary":
                summaries.append(rec)

    print()
    print(common.md_table(summaries, [
        ("engine", "エンジン"), ("dpi", "DPI"), ("det_side", "検出サイズ"),
        ("load_s", "読込(s)"), ("sec_per_page_mean", "平均(s/頁)"), ("sec_per_page_p95", "p95(s/頁)"),
        ("cer_mean", "CER"), ("char_recall_mean", "文字再現率"), ("peak_rss_mb", "ピークRSS(MB)"), ("temp_max_c", "最高温度(℃)"),
        ("throttled_after", "throttled"),
    ]))
    print(f"\n結果: {out_path}")


if __name__ == "__main__":
    main()
