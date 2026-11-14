"""分割・再開できるアップロード（tus 1.0 の core・creation・creation-with-upload・termination・expiration）。

docs/design/api.md 3.3.2。通信が不安定なとき（スマートフォンの回線・Cloudflare Tunnel の 100MB の上限など）でも、
切れたところから送り直せるようにする。

- 受け取り中のデータは tmp_dir/tus-<id>。受け取った大きさ（Upload-Offset）は、そのファイルの大きさそのもの
  （途中で切れた PATCH も、届いた分は書き込んで残す）
- 最後まで届いたら documents.upload に渡して文書にする（コピーせず rename する）。結果は行に残し、
  最後の応答を受け取れなかった利用者が GET で受け取れるようにする
- 同じアップロードへの書き込みは 1 つずつ（api は 1 プロセスなので、プロセスの中の lock で足りる）
- 最後の書き込みから EXPIRE_HOURS 時間たったものは消す（worker の daily と、新しく作るとき）
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from . import folders as fo
from . import limits
from .db import Tx, now
from .errors import AppError, FileTooLarge, NotFound, ValidationFailed
from .storage import BlobStore
from .users import User

TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,creation-with-upload,termination,expiration"
EXPIRE_HOURS = 24
MAX_PENDING_PER_USER = 200    # 受け取り中のまま置いておける数（フォルダごとのアップロードでも足りる程度）
ON_CONFLICT = ("rename", "replace", "error")


class OffsetMismatch(AppError):
    status, code, title = 409, "offset_mismatch", "送られてきた位置が、受け取り済みの大きさと違います"


class UploadBusy(AppError):
    status, code, title = 423, "upload_busy", "このアップロードは別の接続で受け取り中です"


class UploadGone(AppError):
    status, code, title = 410, "upload_gone", "このアップロードは期限切れか、取り消されています"


@dataclass
class Upload:
    id: str
    user_id: int
    folder_id: int
    filename: str
    relative_path: str | None
    on_conflict: str
    length: int
    offset: int
    result: dict | None
    expires_at: str


def data_path(store: BlobStore, upload_id: str) -> Path:
    return store.tmp_dir / f"tus-{upload_id}"


def _expires(hours: int = EXPIRE_HOURS) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_date(iso: str) -> str:
    """Upload-Expires（RFC 9110 の日時）。"""
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    return t.strftime("%a, %d %b %Y %H:%M:%S GMT")


def parse_metadata(header: str | None) -> dict[str, str]:
    """Upload-Metadata: 「key base64値,key2 base64値」。値の無いキーは ""。"""
    out: dict[str, str] = {}
    for part in (header or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, val = part.partition(" ")
        try:
            out[key] = base64.b64decode(val.strip(), validate=True).decode("utf-8") if val.strip() else ""
        except (binascii.Error, UnicodeDecodeError):
            raise ValidationFailed(f"Upload-Metadata の {key} を読めません（base64 の UTF-8 にしてください）") from None
    return out


# ---------------------------------------------------------------------------
# 作成・取得・取り消し
# ---------------------------------------------------------------------------

def create(conn: sqlite3.Connection, store: BlobStore, user: User, *, length: int, meta: dict[str, str],
           max_bytes: int) -> Upload:
    """受け取りを始める。送り始める前に、権限・大きさ・容量の上限を確かめる（遅い回線で送り終えてから断らない）。"""
    if length < 0:
        raise ValidationFailed("Upload-Length は 0 以上にしてください")
    if length > max_bytes:
        raise FileTooLarge(f"1 ファイルの上限（{max_bytes // 1024 // 1024} MB）を超えています")
    try:
        folder_id = int(meta.get("folder_id", ""))
    except ValueError:
        raise ValidationFailed("Upload-Metadata に folder_id（保存先のフォルダ）が必要です") from None
    filename = meta.get("filename") or meta.get("name") or ""
    if not filename:
        raise ValidationFailed("Upload-Metadata に filename が必要です")
    on_conflict = meta.get("on_conflict") or "rename"
    if on_conflict not in ON_CONFLICT:
        raise ValidationFailed("on_conflict は rename / replace / error のいずれかです")
    relative_path = meta.get("relative_path") or None
    fo.require_folder_role(conn, user, folder_id, "editor")
    fo.clean_name(filename.replace("\\", "/").rsplit("/", 1)[-1], what="ファイル名")
    owner_id = limits.owner_of_folder(conn, folder_id)
    limits.check(conn, owner_id, length, owner_is_actor=owner_id == user.id)
    store.check_free_space()
    expire_stale(conn, store)
    n = conn.execute("SELECT count(*) FROM resumable_uploads WHERE user_id = ? AND result IS NULL",
                     (user.id,)).fetchone()[0]
    if n >= MAX_PENDING_PER_USER:
        raise ValidationFailed(f"受け取り中のアップロードが多すぎます（{MAX_PENDING_PER_USER} 件まで）。"
                               "しばらくしてから再度お試しください")
    upload_id = secrets.token_urlsafe(18)
    store.tmp_dir.mkdir(parents=True, exist_ok=True)
    data_path(store, upload_id).touch()
    t = now()
    exp = _expires()
    with Tx(conn):
        conn.execute("INSERT INTO resumable_uploads(id, user_id, folder_id, filename, relative_path, on_conflict,"
                     " length, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (upload_id, user.id, folder_id, filename, relative_path, on_conflict, length, t, t, exp))
    return Upload(upload_id, user.id, folder_id, filename, relative_path, on_conflict, length, 0, None, exp)


def get(conn: sqlite3.Connection, store: BlobStore, user: User, upload_id: str) -> Upload:
    r = conn.execute("SELECT * FROM resumable_uploads WHERE id = ?", (upload_id,)).fetchone()
    if r is None or r["user_id"] != user.id:     # 他の人のものは存在も明かさない
        raise NotFound("アップロードが見つかりません")
    if r["expires_at"] < now():
        raise UploadGone()
    result = json.loads(r["result"]) if r["result"] else None
    p = data_path(store, upload_id)
    if result is not None:
        offset = r["length"]
    elif p.exists():
        offset = p.stat().st_size
    else:
        raise UploadGone()
    return Upload(r["id"], r["user_id"], r["folder_id"], r["filename"], r["relative_path"], r["on_conflict"],
                  r["length"], offset, result, r["expires_at"])


def terminate(conn: sqlite3.Connection, store: BlobStore, user: User, upload_id: str) -> None:
    get(conn, store, user, upload_id)
    with lock(upload_id):
        with Tx(conn):
            conn.execute("DELETE FROM resumable_uploads WHERE id = ?", (upload_id,))
        data_path(store, upload_id).unlink(missing_ok=True)


def touch(conn: sqlite3.Connection, upload_id: str) -> str:
    """書き込んだので期限を延ばす。新しい期限を返す。"""
    exp = _expires()
    with Tx(conn):
        conn.execute("UPDATE resumable_uploads SET updated_at = ?, expires_at = ? WHERE id = ?",
                     (now(), exp, upload_id))
    return exp


def save_result(conn: sqlite3.Connection, upload_id: str, result: dict) -> None:
    with Tx(conn):
        conn.execute("UPDATE resumable_uploads SET result = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(result, ensure_ascii=False), now(), upload_id))


def expire_stale(conn: sqlite3.Connection, store: BlobStore) -> int:
    """期限を過ぎたものと、行の無い受け取り中のファイル（途中で落ちたときの残り）を消す。消した数を返す。"""
    t = now()
    ids = []
    for (upload_id,) in conn.execute("SELECT id FROM resumable_uploads WHERE expires_at < ?", (t,)).fetchall():
        try:
            with lock(upload_id):
                data_path(store, upload_id).unlink(missing_ok=True)
        except UploadBusy:
            continue          # まだ書き込み中（届かなくなった接続を待っている）。次の機会に消す
        ids.append(upload_id)
    if ids:
        with Tx(conn):
            conn.executemany("DELETE FROM resumable_uploads WHERE id = ?", [(i,) for i in ids])
    if store.tmp_dir.exists():
        known = {r[0] for r in conn.execute("SELECT id FROM resumable_uploads")}
        cutoff = dt.datetime.now().timestamp() - EXPIRE_HOURS * 3600
        for p in store.tmp_dir.glob("tus-*"):
            try:
                if p.name[4:] not in known and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    ids.append(p.name[4:])
            except OSError:
                pass
    return len(ids)


# ---------------------------------------------------------------------------
# 書き込みの排他
# ---------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class lock:
    """アップロードごとの排他。取れなければ（別の接続が書き込み中なら）UploadBusy。"""

    def __init__(self, upload_id: str):
        self.upload_id = upload_id

    def __enter__(self):
        with _locks_guard:   # 取得と後片付けを同じ lock の中で行い、古い Lock を 2 人が掴まないようにする
            lk = _locks.setdefault(self.upload_id, threading.Lock())
            if not lk.acquire(blocking=False):
                raise UploadBusy("このアップロードは別の接続で受け取り中です。少し待ってから再開してください")
        self.lk = lk
        return self

    def __exit__(self, *exc):
        with _locks_guard:
            self.lk.release()
            if _locks.get(self.upload_id) is self.lk:
                del _locks[self.upload_id]
