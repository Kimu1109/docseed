"""フォルダの ZIP 一括ダウンロード。api.md 3.2 / 3.8、security.md 4.3。"""

from __future__ import annotations

import secrets
import sqlite3
import zipfile
from pathlib import Path

from . import folders as fo
from .errors import FileTooLarge, ValidationFailed
from .storage import BlobStore

MAX_ARCHIVE_BYTES = 2 * 1024 ** 3


def folder_entries(conn: sqlite3.Connection, folder_id: int) -> list[tuple[str, sqlite3.Row]]:
    """(ZIP 内のパス, 文書) の一覧。配下のフォルダ構造を保つ。"""
    ids = fo.subtree_ids(conn, folder_id)
    names = {}
    for fid in ids:
        crumbs = fo.path(conn, fid)
        idx = next(i for i, c in enumerate(crumbs) if c.id == folder_id)
        names[fid] = "/".join(c.name for c in crumbs[idx + 1:])
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT d.id, d.filename, d.folder_id, b.size_bytes, b.storage_path FROM documents d"
        f" JOIN blobs b ON b.id = d.blob_id WHERE d.folder_id IN ({q}) AND d.deleted_at IS NULL"
        f" ORDER BY d.folder_id, d.filename", ids).fetchall()
    return [((names[r["folder_id"]] + "/" if names[r["folder_id"]] else "") + r["filename"], r) for r in rows]


def build_zip(conn: sqlite3.Connection, store: BlobStore, folder_id: int) -> Path:
    """一時ファイルに ZIP を作ってパスを返す（呼び出し側が送信後に削除する）。"""
    entries = folder_entries(conn, folder_id)
    if not entries:
        raise ValidationFailed("このフォルダにはファイルがありません")
    total = sum(r["size_bytes"] for _, r in entries)
    if total > MAX_ARCHIVE_BYTES:
        raise FileTooLarge("2GB を超えるため ZIP にできません。フォルダを分けてダウンロードしてください")
    store.tmp_dir.mkdir(parents=True, exist_ok=True)
    out = store.tmp_dir / f"zip-{secrets.token_hex(8)}.zip"
    try:
        # 写真や PDF は既に圧縮されていることが多いので、Pi の負荷を抑えるため無圧縮で格納する
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for name, r in entries:
                src = store.path(r["storage_path"])
                if src.exists():
                    z.write(src, arcname=name)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return out
