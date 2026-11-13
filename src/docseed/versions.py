"""文書の過去の版（docs/design/data-model.md 3.3、api.md 3.3）。

同じ名前での上書き（on_conflict=replace）・文書の画面からの「新しい版をアップロード」・Web クリップの再取得で、
それまでの中身を document_versions に残す。版の中身（blob）は、版がある間は gc_blobs で消さない。
"""

from __future__ import annotations

import sqlite3
from typing import BinaryIO

from . import documents as dm
from .audit import audit
from .db import Tx
from .errors import NotFound, ValidationFailed
from .storage import BlobStore
from .users import User


def list_versions(conn: sqlite3.Connection, user: User, doc_id: int) -> list[sqlite3.Row]:
    """過去の版を新しい順に返す（閲覧できる人なら見られる）。"""
    dm.get_document(conn, user, doc_id, "viewer")
    return conn.execute(
        "SELECT v.*, b.size_bytes, b.mime_type, b.sha256, b.storage_path, b.status,"
        " cu.display_name AS created_by_name, au.display_name AS archived_by_name"
        " FROM document_versions v JOIN blobs b ON b.id = v.blob_id"
        " LEFT JOIN users cu ON cu.id = v.created_by LEFT JOIN users au ON au.id = v.archived_by"
        " WHERE v.document_id = ? ORDER BY v.id DESC", (doc_id,)).fetchall()


def get_version(conn: sqlite3.Connection, user: User, doc_id: int, version_id: int,
                role: str = "viewer") -> sqlite3.Row:
    dm.get_document(conn, user, doc_id, role)
    r = conn.execute(
        "SELECT v.*, b.size_bytes, b.mime_type, b.sha256, b.storage_path FROM document_versions v"
        " JOIN blobs b ON b.id = v.blob_id WHERE v.id = ? AND v.document_id = ?", (version_id, doc_id)).fetchone()
    if r is None:
        raise NotFound("その版は見つかりません")
    return r


def upload_new_version(conn: sqlite3.Connection, store: BlobStore, user: User, doc_id: int, filename: str,
                       src: BinaryIO, *, max_bytes: int, keep_versions: int = dm.DEFAULT_KEEP_VERSIONS,
                       ip: str | None = None) -> bool:
    """文書の中身を新しいファイルにする（名前・フォルダ・タグはそのまま）。中身が今と同じなら False。"""
    first = dm.get_document(conn, user, doc_id, "editor")
    if first["kind"] == "webclip":
        raise ValidationFailed("Web クリップは「再取得」で新しい版にしてください")
    from . import limits
    owner_id = first["owner_id"]
    limits.check(conn, owner_id, 0, owner_is_actor=owner_id == user.id)
    store.check_free_space()
    stored = store.receive(src, filename or "無題", max_bytes)  # 受信はトランザクションの外で行う
    try:
        with Tx(conn):
            d = dm.get_document(conn, user, doc_id, "editor")
            limits.check(conn, owner_id, stored.size, owner_is_actor=owner_id == user.id)
            blob_id, dedup = dm._register_blob(conn, store, stored)
            changed = dm.replace_blob(conn, doc_id, blob_id, user.id, keep_versions)
            audit(conn, "upload", user_id=user.id, target_type="document", target_id=doc_id, ip=ip,
                  filename=d["filename"], size=stored.size, deduplicated=dedup, new_version=True)
    except BaseException:
        store.discard(stored)
        raise
    dm.gc_blobs(conn, store)
    return changed


def restore_version(conn: sqlite3.Connection, store: BlobStore, user: User, doc_id: int, version_id: int,
                    *, keep_versions: int = dm.DEFAULT_KEEP_VERSIONS, ip: str | None = None) -> None:
    """過去の版を今の版に戻す。今の版は過去の版として残す（戻したことも取り消せる）。"""
    with Tx(conn):
        d = dm.get_document(conn, user, doc_id, "editor")
        v = get_version(conn, user, doc_id, version_id, "editor")
        if v["blob_id"] == d["blob_id"]:
            raise ValidationFailed("その版は今の版と同じ内容です")
        conn.execute("DELETE FROM document_versions WHERE id = ?", (version_id,))
        # 戻した版の分の枠が空くので、今の版を残しても件数は増えない
        dm.replace_blob(conn, doc_id, v["blob_id"], user.id, keep_versions)
        audit(conn, "restore_version", user_id=user.id, target_type="document", target_id=doc_id, ip=ip,
              version_id=version_id, version_created_at=v["created_at"])
    dm.gc_blobs(conn, store)


def delete_version(conn: sqlite3.Connection, store: BlobStore, user: User, doc_id: int, version_id: int,
                   ip: str | None = None) -> None:
    """過去の版を完全に削除する（ゴミ箱には入らない）。"""
    with Tx(conn):
        v = get_version(conn, user, doc_id, version_id, "editor")
        conn.execute("DELETE FROM document_versions WHERE id = ?", (version_id,))
        audit(conn, "delete_version", user_id=user.id, target_type="document", target_id=doc_id, ip=ip,
              version_id=version_id, version_created_at=v["created_at"])
    dm.gc_blobs(conn, store)


def version_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "filename": r["filename"], "size_bytes": r["size_bytes"], "mime_type": r["mime_type"],
            "sha256": r["sha256"], "created_by": r["created_by"], "created_at": r["created_at"],
            "archived_by": r["archived_by"], "archived_at": r["archived_at"]}
