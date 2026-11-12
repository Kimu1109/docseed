"""タグの管理（一覧・名前の変更・統合・削除）。docs/design/ui.md「タグ」。

タグ（tags）はすべての利用者で共有する名前の表なので、名前の変更・削除は「自分のファイル」（自分が
持ち主のフォルダにある文書。ゴミ箱の中も含む）に付いたタグだけを付け替える。ほかの利用者の文書の
タグは変えない。どの文書にも付いていないタグの名前は、そのたびに片付ける。
"""

from __future__ import annotations

import sqlite3

from .audit import audit
from .db import Tx
from .errors import NotFound, ValidationFailed
from .users import User

MAX_TAG_LEN = 50

_MINE = ("SELECT d.id FROM documents d JOIN folders f ON f.id = d.folder_id WHERE f.owner_id = ?")


def list_tags(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """自分のファイルに付いているタグと件数（ゴミ箱の中は数えない）。件数の多い順。"""
    rows = conn.execute(
        "SELECT t.name, count(*) AS n, max(d.updated_at) AS last FROM document_tags dt"
        " JOIN tags t ON t.id = dt.tag_id JOIN documents d ON d.id = dt.document_id"
        " JOIN folders f ON f.id = d.folder_id"
        " WHERE f.owner_id = ? AND d.deleted_at IS NULL GROUP BY t.id ORDER BY n DESC, t.name", (user_id,))
    return [{"name": r["name"], "count": r["n"], "last": r["last"]} for r in rows]


def documents_with(conn: sqlite3.Connection, user_id: int, name: str, limit: int = 200) -> list[sqlite3.Row]:
    """自分のファイルのうち、このタグが付いているもの（新しい順）。"""
    return conn.execute(
        "SELECT d.id, d.filename, d.kind, d.updated_at, d.folder_id, b.mime_type FROM document_tags dt"
        " JOIN tags t ON t.id = dt.tag_id JOIN documents d ON d.id = dt.document_id"
        " JOIN folders f ON f.id = d.folder_id JOIN blobs b ON b.id = d.blob_id"
        " WHERE f.owner_id = ? AND d.deleted_at IS NULL AND t.name = ? ORDER BY d.updated_at DESC LIMIT ?",
        (user_id, name, limit)).fetchall()


def _tag_id(conn: sqlite3.Connection, name: str) -> int | None:
    r = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()   # name は COLLATE NOCASE
    return r[0] if r else None


def _clean(name: str) -> str:
    name = (name or "").replace("、", "").replace(",", "").strip()
    if not name:
        raise ValidationFailed("タグの名前を入力してください")
    if len(name) > MAX_TAG_LEN:
        raise ValidationFailed(f"タグの名前は {MAX_TAG_LEN} 字以内にしてください")
    return name


def _gc(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM document_tags)")


def rename(conn: sqlite3.Connection, user: User, old: str, new: str) -> int:
    """自分のファイルのタグ old を new に付け替える。new が既にあれば統合になる。変えた文書の数を返す。"""
    new = _clean(new)
    old_id = _tag_id(conn, old)
    if old_id is None:
        raise NotFound("そのタグはありません")
    docs = [r[0] for r in conn.execute(
        f"SELECT document_id FROM document_tags WHERE tag_id = ? AND document_id IN ({_MINE})", (old_id, user.id))]
    if not docs:
        raise NotFound("そのタグはありません")
    with Tx(conn):
        if new.lower() == old.lower():
            if new == old:
                return 0
            # 大文字・小文字だけの変更: 名前の表は共有なので、ほかの利用者が使っていなければ名前を書き換える
            others = conn.execute(f"SELECT 1 FROM document_tags WHERE tag_id = ? AND document_id NOT IN ({_MINE})"
                                  " LIMIT 1", (old_id, user.id)).fetchone()
            if others:
                raise ValidationFailed("ほかの利用者も使っているタグなので、大文字・小文字だけの変更はできません")
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new, old_id))
        else:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (new,))
            new_id = _tag_id(conn, new)
            conn.executemany("INSERT OR IGNORE INTO document_tags(document_id, tag_id) VALUES (?, ?)",
                             [(d, new_id) for d in docs])
            conn.executemany("DELETE FROM document_tags WHERE document_id = ? AND tag_id = ?",
                             [(d, old_id) for d in docs])
            _gc(conn)
        audit(conn, "tag_rename", user_id=user.id, old=old, new=new, documents=len(docs))
    return len(docs)


def delete(conn: sqlite3.Connection, user: User, name: str) -> int:
    """自分のファイルからタグを外す（ファイルは消さない）。外した文書の数を返す。"""
    tag_id = _tag_id(conn, name)
    if tag_id is None:
        raise NotFound("そのタグはありません")
    with Tx(conn):
        cur = conn.execute(f"DELETE FROM document_tags WHERE tag_id = ? AND document_id IN ({_MINE})",
                           (tag_id, user.id))
        if not cur.rowcount:
            raise NotFound("そのタグはありません")
        _gc(conn)
        audit(conn, "tag_delete", user_id=user.id, name=name, documents=cur.rowcount)
    return cur.rowcount
