"""blob の処理（抽出 → OCR → チャンク分割 → 埋め込み）。docs/design/pipeline.md 1〜6 章。

- 抽出・OCR はページ単位でコミットし、再実行時は処理済みのページを飛ばす（途中再開）
- チャンク分割は全ページがそろってから blob 単位で作り直す（トリガーで全文索引とベクトルも消える）
- 埋め込みはベクトルの無いチャンクだけを対象にする
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..db import Tx, now
from ..pipeline import extract
from ..pipeline.embed import Embedder
from ..pipeline.ocr import OcrEngine, order_lines, prepare_image
from ..pipeline.text import chunk_pages, normalize

log = logging.getLogger("docseed.process")

PIPELINE_VERSION = 1
EMBED_COMMIT_EVERY = 64


class Context:
    """worker が持つ重い部品（OCR・埋め込み）と設定。必要になるまで読み込まない。"""

    def __init__(self, files_dir: Path, *, ocr_factory: Callable[[], OcrEngine],
                 embedder_factory: Callable[[], Embedder], dpi: int = 200, min_score: float = 0.5,
                 between_pages: Callable[[], None] | None = None):
        self.files_dir = files_dir
        self._ocr_factory = ocr_factory
        self._embedder_factory = embedder_factory
        self._ocr: OcrEngine | None = None
        self._embedder: Embedder | None = None
        self.dpi = dpi
        self.min_score = min_score
        self.between_pages = between_pages or (lambda: None)

    @property
    def ocr(self) -> OcrEngine:
        if self._ocr is None:
            self._ocr = self._ocr_factory()
        return self._ocr

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder


def _set(conn: sqlite3.Connection, blob_id: int, **cols) -> None:
    cols["updated_at"] = now()
    keys = ", ".join(f"{k} = ?" for k in cols)
    with Tx(conn):
        conn.execute(f"UPDATE blobs SET {keys} WHERE id = ?", (*cols.values(), blob_id))


def _warn(warnings: list[dict], code: str, **kw) -> None:
    warnings.append({"code": code, **kw})


def process_blob(conn: sqlite3.Connection, ctx: Context, blob_id: int, *, force_ocr: bool = False) -> str:
    """blob を処理して最終状態（ready / unsupported）を返す。リトライすべき失敗は例外を投げる。"""
    blob = conn.execute("SELECT * FROM blobs WHERE id = ?", (blob_id,)).fetchone()
    if blob is None:
        return "gone"  # 処理待ちの間に完全削除された
    path = ctx.files_dir / blob["storage_path"]
    mime = blob["mime_type"]
    warnings: list[dict] = json.loads(blob["warnings"]) if blob["warnings"] and blob["status"] == "processing" else []

    try:
        total = extract.page_count(path, mime)
    except extract.Unsupported as e:
        return _give_up(conn, blob_id, e)
    if total > extract.MAX_PAGES:
        _warn(warnings, "too_many_pages", pages=total, processed=extract.MAX_PAGES)
        total = extract.MAX_PAGES

    done_pages = {r[0] for r in conn.execute("SELECT page_no FROM pages WHERE blob_id = ?", (blob_id,))}
    _set(conn, blob_id, status="processing", page_count=total, progress_total=total,
         progress_done=len(done_pages), error=None)

    # ① 抽出・OCR（ページ単位）
    try:
        for page in extract.iter_pages(path, mime, dpi=ctx.dpi, skip=done_pages, force_ocr=force_ocr):
            _store_page(conn, ctx, blob_id, page, warnings)
            done_pages.add(page.page_no)
            _set(conn, blob_id, progress_done=len(done_pages), warnings=json.dumps(warnings, ensure_ascii=False))
            ctx.between_pages()
    except extract.Unsupported as e:
        return _give_up(conn, blob_id, e)

    # ② チャンク分割（作り直す）
    pages = [(r["page_no"], r["text"]) for r in conn.execute(
        "SELECT page_no, text FROM pages WHERE blob_id = ? ORDER BY page_no", (blob_id,))]
    chunks = chunk_pages([(n, t) for n, t in pages if t.strip()])
    have = conn.execute("SELECT count(*) FROM chunks WHERE blob_id = ?", (blob_id,)).fetchone()[0]
    if have != len(chunks) or force_ocr:
        with Tx(conn):
            conn.execute("DELETE FROM chunks WHERE blob_id = ?", (blob_id,))
            conn.executemany(
                "INSERT INTO chunks(blob_id, page_no, seq, char_start, char_end, text) VALUES (?,?,?,?,?,?)",
                [(blob_id, c.page_no, c.seq, c.char_start, c.char_end, c.text) for c in chunks])

    # ③ 埋め込み（ベクトルの無いチャンクだけ）
    embed_chunks(conn, ctx, blob_id)

    _set(conn, blob_id, status="ready", processed_at=now(), pipeline_version=PIPELINE_VERSION,
         embedding_model=ctx.embedder.model_name, progress_done=total,
         warnings=json.dumps(warnings, ensure_ascii=False) if warnings else None)
    return "ready"


def _give_up(conn: sqlite3.Connection, blob_id: int, e: extract.Unsupported) -> str:
    """リトライしても直らない場合。壊れたファイルは failed、形式が未対応なら unsupported（保存のみ）。"""
    status = "failed" if e.code == "corrupt_file" else "unsupported"
    _set(conn, blob_id, status=status, processed_at=now(), pipeline_version=PIPELINE_VERSION,
         error=json.dumps({"code": e.code, "message": str(e)}, ensure_ascii=False))
    return status


def _store_page(conn: sqlite3.Connection, ctx: Context, blob_id: int, page: extract.Page,
                warnings: list[dict]) -> None:
    if page.source != "ocr":
        with Tx(conn):
            conn.execute("INSERT OR REPLACE INTO pages(blob_id, page_no, source, text) VALUES (?,?,?,?)",
                         (blob_id, page.page_no, page.source, page.text or ""))
        return
    img = prepare_image(page.image)
    t = time.monotonic()
    lines = ctx.ocr.recognize(img)
    secs = time.monotonic() - t
    ordered, text, _vertical = order_lines(lines, ctx.min_score)
    text = normalize(text)
    used = {id(x) for x in ordered}
    conf = float(np.mean([x.confidence for x in ordered])) if ordered else None
    with Tx(conn):
        cur = conn.execute(
            "INSERT OR REPLACE INTO pages(blob_id, page_no, source, text, ocr_confidence, width, height)"
            " VALUES (?,?, 'ocr', ?,?,?,?)", (blob_id, page.page_no, text, conf, img.size[0], img.size[1]))
        page_id = cur.lastrowid
        conn.execute("DELETE FROM ocr_lines WHERE page_id = ?", (page_id,))
        # 読み順の行を先に、本文に採用しなかった行（低信頼度）を後ろに保存する
        rest = [x for x in lines if id(x) not in used]
        conn.executemany(
            "INSERT INTO ocr_lines(page_id, line_no, text, confidence, bbox, vertical) VALUES (?,?,?,?,?,?)",
            [(page_id, i, x.text, x.confidence, json.dumps([[round(a, 1), round(b, 1)] for a, b in x.box]),
              int(x.vertical)) for i, x in enumerate(ordered + rest)])
    if secs > 120:
        _warn(warnings, "ocr_slow", page=page.page_no, seconds=round(secs))
    log.info("ocr blob=%s page=%s lines=%s %.1fs", blob_id, page.page_no, len(lines), secs)


def embed_chunks(conn: sqlite3.Connection, ctx: Context, blob_id: int) -> int:
    rows = conn.execute("SELECT id, text FROM chunks WHERE blob_id = ? ORDER BY seq", (blob_id,)).fetchall()
    todo = [r for r in rows if conn.execute("SELECT 1 FROM chunk_vectors WHERE rowid = ?", (r["id"],)).fetchone()
            is None]
    if not todo:
        return 0
    name = conn.execute(
        "SELECT filename FROM documents WHERE blob_id = ? ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
        (blob_id,)).fetchone()
    title = name[0] if name else ""
    for k in range(0, len(todo), EMBED_COMMIT_EVERY):
        part = todo[k:k + EMBED_COMMIT_EVERY]
        vecs = ctx.embedder.embed_documents([f"{title}\n{r['text']}" if title else r["text"] for r in part])
        with Tx(conn):
            conn.executemany("INSERT OR REPLACE INTO chunk_vectors(rowid, embedding) VALUES (?, ?)",
                             [(r["id"], v.astype(np.float32).tobytes()) for r, v in zip(part, vecs, strict=True)])
        ctx.between_pages()
    return len(todo)


def reset_blob(conn: sqlite3.Connection, blob_id: int) -> None:
    """抽出結果を消して最初から処理し直せるようにする（OCR の強制・パイプラインの版の更新）。"""
    with Tx(conn):
        conn.execute("DELETE FROM chunks WHERE blob_id = ?", (blob_id,))
        conn.execute("DELETE FROM pages WHERE blob_id = ?", (blob_id,))
        conn.execute("UPDATE blobs SET status = 'pending', progress_done = 0, warnings = NULL, error = NULL,"
                     " updated_at = ? WHERE id = ?", (now(), blob_id))


def reembed_blob(conn: sqlite3.Connection, ctx: Context, blob_id: int) -> str:
    """埋め込みモデルだけを変えたとき: ベクトルを作り直す（抽出・OCR はやり直さない）。"""
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE blob_id = ?", (blob_id,))]
    with Tx(conn):
        conn.executemany("DELETE FROM chunk_vectors WHERE rowid = ?", [(i,) for i in ids])
    embed_chunks(conn, ctx, blob_id)
    _set(conn, blob_id, embedding_model=ctx.embedder.model_name)
    return "ready"
