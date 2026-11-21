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

from ..i18n import N_, _
from ..urls import base

TZ = ZoneInfo(os.environ.get("DOCSEED_TIMEZONE", "Asia/Tokyo"))

STATUS_LABEL = {
    "pending": N_("処理待ち"),
    "processing": N_("処理中"),
    "ready": N_("検索できます"),
    "failed": N_("処理に失敗"),
    "unsupported": N_("保存のみ"),
}

KIND_LABEL = {
    "folder": N_("フォルダ"), "pdf": "PDF", "image": N_("画像"), "text": N_("テキスト"), "doc": N_("文書"),
    "sheet": N_("表計算"), "slide": N_("スライド"), "archive": N_("圧縮ファイル"), "media": N_("音声・動画"),
    "other": N_("ファイル"), "webclip": N_("Web クリップ"),
}


def kind_of(mime: str | None, filename: str = "", doc_kind: str | None = None) -> str:
    """一覧・画面に出す種類。doc_kind は documents.kind（Web クリップなら 'webclip'）。"""
    if doc_kind == "webclip":
        return "webclip"
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


def duration(seconds: float | None) -> str:
    """「3 日 4 時間」「5 時間 12 分」「8 分」。"""
    if seconds is None:
        return "—"
    m = int(seconds) // 60
    d, h, m = m // 1440, m // 60 % 24, m % 60
    if d:
        return _("{d} 日 {h} 時間", d=d, h=h)
    if h:
        return _("{h} 時間 {m} 分", h=h, m=m)
    return _("{m} 分", m=m)


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
    "too_many_pages": N_("ページ数が多いため、先頭 {processed} ページだけを処理しました（全 {pages} ページ）"),
    "ocr_slow": N_("{page} ページ目の文字の読み取りに {seconds} 秒かかりました"),
    "ocr_timeout": N_("{page} ページ目の文字の読み取りが時間切れになりました"),
    "sheet_truncated": N_("シート「{sheet}」はセルが多いため、先頭 {cells} セルだけを検索の対象にしました"),
    "webclip_text_short": N_("本文を取り出せませんでした（JavaScript で描くページなど）。"
                             "タイトルと説明だけで検索できます"),
    "no_speech": N_("音声から文字を取り出せませんでした（無音・音楽だけなど）"),
    "summary_timeout": N_("要約が {seconds} 秒以内に終わらなかったため、要約なしにしました"),
    "thumbnail_failed": N_("サムネイル・プレビューを作れませんでした（文字の読み取りと検索には影響しません）"),
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
            out.append(_(tmpl, **w))
        except (KeyError, IndexError):
            out.append(_(tmpl))
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
    "webclip": "link",
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
    return Markup(f'<svg class="icon{extra}"{a11y}><use href="{base()}{ICONS_URL}#i-{name}"/></svg>')
