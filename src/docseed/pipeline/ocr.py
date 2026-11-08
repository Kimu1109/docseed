"""OCR（PP-OCRv6 small / RapidOCR）と読み順の組み立て。docs/design/pipeline.md 4 章。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

log = logging.getLogger("docseed.ocr")

MAX_SIDE = 4000


@dataclass
class OcrLine:
    text: str
    confidence: float
    box: list[tuple[float, float]]   # 4 点（画像 px）
    vertical: bool = False

    @property
    def x0(self) -> float: return min(p[0] for p in self.box)
    @property
    def x1(self) -> float: return max(p[0] for p in self.box)
    @property
    def y0(self) -> float: return min(p[1] for p in self.box)
    @property
    def y1(self) -> float: return max(p[1] for p in self.box)
    @property
    def cx(self) -> float: return (self.x0 + self.x1) / 2
    @property
    def cy(self) -> float: return (self.y0 + self.y1) / 2
    @property
    def w(self) -> float: return self.x1 - self.x0
    @property
    def h(self) -> float: return self.y1 - self.y0


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: Image.Image) -> list[OcrLine]: ...


class RapidOcrEngine:
    """RapidOCR（ONNX Runtime）で PP-OCRv6 を動かす。モデルは models_dir/rapidocr に置く。"""

    def __init__(self, models_dir: Path, *, model_type: str = "small", det_side: int = 1600, threads: int = 3,
                 allow_download: bool = False):
        from rapidocr import LangCls, ModelType, OCRVersion, RapidOCR

        root = models_dir / "rapidocr"
        if not allow_download and not any(root.rglob("*_rec_*.onnx")):
            raise RuntimeError(f"OCR モデルがありません: {root}（`docseed fetch-models` を実行してください）")
        root.mkdir(parents=True, exist_ok=True)
        mtype = {"small": ModelType.SMALL, "medium": ModelType.MEDIUM}[model_type]
        self.name = f"PP-OCRv6_{model_type}"
        self._eng = RapidOCR(params={
            "Global.log_level": "warning",
            "Global.max_side_len": MAX_SIDE,
            "Global.model_root_dir": str(root),
            "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": mtype, "Det.lang_type": "japan",
            "Det.limit_side_len": det_side, "Det.limit_type": "max",
            "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": mtype, "Rec.lang_type": "japan",
            "Cls.ocr_version": OCRVersion.PPOCRV5, "Cls.model_type": ModelType.MOBILE, "Cls.lang_type": LangCls.CH,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def load_all(self) -> None:
        """検出・方向分類・認識のモデルをすべて読み込む（無ければ取得する）。

        RapidOCR は必要になったときに読み込むため、文字の無い画像では認識モデルが取得されない。
        """
        for name in ("_load_det_model", "_load_cls_model", "_load_rec_model"):
            loader = getattr(self._eng, name, None)
            if loader is None:
                raise RuntimeError(f"RapidOCR の {name} が見つかりません（バージョンの違い）")
            loader()

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        r = self._eng(np.asarray(image.convert("RGB"))[:, :, ::-1].copy())
        if r is None or r.txts is None:
            return []
        out = []
        for box, text, score in zip(r.boxes, r.txts, r.scores, strict=False):
            pts = [(float(x), float(y)) for x, y in np.asarray(box)]
            out.append(OcrLine(str(text), float(score), pts))
        return out


def prepare_image(img: Image.Image) -> Image.Image:
    """EXIF の回転を適用し、RGB にして（透明部分は白）、長辺を MAX_SIDE 以内にする。"""
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img)
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
# 読み順（XY-cut でブロックに分けてから、ブロック内を行・列に並べる）
# ---------------------------------------------------------------------------

def _gaps(intervals: list[tuple[float, float]], min_gap: float) -> list[float]:
    """区間の集まりの隙間（min_gap 以上）の中央の座標を返す。"""
    iv = sorted(intervals)
    cuts = []
    end = iv[0][1]
    for a, b in iv[1:]:
        if a - end >= min_gap:
            cuts.append((end + a) / 2)
        end = max(end, b)
    return cuts


def _split(lines: list[OcrLine], axis: str, cuts: list[float]) -> list[list[OcrLine]]:
    groups: list[list[OcrLine]] = [[] for _ in range(len(cuts) + 1)]
    for ln in lines:
        c = ln.cx if axis == "x" else ln.cy
        groups[sum(1 for k in cuts if c > k)].append(ln)
    return [g for g in groups if g]


def xy_cut(lines: list[OcrLine], vertical_page: bool, depth: int = 0) -> list[list[OcrLine]]:
    """行の集まりを、段組み・表の区画（ブロック）に分けて読み順に並べる。"""
    if len(lines) <= 1 or depth > 12:
        return [lines]
    char = float(np.median([min(ln.w, ln.h) for ln in lines])) or 1.0
    xs = _gaps([(ln.x0, ln.x1) for ln in lines], char * 1.5)
    ys = _gaps([(ln.y0, ln.y1) for ln in lines], char * 2.0)
    order = ("x", "y") if not vertical_page else ("y", "x")
    for axis in order:
        cuts = xs if axis == "x" else ys
        if not cuts:
            continue
        parts = _split(lines, axis, cuts)
        if len(parts) <= 1:
            continue
        # 細かく分かれすぎる（ほとんどが 1 行だけの区画）なら分けない（罫線の多い表など）
        if len(parts) > 3 and sum(1 for p in parts if len(p) == 1) > len(parts) * 0.7:
            continue
        if axis == "x" and vertical_page:
            parts.reverse()  # 縦書きは右の区画から
        out: list[list[OcrLine]] = []
        for p in parts:
            out.extend(xy_cut(p, vertical_page, depth + 1))
        return out
    return [lines]


def _rows(block: list[OcrLine]) -> list[list[OcrLine]]:
    rows: list[list[OcrLine]] = []
    for ln in sorted(block, key=lambda x: x.cy):
        if rows and abs(np.mean([x.cy for x in rows[-1]]) - ln.cy) <= ln.h / 2:
            rows[-1].append(ln)
        else:
            rows.append([ln])
    return [sorted(r, key=lambda x: x.cx) for r in rows]


def _cols(block: list[OcrLine]) -> list[list[OcrLine]]:
    cols: list[list[OcrLine]] = []
    for ln in sorted(block, key=lambda x: -x.cx):
        if cols and abs(cols[-1][0].cx - ln.cx) <= max(ln.w, cols[-1][0].w) / 2:
            cols[-1].append(ln)
        else:
            cols.append([ln])
    return [sorted(c, key=lambda x: x.cy) for c in cols]


def _is_ja(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF


def _join_row(parts: list[str]) -> str:
    out = parts[0]
    for p in parts[1:]:
        sep = "" if out and p and _is_ja(out[-1]) and _is_ja(p[0]) else " "
        out += sep + p
    return out


def order_lines(lines: list[OcrLine], min_score: float) -> tuple[list[OcrLine], str, bool]:
    """本文に使う行を読み順に並べ、(並べた行, 本文, 縦書きページか) を返す。"""
    use = [ln for ln in lines if ln.confidence >= min_score and ln.text.strip()]
    if not use:
        return [], "", False
    for ln in use:  # 縦長（高さが幅の 1.5 倍以上）で 2 文字以上の行は縦書き
        ln.vertical = ln.vertical or (ln.h >= ln.w * 1.5 and len(ln.text) >= 2)
    vertical_page = sum(1 for ln in use if ln.vertical) > len(use) / 2
    ordered: list[OcrLine] = []
    blocks_text: list[str] = []
    for block in xy_cut(use, vertical_page):
        groups = _cols(block) if vertical_page else _rows(block)
        lines_text = []
        for g in groups:
            ordered.extend(g)
            lines_text.append("".join(x.text for x in g) if vertical_page else _join_row([x.text for x in g]))
        blocks_text.append("\n".join(lines_text))
    return ordered, "\n\n".join(blocks_text), vertical_page
