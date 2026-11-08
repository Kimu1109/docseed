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
    elif scope == "all":
        q = "SELECT id FROM blobs WHERE status IN ('ready', 'failed', 'unsupported')"
    else:
        raise ValueError(scope)
    ids = [r[0] for r in conn.execute(q)]
    return enqueue_reprocess(conn, ids, force_ocr=force_ocr, priority=PRIORITY["reembed_blob"] if scope != "failed"
                             else None)
