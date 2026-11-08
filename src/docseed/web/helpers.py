"""テンプレート用の表示ヘルパー。"""

from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

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
    if "wordprocessingml" in m or m == "application/msword" or ext in ("doc", "docx", "odt", "rtf"):
        return "doc"
    if "spreadsheetml" in m or m == "application/vnd.ms-excel" or ext in ("xls", "xlsx", "ods", "csv", "tsv"):
        return "sheet"
    if "presentationml" in m or m == "application/vnd.ms-powerpoint" or ext in ("ppt", "pptx", "odp"):
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
