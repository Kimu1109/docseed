"""ハイブリッド検索（意味検索 + キーワード検索、RRF で統合）。docs/design/search.md。"""

from __future__ import annotations

import datetime as dt
import html
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .users import User

K_VEC = 200
K_VEC_MAX = 2000
K_KW = 200
RRF_K = 60
SEM_MIN = 0.80       # 意味検索だけでヒットしたチャンクの類似度の下限（ruri-v3-70m で実測して決定。設定で変更可）
SEM_BAND = 0.08      # 意味検索だけのヒットは、最上位の類似度からこの幅以内だけを残す
SNIPPET_LEN = 160
SNIPPETS_PER_DOC = 3
MAX_OFFSET = 500
MODES = ("hybrid", "semantic", "keyword")
FILTER_KEYS = ("type", "tag", "folder", "after", "before")
_TOKEN = re.compile(r'-?"[^"]*"|\S+')


# ---------------------------------------------------------------------------
# クエリの解釈（search.md 2 章）
# ---------------------------------------------------------------------------

@dataclass
class ParsedQuery:
    raw: str
    semantic_text: str = ""
    terms: list[str] = field(default_factory=list)      # キーワード（AND）
    phrases: list[str] = field(default_factory=list)    # ダブルクォート（必須一致）
    excludes: list[str] = field(default_factory=list)   # -語（除外）
    filters: dict[str, str] = field(default_factory=dict)

    @property
    def positive(self) -> list[str]:
        return self.terms + self.phrases

    @property
    def empty(self) -> bool:
        return not self.semantic_text and not self.positive


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip()


def parse_query(q: str) -> ParsedQuery:
    pq = ParsedQuery(raw=q)
    sem: list[str] = []
    for tok in _TOKEN.findall(_norm(q)):
        m = re.fullmatch(r"(type|tag|folder|after|before):(.+)", tok)
        if m:
            pq.filters[m.group(1)] = m.group(2).strip('"')
            continue
        if tok.startswith("-") and len(tok) > 1:
            t = tok[1:].strip('"')
            if t:
                pq.excludes.append(t)
            continue
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            t = tok[1:-1].strip()
            if t:
                pq.phrases.append(t)
                sem.append(t)
            continue
        pq.terms.append(tok)
        sem.append(tok)
    pq.semantic_text = " ".join(sem)
    return pq


def fts_query(terms: list[str], excludes: list[str]) -> str | None:
    """3 文字以上の語だけで FTS5 の検索式を作る（利用者に FTS5 の構文を使わせない）。"""
    def quote(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'
    pos = [quote(t) for t in terms if len(t) >= 3]
    if not pos:
        return None
    expr = " AND ".join(pos)
    for ex in excludes:
        if len(ex) >= 3:
            expr += " NOT " + quote(ex)
    return expr


# ---------------------------------------------------------------------------
# 結果
# ---------------------------------------------------------------------------

@dataclass
class Snippet:
    chunk_id: int
    page_no: int | None
    html: str
    match: list[str]
    summary: bool = False      # 要約（LLM）のチャンクでヒットした


@dataclass
class Hit:
    document: dict[str, Any]
    score: float
    snippets: list[Snippet]


@dataclass
class SearchResult:
    query: str
    mode: str
    total: int
    took_ms: int
    hits: list[Hit]
    notices: list[str]
    parsed: ParsedQuery


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

def accessible_folders(conn: sqlite3.Connection, user: User) -> set[int]:
    rows = conn.execute(
        """
        WITH RECURSIVE acc(folder_id) AS (
          SELECT id FROM folders WHERE owner_id = :u AND deleted_at IS NULL
          UNION
          SELECT m.folder_id FROM folder_members m JOIN folders f ON f.id = m.folder_id
           WHERE m.user_id = :u AND f.deleted_at IS NULL
          UNION
          SELECT f.id FROM folders f JOIN acc a ON f.parent_id = a.folder_id WHERE f.deleted_at IS NULL
        )
        SELECT folder_id FROM acc
        """, {"u": user.id}).fetchall()
    return {r[0] for r in rows}


def _subtree(conn: sqlite3.Connection, folder_id: int) -> set[int]:
    rows = conn.execute(
        "WITH RECURSIVE s(id) AS (SELECT ? UNION SELECT f.id FROM folders f JOIN s ON f.parent_id = s.id"
        " WHERE f.deleted_at IS NULL) SELECT id FROM s", (folder_id,)).fetchall()
    return {r[0] for r in rows}


def _date(s: str) -> str | None:
    try:
        return dt.date.fromisoformat(s.replace("/", "-")).isoformat()
    except ValueError:
        return None


def search(conn: sqlite3.Connection, user: User, q: str, *, mode: str = "hybrid", embedder=None,
           limit: int = 20, offset: int = 0, filters: dict[str, str] | None = None,
           embed_error: str | None = None, sem_min: float | None = None) -> SearchResult:
    from .web.helpers import kind_of  # 種類の判定は一覧の表示と同じ規則を使う

    t0 = time.perf_counter()
    pq = parse_query(q)
    pq.filters.update({k: v for k, v in (filters or {}).items() if v and k in FILTER_KEYS})
    mode = mode if mode in MODES else "hybrid"
    limit = max(1, min(limit, 100))
    offset = max(0, min(offset, MAX_OFFSET))
    notices: list[str] = []
    if pq.empty:
        return SearchResult(q, mode, 0, 0, [], notices, pq)

    use_sem = mode in ("hybrid", "semantic") and bool(pq.semantic_text)
    use_kw = mode in ("hybrid", "keyword")
    if use_sem and embedder is None:
        use_sem = False
        notices.append("意味検索は使えないため、キーワードだけで検索しました"
                       + (f"（{embed_error}）" if embed_error else ""))
        use_kw = True

    # ---- 絞り込みの準備 ----------------------------------------------------
    allowed = accessible_folders(conn, user)
    if "folder" in pq.filters:
        try:
            fid = int(pq.filters["folder"])
            allowed &= _subtree(conn, fid) if fid in allowed else set()
        except ValueError:
            notices.append("folder: にはフォルダの番号を指定してください")
    after = _date(pq.filters["after"]) if "after" in pq.filters else None
    before = _date(pq.filters["before"]) if "before" in pq.filters else None
    if before:
        before = (dt.date.fromisoformat(before) + dt.timedelta(days=1)).isoformat()  # その日を含める

    # ---- (A) 意味検索 ------------------------------------------------------
    sem_rank: dict[int, int] = {}
    sem_sim: dict[int, float] = {}
    qvec = embedder.embed_query(pq.semantic_text) if use_sem else None

    # ---- (B) キーワード検索 ------------------------------------------------
    kw_rank: dict[int, int] = {}
    short = [t for t in pq.positive if len(t) < 3]
    fq = fts_query(pq.positive, pq.excludes) if use_kw else None
    if fq:
        rows = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (fq, K_KW)).fetchall()
        kw_rank = {r[0]: i + 1 for i, r in enumerate(rows)}
    elif use_kw and short and qvec is None:
        # 2 文字以下の語だけで、意味検索の候補も無い（キーワードモード・モデル無し）: 本文を走査する（上限あり）
        rows = conn.execute("SELECT id FROM chunks WHERE instr(lower(text), ?) > 0 ORDER BY id DESC LIMIT ?",
                            (short[0].lower(), K_KW)).fetchall()
        kw_rank = {r[0]: i + 1 for i, r in enumerate(rows)}

    k = K_VEC
    while True:
        if qvec is not None:
            rows = conn.execute(
                "SELECT rowid, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = ?",
                (np.asarray(qvec, dtype=np.float32).tobytes(), k)).fetchall()
            sem_rank = {r[0]: i + 1 for i, r in enumerate(rows)}
            sem_sim = {r[0]: 1.0 - r[1] for r in rows}
        cand = set(sem_rank) | set(kw_rank)
        docs, chunk_docs, texts = _resolve(conn, cand, allowed, after, before, pq, kind_of)
        # 本文で条件を確かめる: 除外語を含まない・ダブルクォートの語を含む（必須）
        # キーワードのヒット = すべての語を含むチャンク（3 文字以上は FTS、2 文字以下は本文で照合）
        valid: set[int] = set()
        kw_hits: set[int] = set()
        for cid, text in texts.items():
            low = text.lower()
            if any(ex.lower() in low for ex in pq.excludes):
                continue
            if any(ph.lower() not in low for ph in pq.phrases):
                continue
            valid.add(cid)
            if use_kw and pq.positive and all(t.lower() in low for t in pq.positive):
                kw_hits.add(cid)
        # キーワード側の順位: FTS の順位、続いて意味検索の候補の中で見つかったものを意味検索の順に
        extra = sorted((c for c in kw_hits if c not in kw_rank), key=lambda c: sem_rank.get(c, 10 ** 9))
        ranked = [c for c in sorted(kw_rank, key=kw_rank.get) if c in kw_hits] + extra
        kw_final = {c: i + 1 for i, c in enumerate(ranked)}
        # 意味検索だけのヒットは、類似度が低いものを落とす
        top = max((sem_sim[c] for c in valid if c in sem_sim), default=0.0)
        floor = SEM_MIN if sem_min is None else sem_min
        sem_ok = {c for c in valid if c in sem_sim and sem_sim[c] >= max(floor, top - SEM_BAND)}
        if mode == "keyword":
            keep = set(kw_final)
        elif mode == "semantic":
            keep = sem_ok
        else:
            keep = set(kw_final) | sem_ok
        n_docs = len({d for c in keep for d in chunk_docs.get(c, [])})
        if qvec is None or n_docs >= offset + limit or len(sem_rank) < k or k >= K_VEC_MAX:
            break
        k *= 2  # 権限・絞り込みで減りすぎたので取り直す（search.md 4.1）

    # ---- RRF で統合し、文書単位にまとめる（search.md 6 章） ---------------
    if use_kw and short and not fq and qvec is not None:
        notices.append("2 文字以下の語（" + "・".join(short) + "）は、意味の近い候補の中でだけ照合しました")
    w_kw = 1.5 if pq.phrases else 1.0
    chunk_score: dict[int, float] = {}
    for c in keep:
        s = 0.0
        if c in sem_rank and mode != "keyword":
            s += 1.0 / (RRF_K + sem_rank[c])
        if c in kw_final and mode != "semantic":
            s += w_kw / (RRF_K + kw_final[c])
        chunk_score[c] = s
    per_doc: dict[int, list[int]] = {}
    for c in keep:
        for d in chunk_docs.get(c, []):
            per_doc.setdefault(d, []).append(c)
    scored = []
    for d, cs in per_doc.items():
        cs.sort(key=lambda c: -chunk_score[c])
        s = chunk_score[cs[0]] + 0.1 * sum(chunk_score[c] for c in cs[1:])
        name = docs[d]["filename"].lower()
        if any(t.lower() in name for t in pq.positive):
            s += 1.0 / RRF_K
        scored.append((s, d, cs))
    scored.sort(key=lambda x: (-x[0], x[1]))
    total = len(scored)

    hits = []
    marks = pq.positive or pq.semantic_text.split()
    for s, d, cs in scored[offset:offset + limit]:
        snippets = []
        for c in cs[:SNIPPETS_PER_DOC]:
            match = []
            if c in kw_final:
                match.append("keyword")
            if c in sem_rank and c not in kw_final:
                match.append("semantic")
            page_no, source = chunk_place(conn, c)
            snippets.append(Snippet(c, page_no, make_snippet(texts[c], marks), match, source == "summary"))
        hits.append(Hit(docs[d], round(s, 5), snippets))
    took = int((time.perf_counter() - t0) * 1000)
    return SearchResult(q, mode, total, took, hits, notices, pq)


def chunk_place(conn: sqlite3.Connection, chunk_id: int) -> tuple[int | None, str]:
    """(ページ番号, 本文か要約か)。"""
    r = conn.execute("SELECT page_no, source FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return (r[0], r[1]) if r else (None, "body")


def _resolve(conn, cand: set[int], allowed: set[int], after, before, pq: ParsedQuery, kind_of):
    """候補チャンク → 見られる文書。(文書の辞書, チャンク→文書 ID の一覧, チャンク本文) を返す。"""
    if not cand or not allowed:
        return {}, {}, {}
    ids = list(cand)
    rows = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows += conn.execute(
            f"SELECT c.id AS chunk_id, c.text, d.id, d.filename, d.kind, d.folder_id, d.updated_at, d.created_at,"
            f" b.mime_type, b.size_bytes, b.status"
            f" FROM chunks c JOIN documents d ON d.blob_id = c.blob_id AND d.deleted_at IS NULL"
            # 手で直した要約のチャンクはその文書だけ。手で直した（消した）文書は、自動の要約では当てない
            f" AND (c.document_id IS NULL OR c.document_id = d.id)"
            f" AND NOT (c.source = 'summary' AND c.document_id IS NULL"
            f"          AND d.id IN (SELECT document_id FROM document_summaries))"
            f" JOIN blobs b ON b.id = c.blob_id WHERE c.id IN ({','.join('?' * len(part))})", part).fetchall()
    tag = pq.filters.get("tag")
    tagged: set[int] | None = None
    if tag:
        tagged = {r[0] for r in conn.execute(
            "SELECT dt.document_id FROM document_tags dt JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?", (tag,))}
    want_type = pq.filters.get("type")
    docs: dict[int, dict] = {}
    chunk_docs: dict[int, list[int]] = {}
    texts: dict[int, str] = {}
    for r in rows:
        if r["folder_id"] not in allowed:
            continue
        if after and r["updated_at"] < after:
            continue
        if before and r["updated_at"] >= before:
            continue
        if tagged is not None and r["id"] not in tagged:
            continue
        if want_type and kind_of(r["mime_type"], r["filename"], r["kind"]) != want_type:
            continue
        docs[r["id"]] = {"id": r["id"], "filename": r["filename"], "kind": r["kind"], "folder_id": r["folder_id"],
                         "mime_type": r["mime_type"], "size_bytes": r["size_bytes"], "status": r["status"],
                         "updated_at": r["updated_at"], "created_at": r["created_at"]}
        chunk_docs.setdefault(r["chunk_id"], []).append(r["id"])
        texts[r["chunk_id"]] = r["text"]
    return docs, chunk_docs, texts


def make_snippet(text: str, terms: list[str]) -> str:
    """検索語の付近を SNIPPET_LEN 文字ほど切り出し、HTML エスケープしてから語を <mark> で囲む。"""
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    pos = min((p for p in (low.find(t.lower()) for t in terms if t) if p >= 0), default=-1)
    start = max(0, pos - SNIPPET_LEN // 3) if pos >= 0 else 0
    piece = flat[start:start + SNIPPET_LEN]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + SNIPPET_LEN < len(flat) else ""
    out = html.escape(piece)
    for t in sorted({t for t in terms if t}, key=len, reverse=True):
        esc = html.escape(t)
        out = re.sub(re.escape(esc), lambda m: f"<mark>{m.group(0)}</mark>", out, flags=re.IGNORECASE)
    out = re.sub(r"<mark>(<mark>)+", "<mark>", re.sub(r"(</mark>)+</mark>", "</mark>", out))
    return html.escape(prefix) + out + html.escape(suffix)


# ---------------------------------------------------------------------------
# クエリの埋め込み（api プロセスで 1 つだけ持つ）
# ---------------------------------------------------------------------------

class QueryEmbedder:
    """検索クエリの埋め込み。モデルは最初の検索で読み込み、同じクエリは LRU で使い回す。"""

    def __init__(self, models_dir, model_name: str, threads: int = 1, cache_size: int = 256):
        import threading
        from collections import OrderedDict
        self._args = (models_dir, model_name, threads)
        self._model = None
        self._error: str | None = None
        self._failed_at = 0.0
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._size = cache_size

    def get(self):
        """(埋め込み器 or None, 使えない理由)。"""
        with self._lock:
            # 失敗しても 60 秒たてば読み込みを試し直す（後からモデルを取得した場合に再起動を不要にする）
            if self._model is None and (self._error is None or time.monotonic() - self._failed_at > 60):
                from .pipeline.embed import OnnxEmbedder
                try:
                    self._model = OnnxEmbedder(*self._args[:2], threads=self._args[2])
                    self._error = None
                except Exception as e:  # noqa: BLE001 - モデルが無い等。キーワード検索だけで続ける
                    self._error = "埋め込みモデルが読み込めません" if "fetch-models" in str(e) else str(e)[:200]
                    self._failed_at = time.monotonic()
            return (self if self._model is not None else None), self._error

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            v = self._cache.get(text)
            if v is not None:
                self._cache.move_to_end(text)
                return v
            v = self._model.embed_query(text)
            self._cache[text] = v
            if len(self._cache) > self._size:
                self._cache.popitem(last=False)
            return v
