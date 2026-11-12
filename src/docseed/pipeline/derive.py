"""派生ファイル（サムネイル・プレビュー）と画像の情報。docs/design/extensions.md 4.2〜4.3。

- 原本は変えない。派生ファイルは向きを直し、sRGB・8 ビットにして WebP で保存する
- 派生ファイルには EXIF を一切入れない（位置情報を含めない）
- 置き場所は <data_dir>/derived/thumbs/<sha256>.webp と derived/view/<sha256>.webp
  （blobs.thumb_path / view_path には data_dir からの相対パスを入れる）
"""

from __future__ import annotations

import datetime as dt
import io
import os
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from . import extract

THUMB_SIDE = 256
THUMB_QUALITY = 80
VIEW_SIDE = 2048
VIEW_QUALITY = 85
VIEW_OVER = 4096              # これより長辺が大きい画像は、表示用のプレビューを作る
TIMEOUT_S = 60
# ブラウザで原本をそのまま表示しない（できない・しにくい）形式。プレビューを作る
VIEW_TYPES = {"image/heic", "image/heif", "image/avif", "image/tiff", "image/bmp"}

_SRGB = ImageCms.createProfile("sRGB")


@dataclass
class Derived:
    thumb_path: str | None = None
    view_path: str | None = None
    image_meta: dict | None = None
    warnings: list[dict] = field(default_factory=list)


class DeriveTimeout(Exception):
    pass


@contextmanager
def _deadline(seconds: int):
    """メインスレッドなら SIGALRM で時間を区切る（worker はメインスレッドで動く）。それ以外は区切らない。"""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return

    def on_alarm(*_):
        raise DeriveTimeout(f"{seconds} 秒以内に終わりませんでした")
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def thumb_rel(sha: str) -> str:
    return f"derived/thumbs/{sha}.webp"


def view_rel(sha: str) -> str:
    return f"derived/view/{sha}.webp"


# ---------------------------------------------------------------------------
# 画像を表示用に整える
# ---------------------------------------------------------------------------

def to_display(im: Image.Image) -> Image.Image:
    """向きを直し、ICC プロファイルがあれば sRGB に変換し、8 ビットの RGB / RGBA にする。"""
    im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile")
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16 ビットのグレー（TIFF など）は 8 ビットに縮める
        im = im.point(lambda v: v / 256).convert("L")
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" and "transparency" in im.info else "RGB")
    if icc and im.mode in ("RGB", "RGBA"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, _SRGB, outputMode=im.mode)
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # 壊れたプロファイルは無視して、そのままの色で作る
    if im.mode == "LA":
        im = im.convert("RGBA")
    elif im.mode == "L":
        im = im.convert("RGB")
    im.info.pop("icc_profile", None)
    im.info.pop("exif", None)
    return im


def _save_webp(im: Image.Image, dest: Path, side: int, quality: int) -> None:
    im = im.copy()
    im.thumbnail((side, side), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".tmp{os.getpid()}")
    # exif・icc_profile を渡さないので、WebP には EXIF も ICC も入らない（色は sRGB に変換済み）
    im.save(tmp, "WEBP", quality=quality, method=4)
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# 画像の情報（4.3）
# ---------------------------------------------------------------------------

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825


def image_meta(im: Image.Image, width: int, height: int) -> dict:
    meta: dict = {"width": width, "height": height}
    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001 - 壊れた EXIF は無いものとして扱う
        return meta
    ifd = exif.get_ifd(_EXIF_IFD) if exif else {}
    taken = ifd.get(0x9003) or exif.get(0x0132)          # DateTimeOriginal / DateTime
    offset = ifd.get(0x9011) or ifd.get(0x9010)           # OffsetTimeOriginal / OffsetTime
    if isinstance(taken, str):
        try:
            t = dt.datetime.strptime(taken.strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
            iso = t.isoformat()
            if isinstance(offset, str) and len(offset.strip()) == 6 and offset.strip()[0] in "+-":
                iso += offset.strip()
            meta["taken_at"] = iso
        except ValueError:
            pass
    model = exif.get(0x0110)                              # Model（例 iPhone 16。Make は付けない）
    if isinstance(model, str) and model.strip("\x00 "):
        meta["camera"] = model.strip("\x00 ")[:100]
    gps = exif.get_ifd(_GPS_IFD) if exif else {}
    meta["has_gps"] = bool(gps and (gps.get(2) or gps.get(4)))   # GPSLatitude / GPSLongitude
    return meta


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def derive(data_dir: Path, path: Path, mime: str, sha: str) -> Derived:
    """派生ファイルを作る。作れなかった場合は warnings に thumbnail_failed を入れて返す（例外は投げない）。"""
    out = Derived()
    k = extract.kind(mime)
    if k not in ("image", "pdf"):
        return out
    try:
        with _deadline(TIMEOUT_S):
            if k == "image":
                _derive_image(data_dir, path, mime, sha, out)
            else:
                _derive_pdf(data_dir, path, sha, out)
    except Exception as e:  # noqa: BLE001 - 派生ファイルの失敗で文書の処理を止めない
        out.warnings.append({"code": "thumbnail_failed", "message": f"{type(e).__name__}: {e}"[:200]})
    return out


def _derive_image(data_dir: Path, path: Path, mime: str, sha: str, out: Derived) -> None:
    extract.register_image_plugins()
    with Image.open(path) as src:
        src.seek(0)                                   # アニメーション・複数ページは最初の 1 枚
        im = to_display(src)
        out.image_meta = image_meta(src, im.width, im.height)
        _save_webp(im, data_dir / thumb_rel(sha), THUMB_SIDE, THUMB_QUALITY)
        out.thumb_path = thumb_rel(sha)
        if mime in VIEW_TYPES or max(im.size) > VIEW_OVER:
            _save_webp(im, data_dir / view_rel(sha), VIEW_SIDE, VIEW_QUALITY)
            out.view_path = view_rel(sha)


def _derive_pdf(data_dir: Path, path: Path, sha: str, out: Derived) -> None:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    try:
        if len(doc) == 0:
            return
        page = doc[0]
        w, h = page.get_size()
        scale = min(4.0, THUMB_SIDE * 2 / max(w, h, 1))  # 縮小の前に少し大きめに描く
        im = page.render(scale=scale).to_pil().convert("RGB")
        page.close()
    finally:
        doc.close()
    _save_webp(im, data_dir / thumb_rel(sha), THUMB_SIDE, THUMB_QUALITY)
    out.thumb_path = thumb_rel(sha)


def remove(data_dir: Path, *rel_paths: str | None) -> None:
    for rel in rel_paths:
        if rel:
            (data_dir / rel).unlink(missing_ok=True)


PAGE_SIDE = 1600       # OCR の結果を重ねて表示するページの画像の長辺
PAGE_QUALITY = 80


def page_image(path: Path, mime: str, page_no: int, max_side: int = PAGE_SIDE) -> bytes:
    """ページの画像（WebP）。OCR の行の位置（ページの幅・高さに対する割合）を重ねて表示するのに使う。

    OCR に渡した画像と同じ向き（PDF はページの /Rotate、画像は EXIF の回転を適用）にする。保存はしない
    （開いたときだけ描く。ブラウザがキャッシュする）。
    """
    import io
    k = extract.kind(mime)
    if k == "pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            if not 1 <= page_no <= len(doc):
                raise ValueError("page out of range")
            page = doc[page_no - 1]
            w, h = page.get_size()
            if page.get_rotation() in (90, 270):
                w, h = h, w
            im = page.render(scale=max_side / max(w, h, 1)).to_pil()
            page.close()
        finally:
            doc.close()
    elif k == "image":
        extract.register_image_plugins()
        with Image.open(path) as src:
            if not 1 <= page_no <= extract._frames(src, mime):
                raise ValueError("page out of range")
            src.seek(page_no - 1)
            im = ImageOps.exif_transpose(src.copy())
    else:
        raise ValueError("no page image for this type")
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=PAGE_QUALITY)
    return buf.getvalue()
