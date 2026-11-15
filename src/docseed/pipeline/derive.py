"""派生ファイル（サムネイル・プレビュー）と画像の情報。docs/design/extensions.md 4.2〜4.3。

- 原本は変えない。派生ファイルは向きを直し、sRGB・8 ビットにして WebP で保存する
- 派生ファイルには EXIF を一切入れない（位置情報を含めない）
- 置き場所は <data_dir>/derived/thumbs/<sha256>.webp と derived/view/<sha256>.webp
  （blobs.thumb_path / view_path には data_dir からの相対パスを入れる）
- 映像は、ブラウザで軽く再生できる低解像度の H.264・MP4 を derived/view/<sha256>.mp4 に作る（ffmpeg）
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
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
# 映像のプレビュー（H.264・MP4）。長辺を 854px（16:9 なら 854x480）に縮める。Pi 5 では実時間の数倍かかる
VIDEO_PROXY_SIDE = 854
VIDEO_PROXY_CRF = 28
VIDEO_PROXY_TIMEOUT_S = 6 * 3600   # 1 本の上限（長い動画でも、途中で打ち切らない程度に長く）
VIDEO_POLL_S = 10                  # 変換中にこの間隔で tick（心拍・停止の確認）を呼ぶ
FFMPEG_THREADS = 2
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


class DeriveStopped(Exception):
    """変換中に tick が例外（停止の合図など）を投げた。派生ファイルの失敗ではないので、そのまま上へ返す。"""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


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


def video_view_rel(sha: str) -> str:
    return f"derived/view/{sha}.mp4"


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

def derive(data_dir: Path, path: Path, mime: str, sha: str, *, video_proxy: bool = True,
           tick: Callable[[], None] | None = None) -> Derived:
    """派生ファイルを作る。作れなかった場合は warnings に thumbnail_failed を入れて返す（例外は投げない）。

    映像は時間がかかるので、制限時間は区切らず、変換中に tick を呼ぶ（tick が投げた例外はそのまま上へ返す）。
    """
    out = Derived()
    k = extract.kind(mime)
    if k == "media":
        if video_proxy and mime.startswith("video/"):
            try:
                _derive_video(data_dir, path, sha, out, tick or (lambda: None))
            except DeriveStopped as s:
                raise s.original from None
            except Exception as e:  # noqa: BLE001 - 映像のプレビューが作れなくても、文書の処理は止めない
                out.warnings.append({"code": "thumbnail_failed", "message": f"{type(e).__name__}: {e}"[:200]})
        return out
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


# ---------------------------------------------------------------------------
# 映像のプレビュー（ffmpeg）
# ---------------------------------------------------------------------------

# 長辺を VIDEO_PROXY_SIDE に収め（縦長も同じ）、縦横とも偶数にする（H.264 の条件）
_VIDEO_FILTER = (f"scale=w='min({VIDEO_PROXY_SIDE}\\,iw)':h='min({VIDEO_PROXY_SIDE}\\,ih)':"
                 "force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2")


def probe_video(path: Path) -> dict | None:
    """映像のトラックの情報（コーデック・幅・高さ）。映像のトラックが無ければ None。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height", "-of", "json", str(path)],
                       capture_output=True, timeout=60, check=False)
    if r.returncode != 0:
        raise RuntimeError("映像の情報を読めませんでした")
    streams = json.loads(r.stdout or b"{}").get("streams") or []
    if not streams:
        return None
    return streams[0]


def _derive_video(data_dir: Path, path: Path, sha: str, out: Derived, tick: Callable[[], None]) -> None:
    info = probe_video(path)
    if info is None:
        return  # 映像のトラックが無い（音声だけの動画）
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if info.get("codec_name") == "h264" and max(w, h) <= VIDEO_PROXY_SIDE:
        return  # 既に十分小さく、ブラウザで再生できる。原本をそのまま使う
    dest = data_dir / video_view_rel(sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{sha}.tmp{os.getpid()}.mp4")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", _VIDEO_FILTER,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_PROXY_CRF), "-pix_fmt", "yuv420p",
           "-threads", str(FFMPEG_THREADS), "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        _run_ffmpeg(cmd, tick)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    out.view_path = video_view_rel(sha)


def _run_ffmpeg(cmd: list[str], tick: Callable[[], None]) -> None:
    """ffmpeg を走らせ、終わるまで VIDEO_POLL_S ごとに tick を呼ぶ。中断・失敗のときは ffmpeg を残さない。"""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg が見つかりません（映像のプレビューを作れません）") from e
    deadline = time.monotonic() + VIDEO_PROXY_TIMEOUT_S
    try:
        while True:
            try:
                _, err = p.communicate(timeout=VIDEO_POLL_S)
                break
            except subprocess.TimeoutExpired:
                try:
                    tick()
                except Exception as e:  # noqa: BLE001 - 停止の合図は、そのまま上へ返す
                    raise DeriveStopped(e) from e
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{VIDEO_PROXY_TIMEOUT_S} 秒以内に終わりませんでした") from None
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    if p.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg が失敗しました"]
        raise RuntimeError(f"映像を変換できませんでした: {msg[0][:150]}")


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
