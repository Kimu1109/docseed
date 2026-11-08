"""本文の正規化とチャンク分割。docs/design/pipeline.md 3.3 / 5 章。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TRAILING_WS = re.compile(r"[ \t　]+\n")
_MANY_BLANK = re.compile(r"\n{3,}")
# 日本語の文字（ひらがな・カタカナ・漢字・全角記号の一部）
_JA = r"぀-ヿ㐀-䶿一-鿿豈-﫿々ー"
# 行末が句読点などで終わらず、次の行が日本語で始まる改行（PDF のテキスト層の折り返し）
_BROKEN_LINE = re.compile(rf"(?<=[{_JA}、])\n(?=[{_JA}])")


def normalize(text: str, *, join_broken_lines: bool = False) -> str:
    """NFKC 正規化、改行の統一、行末空白の除去、空行の圧縮、制御文字の除去。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    text = _TRAILING_WS.sub("\n", text)
    if join_broken_lines:
        text = _BROKEN_LINE.sub("", text)
    text = _MANY_BLANK.sub("\n\n", text)
    return text.strip()


def looks_garbled(text: str) -> bool:
    """置換文字や私用領域の文字が 10% 以上なら、文字化けしたテキスト層とみなす。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    bad = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    return bad / len(chars) >= 0.10


# ---------------------------------------------------------------------------
# チャンク分割
# ---------------------------------------------------------------------------

TARGET = 600
MAX_LEN = 800
OVERLAP = 100
SHORT_PAGE = 100

_PARA = re.compile(r"(?:[^\n]|\n(?!\s*\n))+")      # 空行を含まない連続部分 = 段落
_SENT_END = re.compile(r"[。！？!?]|\.(?=\s)|\n")   # 文末記号・「. 」・改行


@dataclass
class Chunk:
    page_no: int
    seq: int
    char_start: int
    char_end: int
    text: str


@dataclass
class _Sent:
    start: int
    end: int
    text: str


def _sentences(page_text: str) -> list[_Sent]:
    """文単位に分ける（ページ本文内の位置つき）。段落の終わりには空の文を入れる。MAX_LEN を超える文は切る。"""
    out: list[_Sent] = []

    def add(seg: str, offset: int) -> None:
        lead = len(seg) - len(seg.lstrip())
        body = seg.strip()
        for k in range(0, len(body), MAX_LEN):
            piece = body[k:k + MAX_LEN]
            st = offset + lead + k
            out.append(_Sent(st, st + len(piece), piece))

    for pm in _PARA.finditer(page_text):
        para, base = pm.group(), pm.start()
        last = 0
        for m in _SENT_END.finditer(para):
            add(para[last:m.end()], base + last)
            last = m.end()
        add(para[last:], base + last)
        out.append(_Sent(pm.end(), pm.end(), ""))
    return out


def _is_heading(s: _Sent) -> bool:
    t = s.text
    return 0 < len(t) <= 40 and not t.endswith(("。", "、", ".", ",", "!", "?", "！", "？"))


def chunk_pages(pages: list[tuple[int, str]]) -> list[Chunk]:
    """ページ本文のリスト [(page_no, text)] をチャンクに分ける。

    - 文を順に詰め、TARGET 文字を超えたら（段落の切れ目を優先して）確定する。MAX_LEN 文字は超えない
    - 次のチャンクの先頭には、直前のチャンクの末尾から OVERLAP 文字以内に収まる文を重ねる
    - 見出しらしい行でチャンクが終わる場合は、その行を次のチャンクの先頭に回す
    - ページはまたがない（100 文字未満の短いページは次のページとまとめ、page_no は最初のページ）
    """
    merged: list[tuple[int, str]] = []
    carry: tuple[int, str] | None = None
    for page_no, text in pages:
        if carry is not None:
            page_no, text = carry[0], carry[1] + "\n\n" + text
            carry = None
        if len(text.strip()) < SHORT_PAGE:
            carry = (page_no, text)
            continue
        merged.append((page_no, text))
    if carry is not None and carry[1].strip():
        merged.append(carry)

    chunks: list[Chunk] = []
    for page_no, text in merged:
        buf: list[_Sent] = []
        fresh = 0  # buf のうち、前のチャンクと重なっていない文の数
        for s in _sentences(text):
            size = sum(len(x.text) for x in buf)
            if not s.text:  # 段落の切れ目
                if size >= TARGET and fresh:
                    _emit(chunks, page_no, buf, text)
                    buf, fresh = _overlap(buf), 0
                continue
            if fresh and (size + len(s.text) > MAX_LEN or size >= TARGET):
                moved: list[_Sent] = []
                if fresh > 1 and _is_heading(buf[-1]):
                    moved = [buf.pop()]
                _emit(chunks, page_no, buf, text)
                buf, fresh = _overlap(buf) + moved, len(moved)
            buf.append(s)
            fresh += 1
        if fresh:
            _emit(chunks, page_no, buf, text)
    return chunks


def _emit(chunks: list[Chunk], page_no: int, buf: list[_Sent], page_text: str) -> None:
    if buf:
        chunks.append(Chunk(page_no, len(chunks), buf[0].start, buf[-1].end, _join(buf, page_text)))


def _overlap(buf: list[_Sent]) -> list[_Sent]:
    """直前のチャンクの末尾から OVERLAP 文字以内に収まる文（全部が収まる場合は重ねない）。"""
    out: list[_Sent] = []
    total = 0
    for s in reversed(buf):
        if total + len(s.text) > OVERLAP:
            break
        out.insert(0, s)
        total += len(s.text)
    return [] if len(out) == len(buf) else out


def _join(sents: list[_Sent], page_text: str) -> str:
    """元のページ本文での間隔（改行・空白）を保って文をつなぐ。"""
    out = sents[0].text
    for prev, s in zip(sents, sents[1:], strict=False):
        gap = page_text[prev.end:s.start] if s.start >= prev.end else ""
        out += ("\n" if "\n" in gap else (" " if gap else "")) + s.text
    return out
