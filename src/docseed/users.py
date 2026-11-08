"""ユーザー管理。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .db import Tx, now
from .errors import NameConflict, NotFound, ValidationFailed
from .security import hash_password

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    is_active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> User:
        return cls(r["id"], r["username"], r["display_name"], r["role"], bool(r["is_active"]))


def get_user(conn: sqlite3.Connection, user_id: int) -> User:
    r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if r is None:
        raise NotFound("ユーザーが見つかりません")
    return User.from_row(r)


def find_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[User]:
    return [User.from_row(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


def root_folder_id(conn: sqlite3.Connection, user_id: int) -> int:
    r = conn.execute("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS NULL", (user_id,)).fetchone()
    if r is None:
        raise NotFound("ルートフォルダがありません")
    return int(r[0])


def create_user(conn: sqlite3.Connection, username: str, display_name: str, password: str,
                role: str = "user") -> User:
    username = username.strip()
    display_name = display_name.strip() or username
    if not USERNAME_RE.match(username):
        raise ValidationFailed("ユーザー名は英数字と . _ - で 2〜32 文字にしてください（先頭は英数字）",
                               errors=[{"field": "username", "message": "形式が正しくありません"}])
    if len(display_name) > 50:
        raise ValidationFailed("表示名は 50 文字以内にしてください")
    if role not in ("admin", "user"):
        raise ValidationFailed("ロールは admin か user です")
    pw_hash = hash_password(password)
    with Tx(conn):
        if find_by_username(conn, username):
            raise NameConflict(f"ユーザー名「{username}」は既に使われています")
        t = now()
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role, is_active, created_at, updated_at)"
            " VALUES (?,?,?,?,1,?,?)",
            (username, display_name, pw_hash, role, t, t),
        )
        uid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO folders(parent_id, owner_id, name, created_at, updated_at) VALUES (NULL, ?, '', ?, ?)",
            (uid, t, t),
        )
    return get_user(conn, uid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    h = hash_password(password)
    with Tx(conn):
        conn.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (h, now(), user_id))


def update_user(conn: sqlite3.Connection, user_id: int, *, display_name: str | None = None,
                role: str | None = None, is_active: bool | None = None) -> User:
    u = get_user(conn, user_id)
    with Tx(conn):
        if display_name is not None:
            display_name = display_name.strip()
            if not display_name or len(display_name) > 50:
                raise ValidationFailed("表示名は 1〜50 文字にしてください")
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, u.id))
        if role is not None:
            if role not in ("admin", "user"):
                raise ValidationFailed("ロールは admin か user です")
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, u.id))
        if is_active is not None:
            conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), u.id))
            if not is_active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (u.id,))
                conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                             (now(), u.id))
        # 有効な管理者が 1 人もいなくなる変更は拒否する
        n = conn.execute("SELECT count(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]
        if n == 0:
            raise ValidationFailed("有効な管理者が 1 人もいなくなるため変更できません")
        conn.execute("UPDATE users SET updated_at = ? WHERE id = ?", (now(), u.id))
    return get_user(conn, user_id)


def usage_bytes(conn: sqlite3.Connection, user_id: int) -> int:
    r = conn.execute(
        "SELECT coalesce(sum(b.size_bytes), 0) FROM documents d JOIN blobs b ON b.id = d.blob_id"
        " JOIN folders f ON f.id = d.folder_id WHERE f.owner_id = ? AND d.deleted_at IS NULL",
        (user_id,),
    ).fetchone()
    return int(r[0])
