"""原本ファイルの配信。docs/design/security.md 5 章。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.responses import FileResponse

from .errors import NotFound

# ブラウザ内で表示（inline）してよい形式。HTML・SVG・XML は表示させない
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}


NEVER_INLINE = {"text/html", "text/xml", "application/xhtml+xml", "image/svg+xml", "text/javascript"}


def can_preview(mime: str) -> bool:
    if mime in NEVER_INLINE:
        return False
    return mime in INLINE_TYPES or mime.startswith("text/")


def file_response(path: Path, doc: sqlite3.Row, *, inline: bool) -> FileResponse:
    if not path.exists():
        raise NotFound("ファイルの実体が見つかりません。管理者に連絡してください")
    mime = doc["mime_type"]
    inline = inline and can_preview(mime)
    media = mime
    if inline and mime.startswith("text/"):
        media = "text/plain; charset=utf-8"  # HTML などとして解釈させない
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
    return FileResponse(
        path, media_type=media if inline else (mime or "application/octet-stream"),
        filename=doc["filename"], content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )
