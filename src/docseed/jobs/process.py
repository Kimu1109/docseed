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
from ..pipeline import derive, extract, office
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
                 between_pages: Callable[[], None] | None = None, data_dir: Path | None = None,
                 suggest_enabled: bool = True, llm=None):
        self.files_dir = files_dir
        self.data_dir = data_dir or files_dir.parent   # 派生ファイルは <data_dir>/derived/
        self.suggest_enabled = suggest_enabled         # フォルダ・タグの候補（Phase 9）
        self.llm = llm                                 # 要約（LlmClient。無効なら None）
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

    # 派生ファイル（サムネイル・プレビュー）。抽出のついでに作る。途中再開のときは作り直さない
    if blob["thumb_path"] is None:
        warnings.extend(_derive(conn, ctx, blob))

    done_pages = {r[0] for r in conn.execute("SELECT page_no FROM pages WHERE blob_id = ?", (blob_id,))}
    _set(conn, blob_id, status="processing", page_count=total, progress_total=total,
         progress_done=len(done_pages), error=None)

    # ① 抽出・OCR（ページ単位）
    clip = webclip_info(conn, blob_id) if extract.kind(mime) == "html" else None
    try:
        for page in extract.iter_pages(path, mime, dpi=ctx.dpi, skip=done_pages, force_ocr=force_ocr,
                                       webclip=clip):
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
    after_ready(conn, ctx, blob_id)
    return "ready"


def after_ready(conn: sqlite3.Connection, ctx: Context, blob_id: int) -> None:
    """整理の補助のジョブを積む（候補は受け取り箱などの文書だけ。要約は LLM が有効なら）。"""
    from .. import llm, suggest
    if ctx.suggest_enabled:
        suggest.enqueue_for_blob(conn, blob_id)
    if ctx.llm is not None:
        s = llm.get_summary(conn, blob_id)
        if s is None or s["outdated"]:
            llm.enqueue_summary(conn, blob_id)


def summarize_blob(conn: sqlite3.Connection, ctx: Context, blob_id: int) -> str:
    """要約と新しいタグの提案（LLM）。1 件の上限を超えたら要約なしにする（警告 summary_timeout）。"""
    from .. import llm, suggest
    if ctx.llm is None:
        return "disabled"
    inp = llm.summary_input(conn, blob_id)
    if inp is None:
        return "too_short"   # 本文が短すぎるので要約しない
    ctx.between_pages()          # 温度による一時停止は呼ぶ前に確かめる
    try:
        s = ctx.llm.summarize(*inp, known_tags=llm.known_tags(conn, blob_id))
    except llm.LlmTimeout as e:
        row = conn.execute("SELECT warnings FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        warns = [w for w in json.loads(row[0] or "[]") if w.get("code") != "summary_timeout"]
        warns.append({"code": "summary_timeout", "seconds": ctx.llm.timeout_s})
        _set(conn, blob_id, warnings=json.dumps(warns, ensure_ascii=False))
        log.warning("summarize blob=%s: %s", blob_id, e)
        return "timeout"
    llm.save_summary(conn, blob_id, s, ctx.llm.model)
    tags = llm.merge_tags(conn, s.tags, ctx.embedder) if s.tags else []
    for d in conn.execute("SELECT d.id, f.owner_id FROM documents d JOIN folders f ON f.id = d.folder_id"
                          " WHERE d.blob_id = ? AND d.deleted_at IS NULL", (blob_id,)).fetchall():
        if suggest.get_prefs(conn, d["owner_id"])["suggest_tags"]:
            suggest.store(conn, d["id"], suggest.Result(), folders=False, tags=True, llm_tags=tags)
    log.info("summarize blob=%s %sms tags=%s", blob_id, s.elapsed_ms, tags)
    return "summarized"


def webclip_info(conn: sqlite3.Connection, blob_id: int) -> dict | None:
    """この HTML の blob を使う Web クリップのタイトルなど（最近取得したもの）。無ければ None。"""
    r = conn.execute(
        "SELECT w.url, w.final_url, w.title, w.site_name, w.description, w.meta FROM web_clips w"
        " JOIN documents d ON d.id = w.document_id WHERE d.blob_id = ? AND d.kind = 'webclip'"
        " ORDER BY w.fetched_at DESC, w.id DESC LIMIT 1", (blob_id,)).fetchone()
    if r is None:
        return None
    meta = json.loads(r["meta"]) if r["meta"] else {}
    return {"url": r["final_url"] or r["url"], "title": r["title"], "site_name": r["site_name"],
            "description": r["description"], "oembed_text": (meta.get("oembed") or {}).get("text")}


def _derive(conn: sqlite3.Connection, ctx: Context, blob: sqlite3.Row) -> list[dict]:
    d = derive.derive(ctx.data_dir, ctx.files_dir / blob["storage_path"], blob["mime_type"], blob["sha256"])
    if d.thumb_path or d.view_path or d.image_meta:
        with Tx(conn):
            conn.execute("UPDATE blobs SET thumb_path = ?, view_path = ?, image_meta = ?, updated_at = ? WHERE id = ?",
                         (d.thumb_path, d.view_path, json.dumps(d.image_meta, ensure_ascii=False)
                          if d.image_meta else None, now(), blob["id"]))
    if d.warnings:
        log.warning("derive blob=%s: %s", blob["id"], d.warnings)
    return d.warnings


def derive_blob(conn: sqlite3.Connection, ctx: Context, blob_id: int) -> str:
    """派生ファイルだけを作る（docseed derive missing。OCR はやり直さない）。"""
    blob = conn.execute("SELECT * FROM blobs WHERE id = ?", (blob_id,)).fetchone()
    if blob is None:
        return "gone"
    warns = _derive(conn, ctx, blob)
    if warns:
        old = json.loads(blob["warnings"]) if blob["warnings"] else []
        old = [w for w in old if w.get("code") != "thumbnail_failed"] + warns
        _set(conn, blob_id, warnings=json.dumps(old, ensure_ascii=False))
    return "derived" if not warns else "derive_failed"


def _give_up(conn: sqlite3.Connection, blob_id: int, e: extract.Unsupported) -> str:
    """リトライしても直らない場合。壊れたファイルは failed、形式が未対応なら unsupported（保存のみ）。"""
    status = "failed" if e.code == "corrupt_file" else "unsupported"
    _set(conn, blob_id, status=status, processed_at=now(), pipeline_version=PIPELINE_VERSION,
         error=json.dumps({"code": e.code, "message": str(e)}, ensure_ascii=False))
    return status


def _store_page(conn: sqlite3.Connection, ctx: Context, blob_id: int, page: extract.Page,
                warnings: list[dict]) -> None:
    warnings.extend(page.warnings)
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
    rows = conn.execute("SELECT id, page_no, text FROM chunks WHERE blob_id = ? ORDER BY seq", (blob_id,)).fetchall()
    todo = [r for r in rows if conn.execute("SELECT 1 FROM chunk_vectors WHERE rowid = ?", (r["id"],)).fetchone()
            is None]
    if not todo:
        return 0
    name = conn.execute(
        "SELECT filename, kind FROM documents WHERE blob_id = ? ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
        (blob_id,)).fetchone()
    title = name[0] if name else ""
    if name and name[1] == "webclip":
        title = title.removesuffix(".url")   # Web クリップはタイトル（拡張子 .url は付けない）
    # Excel のシートは、途中から始まるチャンクにもシート名と見出し行を文脈として付ける（pipeline.md 5.2）
    headers = {r[0]: office.table_header(r[1]) for r in conn.execute(
        "SELECT page_no, text FROM pages WHERE blob_id = ? AND source = 'office' AND substr(text, 1, ?) = ?",
        (blob_id, len(office.SHEET_PREFIX), office.SHEET_PREFIX))}

    def embed_input(r: sqlite3.Row) -> str:
        parts = [title] if title else []
        head = headers.get(r["page_no"])
        if head and not r["text"].startswith(office.SHEET_PREFIX):
            parts.append(head)
        return "\n".join([*parts, r["text"]])

    for k in range(0, len(todo), EMBED_COMMIT_EVERY):
        part = todo[k:k + EMBED_COMMIT_EVERY]
        vecs = ctx.embedder.embed_documents([embed_input(r) for r in part])
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
