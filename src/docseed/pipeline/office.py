"""Office 文書（docx / xlsx / pptx）からのテキストの取り出し。docs/design/pipeline.md 3.1 / 3.4。

どれも「ページ」の本文（正規化前）のリストを返す。

- Word: 見出し（見出し 1・2 とタイトル）で区切った単位を 1 ページとする。表は 1 行をタブ区切りの 1 行にする
- Excel: シートごとに 1 ページ。1 行目はシート名、以降はセルをタブ区切りにした行（空の行は飛ばす）
- PowerPoint: スライドごとに 1 ページ。タイトル → 上から順の図形の文字 → 表 → ノート
"""

from __future__ import annotations

import datetime as dt
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 古い形式（.doc/.xls/.ppt）と、パスワード付きの OOXML
MAX_UNCOMPRESSED = 500 * 1024 * 1024             # 展開後の合計の上限（ZIP 爆弾対策, security.md 6 章）
MAX_RATIO = 200                                  # 圧縮率の上限（展開後 / 圧縮後）
MAX_ENTRIES = 20_000
MAX_SHEET_CELLS = 1_000_000                      # 1 シートで読むセル数の上限（超えた分は警告して飛ばす）
SHEET_PREFIX = "シート: "

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MIME_KIND = {DOCX: "docx", XLSX: "xlsx", PPTX: "pptx",
             # マクロ付き・テンプレートも中身は同じ形式
             "application/vnd.ms-word.document.macroenabled.12": "docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "docx",
             "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.template": "xlsx",
             "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
             "application/vnd.openxmlformats-officedocument.presentationml.template": "pptx"}


class OfficeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class OfficeText:
    pages: list[str]
    warnings: list[dict] = field(default_factory=list)


def check_container(path: Path) -> None:
    """ZIP として開けるか、展開後の大きさが妥当かを確かめる。"""
    with path.open("rb") as fh:
        head = fh.read(8)
    if head == OLE_MAGIC:
        raise OfficeError("encrypted_office",
                          "パスワード付き、または古い形式（.doc / .xls / .ppt）の Office 文書のため、"
                          "文字を取り出せません（保存・共有はできます）")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        raise OfficeError("corrupt_file", "Office 文書を読み込めませんでした（壊れている可能性があります）") from e
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos) or 1
    if len(infos) > MAX_ENTRIES or total > MAX_UNCOMPRESSED or total / packed > MAX_RATIO:
        raise OfficeError("office_too_large", "展開すると大きすぎる Office 文書のため、文字の取り出しを省略しました"
                                              "（保存・共有はできます）")


def read_office(path: Path, kind: str) -> OfficeText:
    from docx.opc.exceptions import PackageNotFoundError as DocxInvalid
    from openpyxl.utils.exceptions import InvalidFileException as XlsxInvalid
    from pptx.exc import PackageNotFoundError as PptxInvalid

    check_container(path)
    try:
        if kind == "docx":
            return OfficeText(docx_pages(path))
        if kind == "xlsx":
            return xlsx_pages(path)
        if kind == "pptx":
            return OfficeText(pptx_pages(path))
    except OfficeError:
        raise
    except (DocxInvalid, XlsxInvalid, PptxInvalid, KeyError, ValueError, zipfile.BadZipFile, TypeError,
            AttributeError) as e:
        # 必須の部品が無い・XML が壊れている など（lxml の XMLSyntaxError は ValueError の派生）
        raise OfficeError("corrupt_file", "Office 文書を読み込めませんでした（壊れている可能性があります）") from e
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(?:heading|見出し)\s*(\d+)$", re.IGNORECASE)
SPLIT_LEVEL = 2  # この階層以上の見出しでページを区切る


def heading_level(style_name: str | None) -> int | None:
    """段落スタイル名から見出しの階層を求める（タイトルは 0。見出しでなければ None）。"""
    name = (style_name or "").strip()
    if name.lower() in ("title", "表題", "タイトル"):
        return 0
    m = _HEADING.match(name)
    return int(m.group(1)) if m else None


def _row_text(cells: list[str]) -> str:
    """表の 1 行。結合セルは python-docx / python-pptx で同じ値が並ぶので、続けて同じ値は 1 つにする。"""
    out: list[str] = []
    for c in cells:
        c = " ".join(c.split())
        if out and c == out[-1] and c:
            continue
        out.append(c)
    while out and not out[-1]:
        out.pop()
    return "\t".join(out)


def docx_pages(path: Path) -> list[str]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    with path.open("rb") as fh:
        doc = docx.Document(fh)
    pages: list[list[str]] = [[]]
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            text = p.text
            try:
                level = heading_level(p.style.name if p.style is not None else None)
            except (KeyError, ValueError):
                level = None
            if level is not None and level <= SPLIT_LEVEL and any(s.strip() for s in pages[-1]):
                pages.append([])
            pages[-1].append(text)
        elif tag == "tbl":
            table = Table(el, doc)
            rows = [_row_text([c.text for c in row.cells]) for row in table.rows]
            pages[-1].append("\n".join(r for r in rows if r))
            pages[-1].append("")  # 表の後ろは段落の区切り
    return ["\n".join(lines) for lines in pages]


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def cell_text(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, dt.date | dt.time):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else format(v, ".15g")
    return " ".join(str(v).split())


def xlsx_pages(path: Path) -> OfficeText:
    import openpyxl

    # 原本は拡張子の無い名前で保存しているので、パスではなくファイルオブジェクトで渡す（openpyxl は拡張子を見る）
    fh = path.open("rb")
    try:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
    except BaseException:
        fh.close()
        raise
    out = OfficeText([])
    try:
        for ws in wb.worksheets:
            lines = [SHEET_PREFIX + ws.title]
            cells = 0
            for row in ws.iter_rows(values_only=True):
                cells += len(row)
                if cells > MAX_SHEET_CELLS:
                    out.warnings.append({"code": "sheet_truncated", "sheet": ws.title, "cells": MAX_SHEET_CELLS})
                    break
                vals = [cell_text(v) for v in row]
                while vals and not vals[-1]:
                    vals.pop()
                if vals:
                    lines.append("\t".join(vals))
            out.pages.append("\n".join(lines))
    finally:
        wb.close()
        fh.close()
    return out


def table_header(page_text: str) -> str | None:
    """Excel のページの「シート名」と見出し行（先頭 2 行）。チャンクの埋め込みで文脈として使う。"""
    if not page_text.startswith(SHEET_PREFIX):
        return None
    lines = page_text.split("\n", 2)
    return "\n".join(lines[:2])


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------

def _shape_texts(shapes) -> list[str]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def pos(s) -> tuple[int, int]:
        return (s.top or 0, s.left or 0)

    out: list[str] = []
    for s in sorted(shapes, key=pos):
        if s.shape_type == MSO_SHAPE_TYPE.GROUP:
            out.extend(_shape_texts(s.shapes))
        elif getattr(s, "has_table", False) and s.has_table:
            rows = [_row_text([c.text for c in row.cells]) for row in s.table.rows]
            out.append("\n".join(r for r in rows if r))
        elif s.has_text_frame:
            t = s.text_frame.text.replace("\v", "\n")
            if t.strip():
                out.append(t)
    return out


def pptx_pages(path: Path) -> list[str]:
    from pptx import Presentation

    with path.open("rb") as fh:
        prs = Presentation(fh)
    pages: list[str] = []
    for slide in prs.slides:
        title = slide.shapes.title
        parts: list[str] = []
        if title is not None and title.has_text_frame and title.text_frame.text.strip():
            parts.append(title.text_frame.text.replace("\v", "\n"))
        parts.extend(_shape_texts([s for s in slide.shapes if title is None or s.shape_id != title.shape_id]))
        if slide.has_notes_slide:
            tf = slide.notes_slide.notes_text_frame
            notes = tf.text.replace("\v", "\n").strip() if tf is not None else ""
            if notes:
                parts.append("ノート:\n" + notes)
        pages.append("\n\n".join(parts))
    return pages
