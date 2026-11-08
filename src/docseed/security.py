"""パスワード・セッション・API トークン・ログイン試行制限。docs/design/security.md 2 章。"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import Tx, now
from .errors import RateLimited, ValidationFailed

_ph = PasswordHasher()  # argon2id, time_cost=3, memory_cost=64MiB, parallelism=4
# ユーザーが存在しない場合も同じ時間をかけるためのダミーのハッシュ
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LEN = 12
SESSION_COOKIE = "docseed_session"
TOKEN_PREFIX = "dsk_"


def hash_password(password: str) -> str:
    validate_password(password)
    return _ph.hash(password)


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValidationFailed(f"パスワードは {MIN_PASSWORD_LEN} 文字以上にしてください",
                               errors=[{"field": "password", "message": f"{MIN_PASSWORD_LEN} 文字以上"}])


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------------------
# セッション
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id_hash: str
    user_id: int
    csrf_token: str
    expires_at: str


def create_session(conn: sqlite3.Connection, user_id: int, *, remember: bool, days: int, short_hours: int,
                   user_agent: str | None, ip: str | None) -> tuple[str, Session]:
    """新しいセッションを作り、(Cookie に入れる値, Session) を返す。"""
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    t = _utcnow()
    expires = t + (dt.timedelta(days=days) if remember else dt.timedelta(hours=short_hours))
    with Tx(conn):
        conn.execute(
            "INSERT INTO sessions(id, user_id, csrf_token, created_at, last_seen_at, expires_at, user_agent, ip)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sha256(raw), user_id, csrf, _iso(t), _iso(t), _iso(expires), (user_agent or "")[:300], ip),
        )
    return raw, Session(sha256(raw), user_id, csrf, _iso(expires))


def load_session(conn: sqlite3.Connection, raw: str, *, remember_days: int) -> Session | None:
    """Cookie の値からセッションを読む。期限切れ・無効ユーザーなら None。"""
    row = conn.execute(
        "SELECT s.*, u.is_active FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (sha256(raw),),
    ).fetchone()
    if row is None or not row["is_active"]:
        return None
    t = _utcnow()
    if row["expires_at"] <= _iso(t):
        delete_session(conn, raw)
        return None
    # 最終アクセスの更新（書き込みを減らすため 1 分に 1 回まで）。長期セッションは期限も延ばす
    last = dt.datetime.strptime(row["last_seen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    expires = row["expires_at"]
    if (t - last).total_seconds() > 60:
        created = dt.datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        old_exp = dt.datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        if (old_exp - created) > dt.timedelta(days=1):  # 「ログインしたままにする」
            expires = _iso(t + dt.timedelta(days=remember_days))
        with Tx(conn):
            conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                         (_iso(t), expires, row["id"]))
    return Session(row["id"], row["user_id"], row["csrf_token"], expires)


def delete_session(conn: sqlite3.Connection, raw: str) -> None:
    with Tx(conn):
        conn.execute("DELETE FROM sessions WHERE id = ?", (sha256(raw),))


def delete_other_sessions(conn: sqlite3.Connection, user_id: int, keep_id_hash: str | None) -> None:
    with Tx(conn):
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND id IS NOT ?", (user_id, keep_id_hash))


# ---------------------------------------------------------------------------
# API トークン
# ---------------------------------------------------------------------------

def create_api_token(conn: sqlite3.Connection, user_id: int, name: str, scopes: str,
                     expires_at: str | None) -> tuple[str, int]:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValidationFailed("トークンの名前を 1〜100 文字で入力してください")
    if set(scopes.split(",")) - {"read", "write"} or not scopes:
        raise ValidationFailed("スコープは read, write のどちらか、または両方です")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with Tx(conn):
        cur = conn.execute(
            "INSERT INTO api_tokens(user_id, name, token_prefix, token_hash, scopes, expires_at, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, name, raw[:12], sha256(raw), scopes, expires_at, now()),
        )
    return raw, int(cur.lastrowid)


def load_api_token(conn: sqlite3.Connection, raw: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT t.*, u.is_active FROM api_tokens t JOIN users u ON u.id = t.user_id"
        " WHERE t.token_hash = ? AND t.revoked_at IS NULL",
        (sha256(raw),),
    ).fetchone()
    if row is None or not row["is_active"]:
        return None
    if row["expires_at"] and row["expires_at"] <= now():
        return None
    last = row["last_used_at"]
    if last is None or last[:16] != now()[:16]:  # 1 分に 1 回まで更新
        with Tx(conn):
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now(), row["id"]))
    return row


# ---------------------------------------------------------------------------
# ログイン試行の制限（メモリ上。api は 1 プロセスのため）
# ---------------------------------------------------------------------------

class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 900):
        self.max = max_failures
        self.window = window_s
        self._fail: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, t: float) -> list[float]:
        v = [x for x in self._fail.get(key, []) if t - x < self.window]
        self._fail[key] = v
        return v

    def check(self, *keys: str) -> None:
        t = time.monotonic()
        with self._lock:
            for k in keys:
                v = self._recent(k, t)
                if len(v) >= self.max:
                    retry = int(self.window - (t - v[0])) + 1
                    minutes = retry // 60 + 1
                    raise RateLimited(f"ログインの失敗が続いたため、{minutes} 分ほど待ってから再度お試しください",
                                      retry_after=retry)

    def failed(self, *keys: str) -> None:
        t = time.monotonic()
        with self._lock:
            for k in keys:
                self._recent(k, t).append(t)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fail.pop(k, None)
