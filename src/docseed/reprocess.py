"""再処理の登録。docs/design/pipeline.md 2.5、api.md 3.9。"""

from __future__ import annotations

import json
import sqlite3

from .db import Tx, now
from .jobs.process import PIPELINE_VERSION
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
    return enqueue_reprocess(conn, ids, force_ocr=force_ocr, priority=PRIORITY["reembed_blob"] if scope != "failed"
                             else None)


def enqueue_derive(conn: sqlite3.Connection, scope: str = "missing") -> int:
    """派生ファイル（サムネイル・プレビュー）を作るジョブを積む（OCR はやり直さない）。

    missing: まだ無いもの / all: 画像と PDF のすべて（作り直す）。処理待ち・処理中の blob は除く
    （process_blob がついでに作るため）。
    """
    from .pipeline.extract import kind
    if scope not in ("missing", "all"):
        raise ValueError(scope)
    cond = " AND thumb_path IS NULL" if scope == "missing" else ""
    rows = conn.execute("SELECT id, mime_type FROM blobs WHERE status IN ('ready', 'failed', 'unsupported')"
                        + cond).fetchall()
    ids = [r["id"] for r in rows if kind(r["mime_type"]) in ("image", "pdf")]
    n = 0
    with Tx(conn):
        for bid in ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs(type, blob_id, priority, status, run_after, created_at)"
                " VALUES ('derive_blob', ?, ?, 'pending', ?, ?)", (bid, PRIORITY["derive_blob"], now(), now()))
            n += cur.rowcount
    return n
