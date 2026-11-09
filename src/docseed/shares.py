"""共有リンク。docs/design/security.md 4 章、data-model.md 3.6。"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from . import folders as fo
from .db import Tx, now
from .errors import Forbidden, NotFound, ValidationFailed
from .security import _ph, verify_password
from .users import User, get_user

PERMISSIONS = ("read", "upload")
UPLOAD_MAX_DAYS = 30   # 受け取り用リンクの期限の上限（無期限にはできない）
PASS_COOKIE_HOURS = 12
INVALID = "リンクが無効か、期限が切れています"


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 作成・変更
# ---------------------------------------------------------------------------

def _target_folder(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    """共有対象の権限を判定するフォルダ（文書ならその文書のフォルダ）。"""
    if target_type == "folder":
        return fo.get_folder_row(conn, target_id)["id"]
    if target_type == "document":
        r = conn.execute("SELECT folder_id FROM documents WHERE id = ? AND deleted_at IS NULL", (target_id,)).fetchone()
        if r is None:
            raise NotFound("ファイルが見つかりません")
        return r[0]
    raise ValidationFailed("共有の対象は folder か document です")


def parse_expiry(value: str | None) -> str | None:
    """'7d' / '30d' / '1d' / 'never' / 'YYYY-MM-DD'（その日の終わりまで）/ ISO 日時 → 期限（UTC）。"""
    if value in (None, "", "never"):
        return None
    t = dt.datetime.now(dt.UTC)
    if value.endswith("d") and value[:-1].isdigit():
        days = int(value[:-1])
        if not 1 <= days <= 366:
            raise ValidationFailed("期限は 1〜366 日で指定してください")
        return _iso(t + dt.timedelta(days=days))
    try:
        if len(value) == 10:
            # 日付だけなら、その日の終わり（日本時間 23:59:59 → UTC）
            from .web.helpers import TZ
            d = dt.datetime.fromisoformat(value).replace(hour=23, minute=59, second=59, tzinfo=TZ)
            exp = d.astimezone(dt.UTC)
        else:
            exp = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        raise ValidationFailed("期限の形式が正しくありません") from None
    if exp <= t:
        raise ValidationFailed("期限には未来の日時を指定してください")
    return _iso(exp)


def _check_upload_expiry(expires_at: str | None) -> None:
    """受け取り用リンクは誰でもファイルを送れるので、無期限や長すぎる期限にはしない。"""
    limit = _iso(dt.datetime.now(dt.UTC) + dt.timedelta(days=UPLOAD_MAX_DAYS, minutes=1))
    if expires_at is None or expires_at > limit:
        raise ValidationFailed(f"受け取り用のリンクの期限は {UPLOAD_MAX_DAYS} 日以内にしてください")


def create_share(conn: sqlite3.Connection, user: User, target_type: str, target_id: int, *,
                 permission: str = "read", expires: str | None = "7d", password: str | None = None,
                 max_downloads: int | None = None) -> sqlite3.Row:
    if permission not in PERMISSIONS:
        raise ValidationFailed("権限は read か upload です")
    if permission == "upload" and target_type != "folder":
        raise ValidationFailed("受け取り用のリンクはフォルダにだけ作れます")
    if max_downloads is not None and not 1 <= max_downloads <= 100000:
        raise ValidationFailed("ダウンロード回数の上限は 1 以上にしてください")
    if password is not None and password != "" and len(password) < 4:
        raise ValidationFailed("リンクのパスワードは 4 文字以上にしてください")
    expires_at = parse_expiry(expires)
    if permission == "upload":
        _check_upload_expiry(expires_at)
    with Tx(conn):
        fo.require_folder_role(conn, user, _target_folder(conn, target_type, target_id), "editor")
        token = secrets.token_urlsafe(32)
        cur = conn.execute(
            "INSERT INTO shares(token, target_type, target_id, permission, password_hash, expires_at, max_downloads,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (token, target_type, target_id, permission, _ph.hash(password) if password else None, expires_at,
             max_downloads, user.id, now()))
        from .audit import audit
        audit(conn, "share_create", user_id=user.id, target_type=target_type, target_id=target_id,
              share_id=cur.lastrowid, permission=permission)
        return conn.execute("SELECT * FROM shares WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_share_for_manage(conn: sqlite3.Connection, user: User, share_id: int) -> sqlite3.Row:
    """作成者、または対象フォルダの所有者だけが変更・無効化できる。"""
    s = conn.execute("SELECT * FROM shares WHERE id = ?", (share_id,)).fetchone()
    if s is None:
        raise NotFound("共有リンクが見つかりません")
    if s["created_by"] != user.id:
        try:
            role = fo.effective_role(conn, user, _target_folder(conn, s["target_type"], s["target_id"]))
        except NotFound:
            role = None
        if role != "owner":
            raise NotFound("共有リンクが見つかりません")
    return s


def revoke_share(conn: sqlite3.Connection, user: User, share_id: int) -> None:
    with Tx(conn):
        s = get_share_for_manage(conn, user, share_id)
        conn.execute("UPDATE shares SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now(), s["id"]))
        from .audit import audit
        audit(conn, "share_revoke", user_id=user.id, target_type=s["target_type"], target_id=s["target_id"],
              share_id=s["id"])


def update_share(conn: sqlite3.Connection, user: User, share_id: int, *, expires: str | None = None,
                 password: str | None = None, clear_password: bool = False,
                 max_downloads: int | None = None, clear_max: bool = False) -> None:
    with Tx(conn):
        s = get_share_for_manage(conn, user, share_id)
        if expires is not None:
            expires_at = parse_expiry(expires)
            if s["permission"] == "upload":
                _check_upload_expiry(expires_at)
            conn.execute("UPDATE shares SET expires_at = ? WHERE id = ?", (expires_at, s["id"]))
        if clear_password:
            conn.execute("UPDATE shares SET password_hash = NULL WHERE id = ?", (s["id"],))
        elif password:
            if len(password) < 4:
                raise ValidationFailed("リンクのパスワードは 4 文字以上にしてください")
            conn.execute("UPDATE shares SET password_hash = ? WHERE id = ?", (_ph.hash(password), s["id"]))
        if clear_max:
            conn.execute("UPDATE shares SET max_downloads = NULL WHERE id = ?", (s["id"],))
        elif max_downloads is not None:
            conn.execute("UPDATE shares SET max_downloads = ? WHERE id = ?", (max_downloads, s["id"]))


def list_shares(conn: sqlite3.Connection, user: User, *, target_type: str | None = None,
                target_id: int | None = None, include_inactive: bool = False) -> list[sqlite3.Row]:
    """自分が作ったリンク（対象を指定した場合は、その対象のリンクで自分が管理できるもの）。"""
    q = ("SELECT s.*, u.display_name AS created_by_name,"
         " CASE s.target_type WHEN 'document' THEN (SELECT filename FROM documents WHERE id = s.target_id)"
         " ELSE (SELECT name FROM folders WHERE id = s.target_id) END AS target_name"
         " FROM shares s JOIN users u ON u.id = s.created_by WHERE 1 = 1")
    args: list = []
    if target_type is not None:
        q += " AND s.target_type = ? AND s.target_id = ?"
        args += [target_type, target_id]
    else:
        q += " AND s.created_by = ?"
        args.append(user.id)
    if not include_inactive:
        q += " AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > ?)"
        args.append(now())
    rows = conn.execute(q + " ORDER BY s.id DESC", args).fetchall()
    if target_type is not None:
        out = []
        for r in rows:
            try:
                get_share_for_manage(conn, user, r["id"])
                out.append(r)
            except NotFound:
                pass
        return out
    return rows


# ---------------------------------------------------------------------------
# 公開側のアクセス（security.md 4.2）
# ---------------------------------------------------------------------------

@dataclass
class ShareAccess:
    share: sqlite3.Row
    creator: User
    folder_id: int | None        # フォルダの共有なら共有したフォルダ
    document: sqlite3.Row | None  # 文書の共有ならその文書


def resolve(conn: sqlite3.Connection, token: str) -> ShareAccess:
    """トークンを確かめる。使えない理由は区別せず同じエラーにする。"""
    s = conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
    if s is None or s["revoked_at"] or (s["expires_at"] and s["expires_at"] <= now()):
        raise NotFound(INVALID)
    if s["max_downloads"] is not None and s["download_count"] >= s["max_downloads"]:
        raise NotFound(INVALID)
    try:
        creator = get_user(conn, s["created_by"])
        if not creator.is_active:
            raise NotFound(INVALID)
        folder_id = _target_folder(conn, s["target_type"], s["target_id"])
        # 作成者がまだ対象に editor 以上の権限を持っていること
        fo.require_folder_role(conn, creator, folder_id, "editor")
    except (NotFound, Forbidden):
        raise NotFound(INVALID) from None
    doc = None
    if s["target_type"] == "document":
        doc = shared_document(conn, s["target_id"])
    return ShareAccess(s, creator, s["target_id"] if s["target_type"] == "folder" else None, doc)


def shared_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    d = conn.execute(
        "SELECT d.*, b.size_bytes, b.mime_type, b.storage_path, b.thumb_path, b.view_path"
        " FROM documents d JOIN blobs b ON b.id = d.blob_id"
        " WHERE d.id = ? AND d.deleted_at IS NULL", (doc_id,)).fetchone()
    if d is None:
        raise NotFound(INVALID)
    return d


def check_in_share(conn: sqlite3.Connection, acc: ShareAccess, *, folder_id: int | None = None,
                   doc_id: int | None = None) -> None:
    """フォルダの共有で、指定のフォルダ・文書が共有したフォルダの配下にあることを確かめる。"""
    if acc.folder_id is None:
        if doc_id is not None and doc_id != acc.share["target_id"]:
            raise NotFound("見つかりません")
        if folder_id is not None:
            raise NotFound("見つかりません")
        return
    allowed = set(fo.subtree_ids(conn, acc.folder_id))
    if folder_id is not None and folder_id not in allowed:
        raise NotFound("見つかりません")
    if doc_id is not None:
        r = conn.execute("SELECT folder_id FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,)).fetchone()
        if r is None or r[0] not in allowed:
            raise NotFound("見つかりません")


def count_download(conn: sqlite3.Connection, share_id: int) -> None:
    with Tx(conn):
        conn.execute("UPDATE shares SET download_count = download_count + 1 WHERE id = ?", (share_id,))


def uploaded_bytes(conn: sqlite3.Connection, share_id: int) -> int:
    return conn.execute(
        "SELECT coalesce(sum(b.size_bytes), 0) FROM documents d JOIN blobs b ON b.id = d.blob_id"
        " WHERE d.uploaded_via_share_id = ?", (share_id,)).fetchone()[0]


def uploaded_bytes_recent(conn: sqlite3.Connection, hours: int = 24) -> int:
    """すべての受け取り用リンクで、直近 hours 時間に受け取った合計（削除したものも数える）。"""
    since = _iso(dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours))
    return conn.execute(
        "SELECT coalesce(sum(b.size_bytes), 0) FROM documents d JOIN blobs b ON b.id = d.blob_id"
        " WHERE d.uploaded_via_share_id IS NOT NULL AND d.created_at >= ?", (since,)).fetchone()[0]


def upload_allowance(conn: sqlite3.Connection, settings, files_dir, share_id: int) -> int:
    """このリンクで次に受け取れるバイト数。受け取れないときは理由を付けて AppError を出す。

    1 リンクの合計・すべてのリンクの 24 時間の合計・保存先の空きの 3 つで制限する（docs/design/security.md 4.3）。
    """
    import shutil

    from .errors import InsufficientStorage
    st = shutil.disk_usage(files_dir)
    if st.total and st.free / st.total * 100 < settings.share_upload_min_free_pct:
        raise InsufficientStorage("保存先の空き容量が少ないため、いまはファイルを受け取れません")
    gb = 1024 ** 3
    link_left = int(settings.share_upload_link_gb * gb) - uploaded_bytes(conn, share_id)
    if link_left <= 0:
        raise ValidationFailed(f"このリンクで受け取れる合計容量（{settings.share_upload_link_gb:g}GB）を超えました")
    daily_left = int(settings.share_upload_daily_gb * gb) - uploaded_bytes_recent(conn)
    if daily_left <= 0:
        raise ValidationFailed("受け取りが一時的に混み合っています。時間をおいてから送ってください")
    return min(link_left, daily_left)


# ---- パスワード確認済みの Cookie（トークンごと、HMAC 署名つき） ----------

def cookie_name(token: str) -> str:
    return "docseed_share_" + hashlib.sha256(token.encode()).hexdigest()[:12]


def make_pass_cookie(secret: str, token: str) -> str:
    exp = int(time.time()) + PASS_COOKIE_HOURS * 3600
    sig = hmac.new(secret.encode(), f"{token}:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_pass_cookie(secret: str, token: str, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(secret.encode(), f"{token}:{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def check_password(share: sqlite3.Row, password: str) -> bool:
    return verify_password(share["password_hash"], password)
