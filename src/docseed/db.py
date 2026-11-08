"""SQLite の接続とマイグレーション。docs/design/data-model.md 1 章。"""

from __future__ import annotations

import datetime as dt
import sqlite3
from importlib import resources
from pathlib import Path

import sqlite_vec

PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size = -65536;
PRAGMA mmap_size = 268435456;
PRAGMA temp_store = MEMORY;
"""


def now() -> str:
    """UTC の ISO 8601 文字列（秒まで）。"""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(PRAGMAS)
    return conn


class Tx:
    """`with Tx(conn):` で BEGIN IMMEDIATE 〜 COMMIT / ROLLBACK。

    autocommit モード（isolation_level=None）で使う。書き込みを伴う処理は必ずこの中で行う。
    入れ子にした場合は外側のトランザクションに含める。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.outer = False

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            self.outer = True
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.outer:
            return
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


def _migrations() -> list[tuple[int, str, str]]:
    out = []
    for f in resources.files("docseed.migrations").iterdir():
        name = f.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), name, f.read_text(encoding="utf-8")))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """未適用のマイグレーションを番号順に適用し、適用したファイル名を返す。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, sql in _migrations():
        if version in done:
            continue
        # executescript は暗黙に COMMIT するため、スクリプト全体を BEGIN/COMMIT で包む
        conn.executescript(
            "BEGIN;\n" + sql + f"\nINSERT INTO schema_migrations VALUES ({version}, '{now()}');\nCOMMIT;"
        )
        applied.append(name)
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        r = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return r[0] or 0
    except sqlite3.OperationalError:
        return 0
