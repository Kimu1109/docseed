"""文書（ユーザーから見えるファイル）とゴミ箱。docs/design/data-model.md 3.3 / 4 章、api.md 3.3 / 3.4。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import BinaryIO

from . import folders as fo
from .audit import audit
from .db import Tx, now
from .errors import InvalidMove, NameConflict, NotFound, ValidationFailed
from .storage import BlobStore, StoredFile
from .users import User

ON_CONFLICT = ("rename", "replace", "error")
DEFAULT_KEEP_VERSIONS = 10   # 1 文書あたりの過去の版の数（DOCSEED_VERSIONS_KEEP。0 なら版を残さない）


# ---------------------------------------------------------------------------
# 参照
# ---------------------------------------------------------------------------

_DOC_COLUMNS = (
    "d.*, b.size_bytes, b.mime_type, b.sha256, b.storage_path, b.status, b.page_count,"
    " b.progress_done, b.progress_total, b.error, b.warnings, b.thumb_path, b.view_path, b.image_meta, f.owner_id,"
    " u.display_name AS created_by_name"
)
_DOC_FROM = (
    " FROM documents d JOIN blobs b ON b.id = d.blob_id JOIN folders f ON f.id = d.folder_id"
    " JOIN users u ON u.id = d.created_by"
)


def get_document(conn: sqlite3.Connection, user: User, doc_id: int, role: str = "viewer") -> sqlite3.Row:
    r = conn.execute("SELECT " + _DOC_COLUMNS + _DOC_FROM + " WHERE d.id = ? AND d.deleted_at IS NULL",
                     (doc_id,)).fetchone()
    if r is None:
        raise NotFound("ファイルが見つかりません")
    try:
        fo.require_folder_role(conn, user, r["folder_id"], role)
    except NotFound:
        raise NotFound("ファイルが見つかりません") from None
    return r


def document_tags(conn: sqlite3.Connection, doc_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.document_id = ?"
        " ORDER BY t.name", (doc_id,))]


# ---------------------------------------------------------------------------
# 名前
# ---------------------------------------------------------------------------

def _doc_name_taken(conn: sqlite3.Connection, folder_id: int, name: str, exclude_id: int | None = None) -> bool:
    return conn.execute(
        "SELECT 1 FROM documents WHERE folder_id = ? AND filename = ? COLLATE NOCASE AND deleted_at IS NULL"
        " AND id IS NOT ?", (folder_id, name, exclude_id)
    ).fetchone() is not None


def free_doc_name(conn: sqlite3.Connection, folder_id: int, name: str) -> str:
    """「見積書.pdf」が使われていれば「見積書 (2).pdf」のように空いている名前を返す。"""
    if not _doc_name_taken(conn, folder_id, name):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n = 2
    while True:
        cand = f"{stem} ({n})" + (f".{ext}" if ext else "")
        if not _doc_name_taken(conn, folder_id, cand):
            return cand
        n += 1


# ---------------------------------------------------------------------------
# アップロード
# ---------------------------------------------------------------------------

@dataclass
class UploadResult:
    document_id: int
    filename: str
    deduplicated: bool
    replaced: bool


def _register_blob(conn: sqlite3.Connection, store: BlobStore, f: StoredFile) -> tuple[int, bool]:
    """blob を登録して (blob_id, 既存の blob を使ったか) を返す。新規なら処理ジョブも積む。"""
    r = conn.execute("SELECT id FROM blobs WHERE sha256 = ?", (f.sha256,)).fetchone()
    if r is not None:
        store.discard(f)
        return int(r[0]), True
    t = now()
    store.commit(f)
    cur = conn.execute(
        "INSERT INTO blobs(sha256, size_bytes, mime_type, storage_path, status, created_at, updated_at)"
        " VALUES (?,?,?,?, 'pending', ?, ?)",
        (f.sha256, f.size, f.mime_type, f.rel_path, t, t),
    )
    blob_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO jobs(type, blob_id, priority, status, run_after, created_at)"
        " VALUES ('process_blob', ?, 0, 'pending', ?, ?)",
        (blob_id, t, t),
    )
    return blob_id, False


def upload(conn: sqlite3.Connection, store: BlobStore, user: User, folder_id: int, filename: str,
           src: BinaryIO, *, max_bytes: int, on_conflict: str = "rename", relative_path: str | None = None,
           ip: str | None = None, via_share_id: int | None = None, mime_type: str | None = None,
           kind: str = "file", keep_versions: int = DEFAULT_KEEP_VERSIONS) -> UploadResult:
    """ファイルを保存する。mime_type は判定を上書きする（Web クリップの HTML）。kind は documents.kind。"""
    if on_conflict not in ON_CONFLICT:
        raise ValidationFailed("on_conflict は rename / replace / error のいずれかです")
    fo.require_folder_role(conn, user, folder_id, "editor")
    # relative_path（フォルダごとのアップロード）: "2026/10/見積書.pdf" → 2026/10 を作る
    parts: list[str] = []
    if relative_path:
        parts = [p for p in relative_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if parts:
            filename = parts.pop()
    name = fo.clean_name(filename, what="ファイル名")
    from . import limits
    owner_id = limits.owner_of_folder(conn, folder_id)
    limits.check(conn, owner_id, 0, owner_is_actor=owner_id == user.id)   # もう上限なら受信の前に断る
    store.check_free_space()
    stored = store.receive(src, name, max_bytes)  # 時間のかかる受信はトランザクションの外で行う
    if mime_type:
        stored.mime_type = mime_type
    try:
        with Tx(conn):
            limits.check(conn, owner_id, stored.size, owner_is_actor=owner_id == user.id)
            target = fo.ensure_path(conn, user, folder_id, parts) if parts else folder_id
            blob_id, dedup = _register_blob(conn, store, stored)
            t = now()
            existing = conn.execute(
                "SELECT id FROM documents WHERE folder_id = ? AND filename = ? COLLATE NOCASE"
                " AND deleted_at IS NULL", (target, name)
            ).fetchone()
            replaced = False
            if existing is not None:
                if on_conflict == "error":
                    raise NameConflict(f"「{name}」は既にこのフォルダにあります")
                if on_conflict == "replace":
                    doc_id, replaced = int(existing[0]), True
                    replace_blob(conn, doc_id, blob_id, user.id, keep_versions)
                else:
                    name = free_doc_name(conn, target, name)
            if not replaced:
                cur = conn.execute(
                    "INSERT INTO documents(folder_id, blob_id, filename, created_by, created_at, updated_at,"
                    " uploaded_via_share_id, kind) VALUES (?,?,?,?,?,?,?,?)",
                    (target, blob_id, name, user.id, t, t, via_share_id, kind),
                )
                doc_id = int(cur.lastrowid)
            audit(conn, "upload", user_id=user.id, target_type="document", target_id=doc_id, ip=ip,
                  filename=name, size=stored.size, deduplicated=dedup)
    except BaseException:
        store.discard(stored)
        raise
    if replaced:
        gc_blobs(conn, store)
    return UploadResult(doc_id, name, dedup, replaced)


def replace_blob(conn: sqlite3.Connection, doc_id: int, blob_id: int, user_id: int | None,
                 keep_versions: int = DEFAULT_KEEP_VERSIONS) -> bool:
    """文書の中身を差し替え、それまでの中身を過去の版として残す。呼び出し側のトランザクションの中で使う。

    中身が同じ（同じ blob）なら版は増やさない。差し替えたら True。古い blob の削除は呼び出し側が gc_blobs で行う。
    """
    d = conn.execute("SELECT blob_id, filename, created_by, created_at, content_updated_by, content_updated_at"
                     " FROM documents WHERE id = ?", (doc_id,)).fetchone()
    t = now()
    if d["blob_id"] == blob_id:
        conn.execute("UPDATE documents SET updated_at = ? WHERE id = ?", (t, doc_id))
        return False
    if keep_versions > 0:
        conn.execute(
            "INSERT INTO document_versions(document_id, blob_id, filename, created_by, created_at, archived_by,"
            " archived_at) VALUES (?,?,?,?,?,?,?)",
            (doc_id, d["blob_id"], d["filename"], d["content_updated_by"] or d["created_by"],
             d["content_updated_at"] or d["created_at"], user_id, t))
    conn.execute("UPDATE documents SET blob_id = ?, updated_at = ?, content_updated_at = ?, content_updated_by = ?"
                 " WHERE id = ?", (blob_id, t, t, user_id, doc_id))
    prune_versions(conn, doc_id, keep_versions)
    return True


def prune_versions(conn: sqlite3.Connection, doc_id: int | None, keep: int) -> int:
    """過去の版を新しい順に keep 件だけ残す。doc_id が None ならすべての文書。消した件数を返す。"""
    where, args = ("WHERE document_id = ?", [doc_id]) if doc_id is not None else ("", [])
    cur = conn.execute(
        "DELETE FROM document_versions WHERE id IN (SELECT id FROM (SELECT id, row_number() OVER"
        f" (PARTITION BY document_id ORDER BY id DESC) AS n FROM document_versions {where}) WHERE n > ?)",
        (*args, max(keep, 0)))
    return cur.rowcount


# ---------------------------------------------------------------------------
# 変更
# ---------------------------------------------------------------------------

def update_document(conn: sqlite3.Connection, user: User, doc_id: int, *, filename: str | None = None,
                    folder_id: int | None = None, tags: list[str] | None = None,
                    clear_suggestions: bool = True) -> None:
    """名前・フォルダ・タグを変える。移動やタグの手での変更では、古い整理の候補（open）を消す。"""
    with Tx(conn):
        d = get_document(conn, user, doc_id, "editor")
        target = d["folder_id"]
        if folder_id is not None and folder_id != d["folder_id"]:
            dest = fo.get_folder_row(conn, folder_id)
            fo.require_folder_role(conn, user, folder_id, "editor")
            if dest["owner_id"] != d["owner_id"]:
                raise InvalidMove("所有者が異なるフォルダへは移動できません（コピーしてください）")
            target = folder_id
        name = fo.clean_name(filename, what="ファイル名") if filename is not None else d["filename"]
        if _doc_name_taken(conn, target, name, exclude_id=doc_id):
            where = "移動先の" if target != d["folder_id"] else "この"
            raise NameConflict(f"「{name}」は既に{where}フォルダにあります")
        conn.execute("UPDATE documents SET filename = ?, folder_id = ?, updated_at = ? WHERE id = ?",
                     (name, target, now(), doc_id))
        if clear_suggestions and target != d["folder_id"]:
            conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open'"
                         " AND kind = 'folder'", (doc_id,))
        new_tags = sorted({t.strip().lower() for t in tags if t.strip()}) if tags is not None else None
        if clear_suggestions and new_tags is not None and \
                new_tags != sorted(x.lower() for x in document_tags(conn, doc_id)):
            conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open'"
                         " AND kind = 'tag'", (doc_id,))
        if tags is not None:
            names = sorted({t.strip() for t in tags if t.strip()})
            if any(len(t) > 50 for t in names):
                raise ValidationFailed("タグは 50 文字以内にしてください")
            conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
            for n in names:
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (n,))
                tid = conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()[0]
                conn.execute("INSERT INTO document_tags VALUES (?, ?)", (doc_id, tid))


def copy_document(conn: sqlite3.Connection, user: User, doc_id: int, folder_id: int,
                  filename: str | None = None) -> int:
    with Tx(conn):
        d = get_document(conn, user, doc_id, "viewer")
        fo.require_folder_role(conn, user, folder_id, "editor")
        from . import limits
        owner_id = limits.owner_of_folder(conn, folder_id)
        limits.check(conn, owner_id, d["size_bytes"], owner_is_actor=owner_id == user.id)
        name = free_doc_name(conn, folder_id, fo.clean_name(filename or d["filename"], what="ファイル名"))
        t = now()
        cur = conn.execute(
            "INSERT INTO documents(folder_id, blob_id, filename, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (folder_id, d["blob_id"], name, user.id, t, t),
        )
        return int(cur.lastrowid)


def delete_document(conn: sqlite3.Connection, user: User, doc_id: int, ip: str | None = None) -> None:
    with Tx(conn):
        get_document(conn, user, doc_id, "editor")
        conn.execute("UPDATE documents SET deleted_at = ?, deleted_by = ? WHERE id = ?", (now(), user.id, doc_id))
        audit(conn, "delete", user_id=user.id, target_type="document", target_id=doc_id, ip=ip)


# ---------------------------------------------------------------------------
# ゴミ箱
# ---------------------------------------------------------------------------

def list_trash(conn: sqlite3.Connection, user: User) -> list[dict]:
    """自分が所有するフォルダ配下で削除された項目を、削除した単位で返す。"""
    items: list[dict] = []
    for r in conn.execute(
        "SELECT f.id, f.name, f.deleted_at, f.parent_id FROM folders f"
        " LEFT JOIN folders p ON p.id = f.parent_id"
        " WHERE f.owner_id = ? AND f.deleted_at IS NOT NULL"
        " AND (p.deleted_at IS NULL OR p.deleted_at <> f.deleted_at)",
        (user.id,),
    ):
        ids = fo.subtree_ids(conn, r["id"], deleted_at=r["deleted_at"])
        n = conn.execute(
            f"SELECT count(*) FROM documents WHERE deleted_at = ? AND folder_id IN ({','.join('?' * len(ids))})",
            (r["deleted_at"], *ids),
        ).fetchone()[0]
        items.append({"type": "folder", "id": r["id"], "name": r["name"], "deleted_at": r["deleted_at"],
                      "location": location_of(conn, r["parent_id"]), "files": n, "size": None})
    for r in conn.execute(
        "SELECT d.id, d.filename, d.deleted_at, d.folder_id, b.size_bytes, b.mime_type, u.display_name"
        " FROM documents d JOIN folders f ON f.id = d.folder_id JOIN blobs b ON b.id = d.blob_id"
        " LEFT JOIN users u ON u.id = d.deleted_by"
        " WHERE f.owner_id = ? AND d.deleted_at IS NOT NULL"
        " AND (f.deleted_at IS NULL OR f.deleted_at <> d.deleted_at)",
        (user.id,),
    ):
        items.append({"type": "document", "id": r["id"], "name": r["filename"], "deleted_at": r["deleted_at"],
                      "location": location_of(conn, r["folder_id"]), "size": r["size_bytes"],
                      "mime_type": r["mime_type"], "deleted_by": r["display_name"]})
    return sorted(items, key=lambda x: x["deleted_at"], reverse=True)


def get_trashed_document(conn: sqlite3.Connection, user: User, doc_id: int) -> sqlite3.Row:
    """ゴミ箱の中の文書（プレビュー用）。ゴミ箱を見られるのはフォルダの持ち主だけなので、持ち主に限る。

    文書単位で削除したものも、削除したフォルダの中にあるものも返す。"""
    r = conn.execute(
        "SELECT " + _DOC_COLUMNS + ", f.deleted_at AS folder_deleted_at, du.display_name AS deleted_by_name"
        + _DOC_FROM + " LEFT JOIN users du ON du.id = d.deleted_by WHERE d.id = ? AND d.deleted_at IS NOT NULL",
        (doc_id,),
    ).fetchone()
    if r is None or r["owner_id"] != user.id:
        raise NotFound("ゴミ箱に見つかりません")
    return r


def trashed_folder_contents(conn: sqlite3.Connection, user: User, folder_id: int) -> dict:
    """ゴミ箱の中のフォルダと、一緒に削除された配下の文書（フォルダの中の相対パス付き）。"""
    f = _own_trashed_folder(conn, user, folder_id)
    ids = fo.subtree_ids(conn, folder_id, deleted_at=f["deleted_at"])
    q = ",".join("?" * len(ids))
    names = {r["id"]: (r["parent_id"], r["name"]) for r in conn.execute(
        f"SELECT id, parent_id, name FROM folders WHERE id IN ({q})", ids)}

    def rel(fid: int) -> str:
        parts: list[str] = []
        while fid != folder_id and fid in names:
            parent, name = names[fid]
            parts.append(name)
            fid = parent
        return "/".join(reversed(parts))

    docs = [dict(r) | {"path": rel(r["folder_id"])} for r in conn.execute(
        "SELECT d.id, d.filename, d.folder_id, d.kind, b.size_bytes, b.mime_type, b.thumb_path"
        " FROM documents d JOIN blobs b ON b.id = d.blob_id"
        f" WHERE d.deleted_at = ? AND d.folder_id IN ({q})", (f["deleted_at"], *ids))]
    docs.sort(key=lambda x: (x["path"], x["filename"].lower()))
    return {"folder": f, "docs": docs, "folders": len(ids) - 1, "location": location_of(conn, f["parent_id"])}


def location_of(conn: sqlite3.Connection, folder_id: int | None) -> str:
    """「/経理/2026」のような場所の表示（削除済みのフォルダも名前をたどる）。"""
    if folder_id is None:
        return "/"
    return "/" + "/".join(c.name for c in fo.path(conn, folder_id) if c.name)


def _own_trashed_folder(conn: sqlite3.Connection, user: User, folder_id: int) -> sqlite3.Row:
    f = conn.execute("SELECT * FROM folders WHERE id = ? AND deleted_at IS NOT NULL", (folder_id,)).fetchone()
    if f is None or f["owner_id"] != user.id:
        raise NotFound("ゴミ箱に見つかりません")
    return f


def _own_trashed_doc(conn: sqlite3.Connection, user: User, doc_id: int) -> sqlite3.Row:
    d = conn.execute(
        "SELECT d.*, f.owner_id, f.deleted_at AS folder_deleted_at FROM documents d"
        " JOIN folders f ON f.id = d.folder_id WHERE d.id = ? AND d.deleted_at IS NOT NULL", (doc_id,)
    ).fetchone()
    if d is None or d["owner_id"] != user.id:
        raise NotFound("ゴミ箱に見つかりません")
    return d


def _root_of(conn: sqlite3.Connection, owner_id: int) -> int:
    return conn.execute("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS NULL",
                        (owner_id,)).fetchone()[0]


def restore(conn: sqlite3.Connection, user: User, item_type: str, item_id: int) -> None:
    """復元する。元の場所に同名があれば別名にし、元のフォルダも削除済みならルートに戻す。"""
    with Tx(conn):
        if item_type == "document":
            d = _own_trashed_doc(conn, user, item_id)
            folder_id = d["folder_id"] if d["folder_deleted_at"] is None else _root_of(conn, user.id)
            name = free_doc_name(conn, folder_id, d["filename"])
            conn.execute("UPDATE documents SET deleted_at = NULL, deleted_by = NULL, folder_id = ?, filename = ?,"
                         " updated_at = ? WHERE id = ?", (folder_id, name, now(), item_id))
        elif item_type == "folder":
            f = _own_trashed_folder(conn, user, item_id)
            ids = fo.subtree_ids(conn, item_id, deleted_at=f["deleted_at"])
            parent = conn.execute("SELECT deleted_at FROM folders WHERE id = ?", (f["parent_id"],)).fetchone()
            parent_id = f["parent_id"] if parent and parent["deleted_at"] is None else _root_of(conn, user.id)
            name = fo.free_folder_name(conn, parent_id, f["name"])
            q = ",".join("?" * len(ids))
            conn.execute(f"UPDATE documents SET deleted_at = NULL, deleted_by = NULL WHERE deleted_at = ?"
                         f" AND folder_id IN ({q})", (f["deleted_at"], *ids))
            # 先に名前と場所を決めてから復元する（同名の一意制約に当たらないように）
            conn.execute("UPDATE folders SET parent_id = ?, name = ?, updated_at = ? WHERE id = ?",
                         (parent_id, name, now(), item_id))
            conn.execute(f"UPDATE folders SET deleted_at = NULL WHERE id IN ({q})", ids)
        else:
            raise ValidationFailed("種類は folder か document です")


def purge(conn: sqlite3.Connection, store: BlobStore, user: User, item_type: str, item_id: int) -> None:
    """ゴミ箱から完全に削除する。"""
    with Tx(conn):
        if item_type == "document":
            _own_trashed_doc(conn, user, item_id)
            conn.execute("DELETE FROM document_tags WHERE document_id = ?", (item_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (item_id,))
        elif item_type == "folder":
            f = _own_trashed_folder(conn, user, item_id)
            _purge_folder(conn, f["id"])
        else:
            raise ValidationFailed("種類は folder か document です")
        audit(conn, "purge", user_id=user.id, target_type=item_type, target_id=item_id)
    gc_blobs(conn, store)


def _purge_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    # 配下はすべて削除対象（ゴミ箱に入った時点で配下も削除済み）
    ids = fo.subtree_ids(conn, folder_id, deleted_at=None) + fo.subtree_ids(
        conn, folder_id, deleted_at=conn.execute("SELECT deleted_at FROM folders WHERE id = ?",
                                                  (folder_id,)).fetchone()[0])
    ids = list(dict.fromkeys(ids))
    q = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM document_tags WHERE document_id IN (SELECT id FROM documents WHERE folder_id IN ({q}))",
                 ids)
    conn.execute(f"DELETE FROM documents WHERE folder_id IN ({q})", ids)
    conn.execute(f"DELETE FROM folder_members WHERE folder_id IN ({q})", ids)
    conn.execute(f"DELETE FROM shares WHERE target_type = 'folder' AND target_id IN ({q})", ids)
    # 子から順に消す（parent_id の外部キーのため）
    for fid in reversed(ids):
        conn.execute("DELETE FROM folders WHERE id = ?", (fid,))


def empty_trash(conn: sqlite3.Connection, store: BlobStore, user: User) -> int:
    items = list_trash(conn, user)
    with Tx(conn):
        for it in items:
            if it["type"] == "document":
                conn.execute("DELETE FROM document_tags WHERE document_id = ?", (it["id"],))
                conn.execute("DELETE FROM documents WHERE id = ?", (it["id"],))
            else:
                _purge_folder(conn, it["id"])
        audit(conn, "empty_trash", user_id=user.id, count=len(items))
    gc_blobs(conn, store)
    return len(items)


def purge_expired(conn: sqlite3.Connection, store: BlobStore, cutoff_iso: str | dict[int, str]) -> int:
    """保持期間を過ぎた項目を完全に削除する。

    cutoff_iso: この日時より前に削除されたもの。{持ち主のユーザー ID: 日時} なら持ち主ごと（ゴミ箱の保持日数は
    利用者ごとに選べる。limits.retention_cutoffs）。dict に無い持ち主のものは消さない。
    """
    cutoffs = cutoff_iso if isinstance(cutoff_iso, dict) else None
    n = 0
    with Tx(conn):
        for r in conn.execute("SELECT id, owner_id, deleted_at FROM folders WHERE deleted_at IS NOT NULL"
                              " AND parent_id IS NOT NULL ORDER BY id").fetchall():
            cut = cutoffs.get(r["owner_id"]) if cutoffs is not None else cutoff_iso
            if cut is None or r["deleted_at"] >= cut:
                continue
            if conn.execute("SELECT 1 FROM folders WHERE id = ?", (r["id"],)).fetchone():
                _purge_folder(conn, r["id"])
                n += 1
        rows = conn.execute("SELECT d.id, d.deleted_at, f.owner_id FROM documents d"
                            " JOIN folders f ON f.id = d.folder_id WHERE d.deleted_at IS NOT NULL").fetchall()
        for r in rows:
            cut = cutoffs.get(r["owner_id"]) if cutoffs is not None else cutoff_iso
            if cut is not None and r["deleted_at"] < cut:
                conn.execute("DELETE FROM documents WHERE id = ?", (r["id"],))
                n += 1
    gc_blobs(conn, store)
    return n


def gc_blobs(conn: sqlite3.Connection, store: BlobStore) -> int:
    """どの文書からも参照されていない blob を削除する（抽出結果・ベクトルはカスケードで消える）。"""
    with Tx(conn):
        rows = conn.execute(
            "SELECT id, storage_path, thumb_path, view_path FROM blobs b"
            " WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.blob_id = b.id)"
            # Web クリップの og:image（どの文書からも直接は参照されない）は、クリップがある間は残す
            " AND NOT EXISTS (SELECT 1 FROM web_clips w WHERE w.image_blob_id = b.id)"
            # 過去の版の中身は、版がある間は残す
            " AND NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.blob_id = b.id)"
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM blobs WHERE id = ?", (r["id"],))
    for r in rows:  # ファイルは DB の確定後に消す（派生ファイルも）
        store.delete(r["storage_path"])
        store.delete_derived(r["thumb_path"], r["view_path"])
    return len(rows)
