"""形式ごとのページの取り出し。docs/design/pipeline.md 3 章。

各ページは「テキスト」か「OCR が必要な画像」のどちらかで返す。OCR そのものは呼び出し側で行う。
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from .text import looks_garbled, normalize

MAX_PAGES = 2000
MIN_TEXT_CHARS = 20          # これ未満のテキスト層は OCR する
MAX_IMAGE_PIXELS = 100_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # 画像の展開爆弾対策（security.md 6 章）

TEXT_TYPES = {"application/json", "application/xml", "application/x-yaml"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/tiff", "image/gif", "image/bmp"}


class Unsupported(Exception):
    """この形式は抽出しない（保存・共有のみ）。code は pipeline.md 8 章のエラーコード。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Page:
    page_no: int
    source: str                    # text / text_layer / ocr
    text: str | None = None        # source が text / text_layer のとき
    image: Image.Image | None = None  # source が ocr のとき（OCR する画像）


def kind(mime: str) -> str:
    if mime == "application/pdf":
        return "pdf"
    if mime in IMAGE_TYPES:
        return "image"
    if mime == "text/html" or mime == "application/xhtml+xml":
        return "html"
    if mime.startswith("text/") or mime in TEXT_TYPES:
        return "text"
    return "unsupported"


def page_count(path: Path, mime: str) -> int:
    k = kind(mime)
    if k == "pdf":
        import pypdfium2 as pdfium
        try:
            doc = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as e:
            raise _pdf_error(e) from e
        return len(doc)
    if k == "image":
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    if k in ("text", "html"):
        return 1
    raise _unsupported(mime)


def _unsupported(mime: str) -> Unsupported:
    return Unsupported("unsupported_type",
                       f"この形式（{mime}）は文字の取り出しに対応していません（保存・共有はできます）")


def _pdf_error(e: Exception) -> Unsupported:
    msg = str(e).lower()
    if "password" in msg or "security" in msg:
        return Unsupported("encrypted_pdf", "パスワード付きの PDF のため、文字を取り出せません")
    return Unsupported("corrupt_file", "PDF を読み込めませんでした（壊れている可能性があります）")


def decode_text(raw: bytes) -> str:
    """文字コードを判定してデコードする（UTF-8 → UTF-16 → Shift_JIS → EUC-JP の順に試す）。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class _HtmlText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
             "ul", "ol", "pre", "blockquote", "header", "footer", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(src: str) -> str:
    p = _HtmlText()
    p.feed(src)
    text = html.unescape("".join(p.parts))
    return re.sub(r"[ \t]+", " ", text)


def iter_pages(path: Path, mime: str, *, dpi: int = 200, skip: set[int] | None = None,
               force_ocr: bool = False) -> Iterator[Page]:
    """ページを順に取り出す。skip のページ番号は飛ばす（途中再開用）。"""
    skip = skip or set()
    k = kind(mime)
    if k in ("text", "html"):
        if 1 in skip:
            return
        raw = path.read_bytes()
        text = decode_text(raw)
        if k == "html":
            text = html_to_text(text)
        yield Page(1, "text", text=normalize(text))
        return

    if k == "image":
        with Image.open(path) as im:
            n = min(getattr(im, "n_frames", 1), MAX_PAGES)
            for i in range(n):
                if i + 1 in skip:
                    continue
                im.seek(i)
                yield Page(i + 1, "ocr", image=im.copy())
        return

    if k == "pdf":
        import pypdfium2 as pdfium
        try:
            doc = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as e:
            raise _pdf_error(e) from e
        try:
            for i in range(min(len(doc), MAX_PAGES)):
                if i + 1 in skip:
                    continue
                page = doc[i]
                text = ""
                if not force_ocr:
                    tp = page.get_textpage()
                    text = normalize(tp.get_text_range(), join_broken_lines=True)
                    tp.close()
                if not force_ocr and len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS and not looks_garbled(text):
                    yield Page(i + 1, "text_layer", text=text)
                else:
                    image = page.render(scale=dpi / 72).to_pil()
                    yield Page(i + 1, "ocr", image=image)
                page.close()
        finally:
            doc.close()
        return

    raise _unsupported(mime)
