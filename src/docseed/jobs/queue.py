"""SQLite のテーブルを使ったジョブキュー。docs/design/pipeline.md 2 章。"""

from __future__ import annotations

import datetime as dt
import sqlite3

from ..db import Tx, now

MAX_ATTEMPTS = 3
STALE_MINUTES = 30
PRIORITY = {"process_blob": 0, "reprocess": 5, "reembed_blob": 10, "purge_trash": 20}


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(conn: sqlite3.Connection, job_type: str, blob_id: int | None, *, priority: int | None = None,
            payload: str | None = None) -> bool:
    """ジョブを積む。同じ blob に未完了の同種ジョブがあれば積まない（False を返す）。"""
    with Tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs(type, blob_id, payload, priority, status, run_after, created_at)"
            " VALUES (?,?,?,?, 'pending', ?, ?)",
            (job_type, blob_id, payload, PRIORITY.get(job_type, 0) if priority is None else priority, now(), now()),
        )
        return cur.rowcount == 1


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    t = now()
    with Tx(conn):
        return conn.execute(
            """
            UPDATE jobs SET status = 'running', locked_at = ?, attempts = attempts + 1
             WHERE id = (SELECT id FROM jobs WHERE status = 'pending' AND run_after <= ?
                          ORDER BY priority, id LIMIT 1)
            RETURNING *
            """,
            (t, t),
        ).fetchone()


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    with Tx(conn):
        conn.execute("UPDATE jobs SET status = 'done', finished_at = ?, error = NULL WHERE id = ?", (now(), job_id))


def fail(conn: sqlite3.Connection, job: sqlite3.Row, error: str, *, retry: bool = True) -> bool:
    """失敗を記録する。まだリトライできるなら pending に戻して True を返す。"""
    with Tx(conn):
        if retry and job["attempts"] < MAX_ATTEMPTS:
            delay = 30 * 4 ** (job["attempts"] - 1)  # 30 秒 → 2 分 → 8 分
            run_after = _iso(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay))
            conn.execute("UPDATE jobs SET status = 'pending', run_after = ?, locked_at = NULL, error = ? WHERE id = ?",
                         (run_after, error[:2000], job["id"]))
            return True
        conn.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                     (now(), error[:2000], job["id"]))
        return False


def recover_stale(conn: sqlite3.Connection) -> int:
    """running のまま長時間たったジョブ（worker の異常終了）を pending に戻す。"""
    cutoff = _iso(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=STALE_MINUTES))
    with Tx(conn):
        cur = conn.execute("UPDATE jobs SET status = 'pending', locked_at = NULL WHERE status = 'running'"
                           " AND locked_at < ?", (cutoff,))
        return cur.rowcount


def recover_all_running(conn: sqlite3.Connection) -> int:
    """worker 起動時: 自分は 1 つしか動かないので、running のジョブはすべて前回の中断分。"""
    with Tx(conn):
        cur = conn.execute("UPDATE jobs SET status = 'pending', locked_at = NULL WHERE status = 'running'")
        return cur.rowcount


def cleanup(conn: sqlite3.Connection, days: int = 30) -> None:
    cutoff = _iso(dt.datetime.now(dt.UTC) - dt.timedelta(days=days))
    with Tx(conn):
        conn.execute("DELETE FROM jobs WHERE status IN ('done', 'cancelled') AND finished_at < ?", (cutoff,))
