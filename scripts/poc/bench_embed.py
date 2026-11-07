"""埋め込みモデルの速度・メモリ・量子化による劣化を計測する（Phase 0）。

使い方の例:
    # 既定: ruri-v3-30m / 70m / 130m を PyTorch と ONNX(int8) で比較
    python bench_embed.py

    # 実データのテキスト（OCR 結果など）をチャンクにして使う
    python bench_embed.py --corpus out/text --n 512

    # 採用予定の 70m だけ、バックエンドを 3 種類比較
    python bench_embed.py --models cl-nagoya/ruri-v3-70m --backends torch,onnx,onnx-int8

計測項目:
    - モデルの読み込み時間、ピークメモリ（RSS）
    - 文書チャンクの埋め込み速度（chunks/s）
    - 検索クエリ 1 件の埋め込み時間（p50 / p95）
    - torch(fp32) の結果とのコサイン類似度（ONNX・int8 量子化での劣化の確認）

出力: results/<日時>-embed.jsonl と標準出力の Markdown 表
"""

from __future__ import annotations

import argparse
import itertools
import json
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_MODELS = "cl-nagoya/ruri-v3-30m,cl-nagoya/ruri-v3-70m,cl-nagoya/ruri-v3-130m"
BACKENDS = ["torch", "onnx", "onnx-int8"]
DOC_PREFIX = "検索文書: "
QUERY_PREFIX = "検索クエリ: "

QUERIES = [
    "振込先の口座番号", "契約の更新条件", "健康診断の結果", "見積書の有効期限",
    "秘密保持の条項", "会議の議事録", "領収書の再発行", "問い合わせ先の電話番号",
    "マイナンバーカードの手続き", "Raspberry Pi の設定方法",
]


# ---------------------------------------------------------------------------
# コーパス
# ---------------------------------------------------------------------------

def chunk_text(text: str, target: int = 600, max_len: int = 800) -> list[str]:
    """設計書 pipeline.md 5 章の簡易版（文単位で詰める。重なりは省略）。"""
    import re
    sents = [s for s in re.split(r"(?<=[。！？!?\n])", text) if s.strip()]
    chunks, cur = [], ""
    for s in sents:
        while len(s) > max_len:
            chunks.append(s[:max_len])
            s = s[max_len:]
        if len(cur) + len(s) > target and cur:
            chunks.append(cur)
            cur = ""
        cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks


def build_corpus(corpus_dir: Path | None, n: int, seed: int) -> list[str]:
    rng = np.random.default_rng(seed)
    chunks: list[str] = []
    if corpus_dir:
        for f in sorted(corpus_dir.rglob("*.txt")):
            chunks += chunk_text(f.read_text(encoding="utf-8", errors="replace"))
        if not chunks:
            raise SystemExit(f"{corpus_dir} に .txt がありません")
    else:
        from make_samples import SENTENCES
        # 文を並べ替えて 300〜800 文字の疑似チャンクを作る（長さのばらつきも再現）
        for _ in range(n):
            target = int(rng.integers(300, 800))
            s = ""
            while len(s) < target:
                s += SENTENCES[int(rng.integers(len(SENTENCES)))]
            chunks.append(s[:target])
    while len(chunks) < n:
        chunks += chunks
    return chunks[:n]


# ---------------------------------------------------------------------------
# モデルの読み込み
# ---------------------------------------------------------------------------

def quant_config_name() -> str:
    return "arm64" if platform.machine() in ("aarch64", "arm64") else "avx2"


def load_model(name: str, backend: str, threads: int, models_dir: Path) -> Any:
    from sentence_transformers import SentenceTransformer

    if backend == "torch":
        import torch
        torch.set_num_threads(threads)
        return SentenceTransformer(name, device="cpu", cache_folder=str(models_dir / "hf"))

    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    kwargs: dict[str, Any] = {"provider": "CPUExecutionProvider", "session_options": so}

    # ONNX への変換結果をキャッシュする（初回のみ時間がかかる）
    export_dir = models_dir / "onnx" / name.replace("/", "__")
    if not (export_dir / "onnx" / "model.onnx").exists():
        print(f"  ONNX に変換しています: {name} → {export_dir}", file=sys.stderr)
        m = SentenceTransformer(name, device="cpu", backend="onnx",
                                cache_folder=str(models_dir / "hf"))
        m.save_pretrained(str(export_dir))
        del m

    if backend == "onnx-int8":
        cfg = quant_config_name()
        qfile = export_dir / "onnx" / f"model_qint8_{cfg}.onnx"
        if not qfile.exists():
            from sentence_transformers import export_dynamic_quantized_onnx_model
            print(f"  int8 に量子化しています（{cfg}）", file=sys.stderr)
            base = SentenceTransformer(str(export_dir), device="cpu", backend="onnx")
            export_dynamic_quantized_onnx_model(base, cfg, str(export_dir))
            del base
        kwargs["file_name"] = f"onnx/{qfile.name}"
    else:
        kwargs["file_name"] = "onnx/model.onnx"

    return SentenceTransformer(str(export_dir), device="cpu", backend="onnx", model_kwargs=kwargs)


# ---------------------------------------------------------------------------
# 1 設定分の計測（子プロセス）
# ---------------------------------------------------------------------------

def run_single(args: argparse.Namespace) -> None:
    cfg = {"model": args.model, "backend": args.backend, "threads": args.threads,
           "batch_size": args.batch_size}
    corpus = build_corpus(args.corpus, args.n, args.seed)
    docs = [DOC_PREFIX + c for c in corpus]
    common.wait_for_cool(args.cool_to)

    with common.Timer() as t_load:
        model = load_model(args.model, args.backend, args.threads, args.models_dir)
    model.max_seq_length = args.max_seq_length
    rss_after_load = common.peak_rss_mb()

    # ウォームアップ
    model.encode(docs[: args.batch_size], batch_size=args.batch_size, normalize_embeddings=True)
    model.encode([QUERY_PREFIX + QUERIES[0]], normalize_embeddings=True)

    # 文書の埋め込み（長さ順に並べてパディングを減らす。sentence-transformers も内部で並べ替える）
    temp_before = common.cpu_temp_c()
    with common.Timer() as t_docs:
        emb = model.encode(docs, batch_size=args.batch_size, normalize_embeddings=True,
                           convert_to_numpy=True, show_progress_bar=False)
    temp_after = common.cpu_temp_c()

    # クエリ 1 件ずつ（検索時の応答時間に相当）
    q_secs = []
    for q in itertools.islice(itertools.cycle(QUERIES), args.queries):
        with common.Timer() as t:
            model.encode([QUERY_PREFIX + q], normalize_embeddings=True)
        q_secs.append(t.elapsed)
    qs = common.summarize(q_secs)

    # 品質比較用に先頭 64 件のベクトルを保存
    if args.emb_out:
        np.save(args.emb_out, emb[:64].astype(np.float32))

    lengths = [len(c) for c in corpus]
    print(json.dumps({
        "type": "summary", **cfg,
        "dim": int(emb.shape[1]), "chunks": len(docs),
        "chunk_chars_mean": float(np.mean(lengths)),
        "load_s": t_load.elapsed,
        "docs_s": t_docs.elapsed,
        "chunks_per_s": len(docs) / t_docs.elapsed,
        "query_ms_p50": qs["p50"] * 1000, "query_ms_p95": qs["p95"] * 1000,
        "rss_after_load_mb": rss_after_load,
        "peak_rss_mb": common.peak_rss_mb(),
        "temp_before_c": temp_before, "temp_after_c": temp_after,
        "throttled": common.throttled(),
    }, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# 親プロセス
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=DEFAULT_MODELS, help="カンマ区切りのモデル名")
    ap.add_argument("--backends", default="torch,onnx-int8", help=f"カンマ区切り（{', '.join(BACKENDS)}）")
    ap.add_argument("--threads", default="3", help="スレッド数（カンマ区切りで複数可。例: 1,3,4）")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--n", type=int, default=256, help="埋め込むチャンク数")
    ap.add_argument("--queries", type=int, default=50, help="クエリ埋め込みの計測回数")
    ap.add_argument("--max-seq-length", type=int, default=1024, help="トークン数の上限（800 文字のチャンクなら十分）")
    ap.add_argument("--corpus", type=Path, help=".txt を含むディレクトリ（省略時は合成テキスト）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cool-to", type=float, default=60.0, help="各設定の開始前に CPU がこの温度（℃）以下になるまで待つ")
    ap.add_argument("--models-dir", type=Path, default=Path(__file__).resolve().parent / "models",
                    help="モデルと ONNX 変換結果の保存先")
    ap.add_argument("--out", type=Path, help="結果の保存先ディレクトリ（既定: scripts/poc/results）")
    ap.add_argument("--_single", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--model", help=argparse.SUPPRESS)
    ap.add_argument("--backend", help=argparse.SUPPRESS)
    ap.add_argument("--emb-out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._single:
        args.threads = int(args.threads)
        run_single(args)
        return

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in BACKENDS:
            raise SystemExit(f"不明なバックエンド: {b}")
    threads_list = [int(t) for t in args.threads.split(",")]
    # 劣化の比較のため torch を基準として必ず先に実行する
    if any(b != "torch" for b in backends) and "torch" not in backends:
        backends = ["torch", *backends]
    backends.sort(key=lambda b: b != "torch")

    out_path = common.results_path("embed", args.out)
    common.write_jsonl(out_path, {"type": "system", **common.system_info(), "argv": sys.argv[1:]})
    summaries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for model, threads, backend in itertools.product(models, threads_list, backends):
            print(f"▶ {model} backend={backend} threads={threads}", file=sys.stderr)
            emb_file = Path(tmp) / f"{model.replace('/', '__')}-{backend}-{threads}.npy"
            argv = [__file__, "--_single", "--model", model, "--backend", backend,
                    "--threads", str(threads), "--batch-size", str(args.batch_size),
                    "--n", str(args.n), "--queries", str(args.queries),
                    "--max-seq-length", str(args.max_seq_length), "--seed", str(args.seed),
                    "--cool-to", str(args.cool_to), "--models-dir", str(args.models_dir),
                    "--emb-out", str(emb_file)]
            if args.corpus:
                argv += ["--corpus", str(args.corpus)]
            for rec in common.run_isolated(argv):
                ref = Path(tmp) / f"{model.replace('/', '__')}-torch-{threads}.npy"
                if backend != "torch" and ref.exists() and emb_file.exists():
                    a, b = np.load(ref), np.load(emb_file)
                    sims = (a * b).sum(axis=1)  # どちらも L2 正規化済み
                    rec["cos_vs_torch_mean"] = float(sims.mean())
                    rec["cos_vs_torch_min"] = float(sims.min())
                common.write_jsonl(out_path, rec)
                summaries.append(rec)
                print(f"  {rec['chunks_per_s']:.1f} chunks/s, query p50 {rec['query_ms_p50']:.0f}ms",
                      file=sys.stderr)

    print()
    print(common.md_table(summaries, [
        ("model", "モデル"), ("backend", "実行方式"), ("threads", "スレッド"), ("dim", "次元"),
        ("load_s", "読込(s)"), ("chunks_per_s", "chunks/s"),
        ("query_ms_p50", "クエリp50(ms)"), ("query_ms_p95", "クエリp95(ms)"),
        ("peak_rss_mb", "ピークRSS(MB)"), ("cos_vs_torch_min", "torch比cos(最小)"),
        ("temp_after_c", "温度(℃)"),
    ]))
    print(f"\n結果: {out_path}")


if __name__ == "__main__":
    main()
