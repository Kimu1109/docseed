"""テンプレート用の表示ヘルパー。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

TZ = ZoneInfo(os.environ.get("DOCSEED_TIMEZONE", "Asia/Tokyo"))

STATUS_LABEL = {
    "pending": "処理待ち",
    "processing": "処理中",
    "ready": "検索できます",
    "failed": "処理に失敗",
    "unsupported": "保存のみ",
}

KIND_LABEL = {
    "folder": "フォルダ", "pdf": "PDF", "image": "画像", "text": "テキスト", "doc": "文書",
    "sheet": "表計算", "slide": "スライド", "archive": "圧縮ファイル", "media": "音声・動画", "other": "ファイル",
}


def kind_of(mime: str | None, filename: str = "") -> str:
    m = mime or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if m == "application/pdf":
        return "pdf"
    if m.startswith("image/"):
        return "image"
    if m.startswith(("audio/", "video/")):
        return "media"
    if "wordprocessingml" in m or m == "application/msword" or ext in ("doc", "docx", "docm", "odt", "rtf"):
        return "doc"
    if "spreadsheetml" in m or m == "application/vnd.ms-excel" or ext in ("xls", "xlsx", "xlsm", "ods", "csv", "tsv"):
        return "sheet"
    if "presentationml" in m or m == "application/vnd.ms-powerpoint" or ext in ("ppt", "pptx", "pptm", "odp"):
        return "slide"
    if m in ("application/zip", "application/x-7z-compressed", "application/x-tar", "application/gzip",
             "application/x-rar-compressed") or ext in ("zip", "7z", "tar", "gz", "rar"):
        return "archive"
    if m.startswith("text/") or m in ("application/json", "application/xml"):
        return "text"
    return "other"


def filesize(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def taken_at(iso: str | None) -> str:
    """EXIF の撮影日時（時差つきなら画面のタイムゾーンに直す。無ければそのまま）。"""
    if not iso:
        return "—"
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if t.tzinfo is not None:
        t = t.astimezone(TZ)
    return t.strftime("%Y/%m/%d %H:%M")


def localtime(iso: str | None, fmt: str = "%Y/%m/%d %H:%M") -> str:
    if not iso:
        return "—"
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC).astimezone(TZ)
    return t.strftime(fmt)


def shortdate(iso: str | None) -> str:
    """今年なら「11/07 14:02」、それ以外は「2025/03/01」。"""
    if not iso:
        return "—"
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC).astimezone(TZ)
    if t.year == dt.datetime.now(TZ).year:
        return t.strftime("%m/%d %H:%M")
    return t.strftime("%Y/%m/%d")


WARNING_TEXT = {
    "too_many_pages": "ページ数が多いため、先頭 {processed} ページだけを処理しました（全 {pages} ページ）",
    "ocr_slow": "{page} ページ目の文字の読み取りに {seconds} 秒かかりました",
    "ocr_timeout": "{page} ページ目の文字の読み取りが時間切れになりました",
    "sheet_truncated": "シート「{sheet}」はセルが多いため、先頭 {cells} セルだけを検索の対象にしました",
    "thumbnail_failed": "サムネイル・プレビューを作れませんでした（文字の読み取りと検索には影響しません）",
}


def problem_message(raw: str | None) -> str | None:
    """blobs.error（JSON）を画面に出す文に変える。"""
    if not raw:
        return None
    try:
        return json.loads(raw).get("message") or raw
    except (ValueError, AttributeError):
        return raw


def warning_messages(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    out = []
    for w in items:
        tmpl = WARNING_TEXT.get(w.get("code"), w.get("code", ""))
        try:
            out.append(tmpl.format(**w))
        except (KeyError, IndexError):
            out.append(tmpl)
    return out


# ---------------------------------------------------------------------------
# アイコン（Material Symbols の SVG スプライト。docs/design/extensions.md 3 章）
# ---------------------------------------------------------------------------

_ICONS_SVG = Path(__file__).parent / "static" / "icons.svg"
_icons_text = _ICONS_SVG.read_text()
ICON_NAMES = frozenset(re.findall(r'<symbol id="i-([a-z0-9_]+)"', _icons_text))
# ファイルの内容が変わったら URL も変わるようにする（ブラウザのキャッシュ対策）
ICONS_URL = "/static/icons.svg?v=" + hashlib.sha256(_icons_text.encode()).hexdigest()[:10]

KIND_ICON = {
    "folder": "folder", "pdf": "picture_as_pdf", "image": "image", "text": "description", "doc": "article",
    "sheet": "table", "slide": "slideshow", "archive": "folder_zip", "media": "movie", "other": "draft",
}
STATUS_ICON = {"pending": "schedule", "processing": "progress_activity", "ready": "check_circle",
               "failed": "error", "unsupported": "block"}


def icon(name: str, label: str | None = None, cls: str = "") -> Markup:
    """<svg class="icon"><use href=".../icons.svg#i-<name>"/></svg>。label を渡すと読み上げ用の名前を付ける。"""
    if name not in ICON_NAMES:
        raise ValueError(f"アイコン「{name}」は icons.svg にありません（src/docseed/web/icons.txt に足して"
                         " scripts/build_icons.py を実行）")
    a11y = f' role="img" aria-label="{escape(label)}"' if label else ' aria-hidden="true" focusable="false"'
    extra = f" {escape(cls)}" if cls else ""
    return Markup(f'<svg class="icon{extra}"{a11y}><use href="{ICONS_URL}#i-{name}"/></svg>')
