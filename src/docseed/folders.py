"""フォルダと権限。docs/design/security.md 3 章、data-model.md 3.2 / 4 章。"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from .db import Tx, now
from .errors import Forbidden, InvalidMove, NameConflict, NotFound, ValidationFailed
from .users import User

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def clean_name(name: str, *, what: str = "名前") -> str:
    """ファイル名・フォルダ名を整える。パス区切り・制御文字を除き、255 バイト以内に切り詰める。"""
    name = unicodedata.normalize("NFC", name)
    name = _BAD_CHARS.sub("", name).strip()
    if name in (".", ".."):
        name = ""
    if not name:
        raise ValidationFailed(f"{what}を入力してください", errors=[{"field": "name", "message": "空です"}])
    b = name.encode("utf-8")
    if len(b) > 255:
        # 拡張子を残して切り詰める
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext.encode()) <= 16:
            keep = 255 - len(ext.encode()) - 1
            name = stem.encode()[:keep].decode("utf-8", "ignore") + "." + ext
        else:
            name = b[:255].decode("utf-8", "ignore")
    return name


# ---------------------------------------------------------------------------
# 権限
# ---------------------------------------------------------------------------

def get_folder_row(conn: sqlite3.Connection, folder_id: int, *, include_deleted: bool = False) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if r is None or (r["deleted_at"] and not include_deleted):
        raise NotFound("フォルダが見つかりません")
    return r


def effective_role(conn: sqlite3.Connection, user: User, folder_id: int) -> str | None:
    """ユーザーのフォルダに対する実効ロール（owner / editor / viewer / None）。"""
    r = conn.execute(
        """
        WITH RECURSIVE anc(id, parent_id) AS (
          SELECT id, parent_id FROM folders WHERE id = :f
          UNION ALL
          SELECT f.id, f.parent_id FROM folders f JOIN anc a ON f.id = a.parent_id
        )
        SELECT (SELECT owner_id FROM folders WHERE id = :f) AS owner_id,
               (SELECT MAX(CASE m.role WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 END)
                  FROM folder_members m JOIN anc a ON a.id = m.folder_id
                 WHERE m.user_id = :u) AS member_rank
        """,
        {"f": folder_id, "u": user.id},
    ).fetchone()
    if r is None or r["owner_id"] is None:
        return None
    if r["owner_id"] == user.id:
        return "owner"
    return {2: "editor", 1: "viewer"}.get(r["member_rank"])


def require_folder_role(conn: sqlite3.Connection, user: User, folder_id: int, role: str) -> str:
    """必要なロールを満たさなければ例外。権限が全く無い場合は存在を隠すため 404。"""
    get_folder_row(conn, folder_id)
    actual = effective_role(conn, user, folder_id)
    if actual is None:
        raise NotFound("フォルダが見つかりません")
    if ROLE_RANK[actual] < ROLE_RANK[role]:
        raise Forbidden({"editor": "このフォルダを変更する権限がありません",
                         "owner": "この操作はフォルダの所有者だけが行えます"}.get(role, "権限がありません"))
    return actual


# ---------------------------------------------------------------------------
# 参照
# ---------------------------------------------------------------------------

@dataclass
class Crumb:
    id: int
    name: str


def path(conn: sqlite3.Connection, folder_id: int) -> list[Crumb]:
    rows = conn.execute(
        """
        WITH RECURSIVE anc(id, parent_id, name, depth) AS (
          SELECT id, parent_id, name, 0 FROM folders WHERE id = ?
          UNION ALL
          SELECT f.id, f.parent_id, f.name, a.depth + 1 FROM folders f JOIN anc a ON f.id = a.parent_id
        )
        SELECT id, name FROM anc ORDER BY depth DESC
        """,
        (folder_id,),
    ).fetchall()
    return [Crumb(r["id"], r["name"]) for r in rows]


def list_children(conn: sqlite3.Connection, folder_id: int, *, sort: str = "name",
                  order: str = "asc") -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(子フォルダ, 文書) を返す。フォルダは常に名前順で先頭。"""
    folders = conn.execute(
        "SELECT f.*, (SELECT count(*) FROM documents d WHERE d.folder_id = f.id AND d.deleted_at IS NULL)"
        " + (SELECT count(*) FROM folders c WHERE c.parent_id = f.id AND c.deleted_at IS NULL) AS item_count,"
        " (SELECT count(*) FROM folder_members m WHERE m.folder_id = f.id) AS member_count"
        " FROM folders f WHERE f.parent_id = ? AND f.deleted_at IS NULL ORDER BY f.name COLLATE NOCASE",
        (folder_id,),
    ).fetchall()
    key = {"name": "d.filename COLLATE NOCASE", "updated_at": "d.updated_at", "size": "b.size_bytes"}.get(sort)
    if key is None:
        raise ValidationFailed("sort は name / updated_at / size のいずれかです")
    direction = "DESC" if order == "desc" else "ASC"
    docs = conn.execute(
        f"SELECT d.*, b.size_bytes, b.mime_type, b.status, b.sha256, b.page_count,"
        f" b.progress_done, b.progress_total, b.warnings, b.thumb_path, b.view_path"
        f" FROM documents d JOIN blobs b ON b.id = d.blob_id"
        f" WHERE d.folder_id = ? AND d.deleted_at IS NULL ORDER BY {key} {direction}, d.id",
        (folder_id,),
    ).fetchall()
    return folders, docs


def folders_of_owner(conn: sqlite3.Connection, owner_id: int) -> list[tuple[int, str]]:
    """移動先の候補（同じ所有者の全フォルダ）を (id, パス表記) で返す。"""
    rows = conn.execute(
        "SELECT id, parent_id, name FROM folders WHERE owner_id = ? AND deleted_at IS NULL", (owner_id,)
    ).fetchall()
    by_id = {r["id"]: r for r in rows}

    def label(fid: int) -> str:
        parts = []
        while fid is not None and fid in by_id:
            r = by_id[fid]
            parts.append(r["name"])
            fid = r["parent_id"]
        parts = [p for p in reversed(parts) if p]
        return "/" + "/".join(parts)

    return sorted(((r["id"], label(r["id"])) for r in rows), key=lambda x: x[1])


def shared_with(conn: sqlite3.Connection, user: User) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, m.role, u.display_name AS owner_name FROM folder_members m"
        " JOIN folders f ON f.id = m.folder_id JOIN users u ON u.id = f.owner_id"
        " WHERE m.user_id = ? AND f.deleted_at IS NULL ORDER BY u.display_name, f.name",
        (user.id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 変更
# ---------------------------------------------------------------------------

def _name_taken(conn: sqlite3.Connection, parent_id: int, name: str, exclude_id: int | None = None) -> bool:
    return conn.execute(
        "SELECT 1 FROM folders WHERE parent_id = ? AND name = ? COLLATE NOCASE AND deleted_at IS NULL"
        " AND id IS NOT ?", (parent_id, name, exclude_id)
    ).fetchone() is not None


def free_folder_name(conn: sqlite3.Connection, parent_id: int, name: str) -> str:
    if not _name_taken(conn, parent_id, name):
        return name
    n = 2
    while _name_taken(conn, parent_id, f"{name} ({n})"):
        n += 1
    return f"{name} ({n})"


def create_folder(conn: sqlite3.Connection, user: User, parent_id: int, name: str) -> int:
    name = clean_name(name, what="フォルダ名")
    with Tx(conn):
        require_folder_role(conn, user, parent_id, "editor")
        parent = get_folder_row(conn, parent_id)
        if _name_taken(conn, parent_id, name):
            raise NameConflict(f"「{name}」という名前のフォルダは既にあります")
        t = now()
        cur = conn.execute(
            "INSERT INTO folders(parent_id, owner_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (parent_id, parent["owner_id"], name, t, t),
        )
        return int(cur.lastrowid)


def ensure_path(conn: sqlite3.Connection, user: User, parent_id: int, parts: list[str]) -> int:
    """parent_id の下に parts のフォルダを（無ければ作って）たどり、最後のフォルダ ID を返す。"""
    fid = parent_id
    for raw in parts:
        name = clean_name(raw, what="フォルダ名")
        r = conn.execute(
            "SELECT id FROM folders WHERE parent_id = ? AND name = ? COLLATE NOCASE AND deleted_at IS NULL",
            (fid, name),
        ).fetchone()
        fid = r[0] if r else create_folder(conn, user, fid, name)
    return fid


def is_descendant(conn: sqlite3.Connection, folder_id: int, ancestor_id: int) -> bool:
    return any(c.id == ancestor_id for c in path(conn, folder_id))


def update_folder(conn: sqlite3.Connection, user: User, folder_id: int, *, name: str | None = None,
                  parent_id: int | None = None) -> None:
    with Tx(conn):
        f = get_folder_row(conn, folder_id)
        if f["parent_id"] is None:
            raise InvalidMove("ルートフォルダは変更できません")
        require_folder_role(conn, user, folder_id, "editor")
        new_parent = f["parent_id"]
        if parent_id is not None and parent_id != f["parent_id"]:
            target = get_folder_row(conn, parent_id)
            require_folder_role(conn, user, parent_id, "editor")
            if target["owner_id"] != f["owner_id"]:
                raise InvalidMove("所有者が異なるフォルダへは移動できません")
            if is_descendant(conn, parent_id, folder_id):
                raise InvalidMove("フォルダを自分自身の中へは移動できません")
            new_parent = parent_id
        new_name = clean_name(name, what="フォルダ名") if name is not None else f["name"]
        if _name_taken(conn, new_parent, new_name, exclude_id=folder_id):
            raise NameConflict(f"移動先に「{new_name}」という名前のフォルダが既にあります")
        conn.execute("UPDATE folders SET name = ?, parent_id = ?, updated_at = ? WHERE id = ?",
                     (new_name, new_parent, now(), folder_id))


def subtree_ids(conn: sqlite3.Connection, folder_id: int, *, deleted_at: str | None = None) -> list[int]:
    """folder_id とその配下のフォルダ ID。deleted_at を指定するとその値で削除されたものだけをたどる。"""
    if deleted_at is None:
        cond = "c.deleted_at IS NULL"
        args: tuple = (folder_id,)
    else:
        cond = "c.deleted_at = ?"
        args = (folder_id, deleted_at)
    rows = conn.execute(
        f"""
        WITH RECURSIVE sub(id) AS (
          SELECT ?
          UNION ALL
          SELECT c.id FROM folders c JOIN sub s ON c.parent_id = s.id WHERE {cond}
        )
        SELECT id FROM sub
        """,
        args,
    ).fetchall()
    return [r[0] for r in rows]


def delete_folder(conn: sqlite3.Connection, user: User, folder_id: int) -> None:
    """フォルダと配下をゴミ箱へ（同じ deleted_at を付けて一括で復元できるようにする）。"""
    with Tx(conn):
        f = get_folder_row(conn, folder_id)
        if f["parent_id"] is None:
            raise InvalidMove("ルートフォルダは削除できません")
        require_folder_role(conn, user, folder_id, "editor")
        ids = subtree_ids(conn, folder_id)
        t = now()
        q = ",".join("?" * len(ids))
        conn.execute(f"UPDATE folders SET deleted_at = ? WHERE id IN ({q})", (t, *ids))
        conn.execute(
            f"UPDATE documents SET deleted_at = ?, deleted_by = ? WHERE folder_id IN ({q}) AND deleted_at IS NULL",
            (t, user.id, *ids),
        )


# ---------------------------------------------------------------------------
# メンバー（ユーザー間のフォルダ共有）
# ---------------------------------------------------------------------------

def list_members(conn: sqlite3.Connection, user: User, folder_id: int) -> list[dict]:
    """このフォルダに対して権限を持つ人（所有者・このフォルダのメンバー・上位フォルダから継承したメンバー）。"""
    require_folder_role(conn, user, folder_id, "viewer")
    f = get_folder_row(conn, folder_id)
    owner = conn.execute("SELECT id, username, display_name FROM users WHERE id = ?", (f["owner_id"],)).fetchone()
    out = [{"user_id": owner["id"], "username": owner["username"], "display_name": owner["display_name"],
            "role": "owner", "inherited_from": None}]
    crumbs = path(conn, folder_id)
    names = {c.id: (c.name or "マイファイル") for c in crumbs}
    rows = conn.execute(
        f"SELECT m.folder_id, m.role, u.id, u.username, u.display_name, u.is_active FROM folder_members m"
        f" JOIN users u ON u.id = m.user_id WHERE m.folder_id IN ({','.join('?' * len(crumbs))})"
        f" ORDER BY u.display_name", [c.id for c in crumbs]).fetchall()
    best: dict[int, dict] = {}
    for r in rows:
        cur = best.get(r["id"])
        if cur is None or ROLE_RANK[r["role"]] > ROLE_RANK[cur["role"]] or \
                (r["role"] == cur["role"] and r["folder_id"] == folder_id):
            best[r["id"]] = {"user_id": r["id"], "username": r["username"], "display_name": r["display_name"],
                             "role": r["role"], "active": bool(r["is_active"]),
                             "inherited_from": None if r["folder_id"] == folder_id else names[r["folder_id"]]}
    return out + sorted(best.values(), key=lambda x: x["display_name"])


def set_member(conn: sqlite3.Connection, user: User, folder_id: int, member_id: int, role: str) -> None:
    if role not in ("viewer", "editor"):
        raise ValidationFailed("権限は viewer か editor です")
    with Tx(conn):
        require_folder_role(conn, user, folder_id, "owner")
        f = get_folder_row(conn, folder_id)
        target = conn.execute("SELECT id, is_active FROM users WHERE id = ?", (member_id,)).fetchone()
        if target is None or not target["is_active"]:
            raise NotFound("ユーザーが見つかりません")
        if member_id == f["owner_id"]:
            raise ValidationFailed("所有者はメンバーに追加できません")
        conn.execute(
            "INSERT INTO folder_members(folder_id, user_id, role, granted_by, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(folder_id, user_id) DO UPDATE SET role = excluded.role, granted_by = excluded.granted_by",
            (folder_id, member_id, role, user.id, now()))
        from .audit import audit
        audit(conn, "member_grant", user_id=user.id, target_type="folder", target_id=folder_id,
              member=member_id, role=role)


def remove_member(conn: sqlite3.Connection, user: User, folder_id: int, member_id: int) -> None:
    with Tx(conn):
        require_folder_role(conn, user, folder_id, "owner")
        cur = conn.execute("DELETE FROM folder_members WHERE folder_id = ? AND user_id = ?", (folder_id, member_id))
        if cur.rowcount == 0:
            raise NotFound("このフォルダのメンバーではありません"
                           "（上位のフォルダで共有されている場合は、そちらで削除してください）")
        from .audit import audit
        audit(conn, "member_revoke", user_id=user.id, target_type="folder", target_id=folder_id, member=member_id)
