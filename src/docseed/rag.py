"""質問に答える（検索 + 小型の LLM による生成。RAG）。docs/design/search.md 10 章。

1. 質問で検索する（いつものハイブリッド検索。見る人が開ける文書だけ）。
2. 上位の文書から、当たった部分（チャンク）を文字数の上限まで集め、[1] [2] … と番号を付けて資料にする。
3. 「資料に書いてあることだけで答え、根拠の番号を付ける。分からなければ分からないと答える」と頼む。
4. 答えに出てきた番号を、資料（文書とページ）への参照にする。

資料が 1 つも見つからなければ、LLM は呼ばない（作り話を防ぐ）。質問の本文は保存しない。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from .search import SearchResult

MAX_SOURCES = 6            # 資料にするチャンクの数の上限
PER_DOC = 2                # 1 つの文書から使うチャンクの数の上限
SEARCH_DOCS = 5            # 資料を探す文書の数
MAX_ANSWER_TOKENS = 400
MAX_QUESTION = 300
BUSY_WAIT_S = 5.0          # ほかの質問に答えている間、待つ秒数（それを超えたら「混んでいます」）
_slots = threading.BoundedSemaphore(2)

SYSTEM = (
    "あなたは、利用者の文書を調べて質問に答える係です。"
    "答えには、下の「資料」に書いてあることだけを使ってください。資料に無いことを推測で書いてはいけません。"
    "資料から答えが分からないときは「資料からは分かりません」とだけ答えてください。"
    "答えは日本語で、3 文・200 字以内にまとめてください。"
    "根拠にした資料の番号を、その文の終わりに [1] のように付けてください。"
)

_CITE = re.compile(r"\[(\d{1,2})\]")


class Busy(Exception):
    pass


@dataclass
class Source:
    n: int
    document_id: int
    filename: str
    folder_id: int
    page_no: int | None
    text: str


@dataclass
class Prepared:
    question: str
    sources: list[Source] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.sources


def clean_question(q: str) -> str:
    q = " ".join((q or "").split())
    return q[:MAX_QUESTION]


def collect_sources(conn: sqlite3.Connection, result: SearchResult, max_chars: int) -> list[Source]:
    """検索の結果から資料を集める（文書の順に、当たったチャンクを文書ごとに PER_DOC まで）。"""
    picked: list[tuple[dict, int]] = []
    for h in result.hits:
        for s in h.snippets[:PER_DOC]:
            picked.append((h.document, s.chunk_id))
    if not picked:
        return []
    ids = [c for _, c in picked]
    texts = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT id, text, page_no FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)}
    out: list[Source] = []
    used = 0
    per_source = max(200, max_chars // MAX_SOURCES)
    for doc, cid in picked:
        if cid not in texts or len(out) >= MAX_SOURCES:
            continue
        text = " ".join(texts[cid][0].split())[:per_source]
        if used + len(text) > max_chars and out:
            break
        used += len(text)
        out.append(Source(len(out) + 1, doc["id"], doc["filename"], doc["folder_id"], texts[cid][1], text))
    return out


def build_prompt(question: str, sources: list[Source]) -> str:
    lines = ["資料:"]
    for s in sources:
        where = f"（{s.page_no} ページ）" if s.page_no else ""
        lines.append(f"[{s.n}] {s.filename}{where}\n{s.text}")
    lines.append(f"\n質問: {question}")
    return "\n\n".join(lines)


def cited(answer: str, sources: list[Source]) -> list[int]:
    """答えに出てきた資料の番号（出てきた順、重複なし、範囲外は除く）。"""
    seen: list[int] = []
    for m in _CITE.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= len(sources) and n not in seen:
            seen.append(n)
    return seen


def answer_stream(client, prepared: Prepared) -> Iterator[str]:
    """答えを少しずつ返す。同時に答えるのは 2 件まで（llama-server の枠に合わせる）。"""
    if not _slots.acquire(timeout=BUSY_WAIT_S):
        raise Busy("ほかの質問に答えています。少し待ってからもう一度お試しください")
    try:
        yield from client.stream_chat(SYSTEM, build_prompt(prepared.question, prepared.sources),
                                      max_tokens=MAX_ANSWER_TOKENS)
    finally:
        _slots.release()
