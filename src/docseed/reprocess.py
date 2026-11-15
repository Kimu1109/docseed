"""再処理の登録。docs/design/pipeline.md 2.5、api.md 3.9。"""

from __future__ import annotations

import json
import sqlite3

from .db import Tx, now
from .jobs.process import PIPELINE_CHANGES, PIPELINE_VERSION
from .jobs.queue import PRIORITY


def enqueue_reprocess(conn: sqlite3.Connection, blob_ids: list[int], *, force_ocr: bool = False,
                      priority: int | None = None) -> int:
    """抽出結果を消して最初から処理し直すジョブを積む。処理待ち・処理中のものは積まない。"""
    payload = json.dumps({"reset": True, "force_ocr": force_ocr})
    n = 0
    with Tx(conn):
        for bid in blob_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs(type, blob_id, payload, priority, status, run_after, created_at)"
                " VALUES ('process_blob', ?, ?, ?, 'pending', ?, ?)",
                (bid, payload, PRIORITY["reprocess"] if priority is None else priority, now(), now()))
            if cur.rowcount:
                conn.execute("UPDATE blobs SET status = 'pending', updated_at = ? WHERE id = ?", (now(), bid))
                n += 1
    return n


def enqueue_pages(conn: sqlite3.Connection, blob_id: int, pages: list[int], *, force_ocr: bool = False) -> bool:
    """指定したページだけを読み直すジョブを積む（ほかのページの結果はそのまま）。処理待ち・処理中なら積まない。

    force_ocr: PDF の埋め込みテキストを使わず、ページを画像として読み取る（そのページだけ）。
    """
    payload = json.dumps({"pages": sorted(set(pages)), "force_ocr": force_ocr})
    with Tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs(type, blob_id, payload, priority, status, run_after, created_at)"
            " VALUES ('process_blob', ?, ?, ?, 'pending', ?, ?)",
            (blob_id, payload, PRIORITY["reprocess"], now(), now()))
        if cur.rowcount:
            conn.execute("UPDATE blobs SET status = 'pending', updated_at = ? WHERE id = ?", (now(), blob_id))
    return cur.rowcount == 1


def reprocess(conn: sqlite3.Connection, scope: str, *, force_ocr: bool = False) -> int:
    if scope == "failed":
        q = "SELECT id FROM blobs WHERE status = 'failed'"
    elif scope == "outdated":
        q = f"SELECT id FROM blobs WHERE status = 'ready' AND coalesce(pipeline_version, 0) < {PIPELINE_VERSION}"
    elif scope == "unsupported":
        # 対応形式が増えたとき（Phase 5 の Office 文書など）。未対応のままのものはすぐ unsupported に戻る
        q = "SELECT id FROM blobs WHERE status = 'unsupported'"
    elif scope == "all":
        q = "SELECT id FROM blobs WHERE status IN ('ready', 'failed', 'unsupported')"
    else:
        raise ValueError(scope)
    ids = [r[0] for r in conn.execute(q)]
    if scope == "outdated":
        ids = _skip_unchanged(conn, ids)
    return enqueue_reprocess(conn, ids, force_ocr=force_ocr, priority=PRIORITY["reembed_blob"] if scope != "failed"
                             else None)


def _skip_unchanged(conn: sqlite3.Connection, blob_ids: list[int]) -> list[int]:
    """古い版で処理した blob のうち、その後の版の変更が及ぶ形式のものだけを返す。

    及ばないもの（PDF の変更のときの画像・テキストなど）は処理し直しても結果が同じなので、版だけを上げる。
    """
    from .pipeline.extract import kind
    todo, bump = [], []
    for bid in blob_ids:
        r = conn.execute("SELECT mime_type, coalesce(pipeline_version, 0) FROM blobs WHERE id = ?", (bid,)).fetchone()
        if r is None:
            continue
        changed = range(r[1] + 1, PIPELINE_VERSION + 1)
        # 変更の記録が無い版（1 以前）は、すべての形式に及ぶものとして扱う
        if any(v not in PIPELINE_CHANGES or kind(r[0]) in PIPELINE_CHANGES[v] for v in changed):
            todo.append(bid)
        else:
            bump.append(bid)
    if bump:
        with Tx(conn):
            conn.executemany("UPDATE blobs SET pipeline_version = ? WHERE id = ?",
                             [(PIPELINE_VERSION, b) for b in bump])
    return todo


def enqueue_derive(conn: sqlite3.Connection, scope: str = "missing") -> int:
    """派生ファイル（サムネイル・プレビュー）を作るジョブを積む（OCR はやり直さない）。

    missing: まだ無いもの / all: 画像と PDF のすべて（作り直す）。処理待ち・処理中の blob は除く
    （process_blob がついでに作るため）。
    """
    from .pipeline.extract import kind
    if scope not in ("missing", "all"):
        raise ValueError(scope)
    cond = " AND thumb_path IS NULL AND view_path IS NULL" if scope == "missing" else ""
    rows = conn.execute("SELECT id, mime_type FROM blobs WHERE status IN ('ready', 'failed', 'unsupported')"
                        + cond).fetchall()
    ids = [r["id"] for r in rows
           if kind(r["mime_type"]) in ("image", "pdf") or (r["mime_type"] or "").startswith("video/")]
    n = 0
    with Tx(conn):
        for bid in ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs(type, blob_id, priority, status, run_after, created_at)"
                " VALUES ('derive_blob', ?, ?, 'pending', ?, ?)", (bid, PRIORITY["derive_blob"], now(), now()))
            n += cur.rowcount
    return n
