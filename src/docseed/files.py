"""原本ファイルの配信。docs/design/security.md 5 章。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.responses import FileResponse

from .errors import NotFound

# ブラウザ内で表示（inline）してよい形式。HTML・SVG・XML は表示させない
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
# 画面の <img> で原本をそのまま表示する画像（HEIC・TIFF などはプレビュー（WebP）で表示する）
BROWSER_IMAGES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
# 画面の <video>・<audio> で再生する音声・動画。キーは保存された MIME、値は配信するときの MIME。
# ブラウザで広く再生できる形式だけを入れる（AVI・WMV・MKV などは保存と取り出しだけ）
PLAYABLE_MEDIA = {
    "video/mp4": "video/mp4", "video/x-m4v": "video/mp4", "video/webm": "video/webm",
    "video/quicktime": "video/quicktime",
    "audio/mpeg": "audio/mpeg", "audio/mp4": "audio/mp4", "audio/aac": "audio/aac", "audio/ogg": "audio/ogg",
    "audio/x-wav": "audio/wav", "audio/wav": "audio/wav", "audio/x-flac": "audio/flac", "audio/flac": "audio/flac",
}


NEVER_INLINE = {"text/html", "text/xml", "application/xhtml+xml", "image/svg+xml", "text/javascript"}


def can_preview(mime: str) -> bool:
    if mime in NEVER_INLINE:
        return False
    return mime in INLINE_TYPES or mime in PLAYABLE_MEDIA or mime.startswith("text/")


def media_kind(mime: str | None) -> str | None:
    """画面で再生する種類（"video" か "audio"）。再生できない形式は None。"""
    served = PLAYABLE_MEDIA.get(mime or "")
    if served is None:
        return None
    return "video" if served.startswith("video/") else "audio"


def image_src(doc, base: str) -> str | None:
    """画面に画像として表示する URL。base は /api/v1/documents/<id> や /s/<token> など。"""
    if doc["view_path"] and doc["mime_type"].startswith("image/"):  # 映像のプレビュー（mp4）は画像ではない
        return f"{base}/view"
    if doc["mime_type"] in BROWSER_IMAGES:
        return f"{base}/content?inline=1"
    return None


def derived_response(path: Path | None) -> FileResponse:
    """派生ファイルの配信（画像は WebP、映像のプレビューは mp4）。ほかの形式として解釈させないヘッダを付ける。"""
    if path is None or not path.exists():
        raise NotFound("サムネイルはありません")
    media = "video/mp4" if path.suffix == ".mp4" else "image/webp"
    return FileResponse(path, media_type=media, headers={
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'self'",
        # 内容は原本の SHA-256 で決まるので、同じ URL なら同じ内容（ただし権限は毎回確かめる）
        "Cache-Control": "private, max-age=86400",
    })


class WholeFileResponse(FileResponse):
    """範囲指定（Range）に応えず、いつもファイル全体を返す。

    ダウンロードの回数を数えるリンクで使う。Range に応えると、数えない取得（「bytes=1-」など）を
    何度でも繰り返せてしまう（docs/design/security.md 4.2）。
    """

    async def __call__(self, scope, receive, send) -> None:
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in (b"range", b"if-range")]
        await super().__call__(scope, receive, send)


def file_response(path: Path, doc: sqlite3.Row, *, inline: bool, allow_range: bool = True) -> FileResponse:
    """原本を返す。allow_range=False なら Range に応えない（WholeFileResponse）。"""
    if not path.exists():
        raise NotFound("ファイルの実体が見つかりません。管理者に連絡してください")
    mime = doc["mime_type"]
    inline = inline and can_preview(mime)
    media = mime
    if inline and mime.startswith("text/"):
        media = "text/plain; charset=utf-8"  # HTML などとして解釈させない
    elif inline and mime in PLAYABLE_MEDIA:
        media = PLAYABLE_MEDIA[mime]
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    if inline and mime == "application/pdf":
        # PDF はブラウザの PDF ビューアで表示する。sandbox を付けるとビューアが動かないため付けない
        headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox;"
                                              " frame-ancestors 'self'")
    if not allow_range:
        headers["Accept-Ranges"] = "none"
    cls = FileResponse if allow_range else WholeFileResponse
    return cls(
        path, media_type=media if inline else (mime or "application/octet-stream"),
        filename=doc["filename"], content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )
