"""ユーザーごとの容量の上限と、ゴミ箱の保持日数（docs/design/data-model.md 3.1・4 章）。

容量の上限:
- 数えるのは、そのユーザーが持ち主のフォルダにある文書（ゴミ箱の中を含む）と、それらの過去の版の大きさ。
  同じ中身のファイル（重複排除で 1 つの実体）も、文書ごとに数える（共有の具合で使用量が変わらないように）。
- 共有されたフォルダや受け取り用の共有リンクに置かれたファイルは、フォルダの持ち主の分として数える。
- 上限は users.quota_bytes（NULL = 全体の既定、0 = 上限なし）。全体の既定は settings の quota.default_bytes。

ゴミ箱の保持日数:
- 利用者が設定で選ぶ（user_prefs の trash_retention_days）。無ければ DOCSEED_TRASH_RETENTION_DAYS。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from .db import Tx
from .errors import AppError, ValidationFailed

DEFAULT_KEY = "quota.default_bytes"
GB = 1024 ** 3
RETENTION_CHOICES = (7, 14, 30, 60, 90, 180, 365)


class QuotaExceeded(AppError):
    status, code, title = 507, "quota_exceeded", "容量の上限に達しています"


# ---------------------------------------------------------------------------
# 容量の上限
# ---------------------------------------------------------------------------

def default_quota(conn: sqlite3.Connection) -> int:
    """全体の既定（バイト。0 = 上限なし）。"""
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (DEFAULT_KEY,)).fetchone()
    try:
        return max(int(r[0]), 0) if r else 0
    except ValueError:
        return 0


def set_default_quota(conn: sqlite3.Connection, quota_bytes: int) -> None:
    if quota_bytes < 0:
        raise ValidationFailed("容量の上限は 0 以上にしてください（0 は上限なし）")
    with Tx(conn):
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (DEFAULT_KEY, str(quota_bytes)))


def user_quota_setting(conn: sqlite3.Connection, user_id: int) -> int | None:
    """そのユーザーに個別に決めた上限（None = 全体の既定に従う、0 = 上限なし）。"""
    r = conn.execute("SELECT quota_bytes FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if r is None or r[0] is None else int(r[0])


def quota(conn: sqlite3.Connection, user_id: int) -> int:
    """そのユーザーに効いている上限（バイト。0 = 上限なし）。"""
    own = user_quota_setting(conn, user_id)
    return default_quota(conn) if own is None else own


def set_user_quota(conn: sqlite3.Connection, user_id: int, quota_bytes: int | None) -> None:
    if quota_bytes is not None and quota_bytes < 0:
        raise ValidationFailed("容量の上限は 0 以上にしてください（0 は上限なし）")
    with Tx(conn):
        conn.execute("UPDATE users SET quota_bytes = ? WHERE id = ?", (quota_bytes, user_id))


def parse_gb(value: str | None) -> int | None:
    """画面の入力（GB）→ バイト。空なら None。"""
    v = (value or "").strip()
    if not v:
        return None
    try:
        gb = float(v)
    except ValueError:
        raise ValidationFailed("容量は GB の数で入れてください（例 50、0.5。0 は上限なし）") from None
    if gb < 0 or gb > 1_000_000:
        raise ValidationFailed("容量は 0 以上の GB の数で入れてください")
    return int(gb * GB)


def usage(conn: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """{current, trash, versions, total}（バイト）。"""
    r = conn.execute(
        "SELECT coalesce(sum(CASE WHEN d.deleted_at IS NULL THEN b.size_bytes END), 0) AS current,"
        " coalesce(sum(CASE WHEN d.deleted_at IS NOT NULL THEN b.size_bytes END), 0) AS trash"
        " FROM documents d JOIN blobs b ON b.id = d.blob_id JOIN folders f ON f.id = d.folder_id"
        " WHERE f.owner_id = ?", (user_id,)).fetchone()
    v = conn.execute(
        "SELECT coalesce(sum(b.size_bytes), 0) FROM document_versions v JOIN blobs b ON b.id = v.blob_id"
        " JOIN documents d ON d.id = v.document_id JOIN folders f ON f.id = d.folder_id WHERE f.owner_id = ?",
        (user_id,)).fetchone()[0]
    out = {"current": int(r["current"]), "trash": int(r["trash"]), "versions": int(v)}
    out["total"] = sum(out.values())
    return out


def status(conn: sqlite3.Connection, user_id: int) -> dict:
    """使用量と上限（画面・API 用）。ratio は上限が無ければ None。"""
    u = usage(conn, user_id)
    q = quota(conn, user_id)
    return u | {"quota": q, "ratio": (u["total"] / q) if q else None,
                "custom": user_quota_setting(conn, user_id) is not None}


def owner_of_folder(conn: sqlite3.Connection, folder_id: int) -> int:
    return int(conn.execute("SELECT owner_id FROM folders WHERE id = ?", (folder_id,)).fetchone()[0])


def _fmt(n: int) -> str:
    from .web.helpers import filesize
    return filesize(n)


def check(conn: sqlite3.Connection, owner_id: int, add_bytes: int, *, owner_is_actor: bool = True) -> None:
    """add_bytes 増やすと上限を超えるなら QuotaExceeded。add_bytes = 0 なら、もう上限に達しているかだけを見る。"""
    q = quota(conn, owner_id)
    if not q:
        return
    total = usage(conn, owner_id)["total"]
    if total + add_bytes > q or (add_bytes == 0 and total >= q):
        who = "" if owner_is_actor else "フォルダの持ち主の"
        hint = ("ゴミ箱を空にするか、過去の版を消すと空きます" if owner_is_actor
                else "持ち主に容量を空けてもらってください")
        raise QuotaExceeded(f"{who}容量の上限（{_fmt(q)}）を超えるため保存できません（使用中 {_fmt(total)}）。{hint}")


# ---------------------------------------------------------------------------
# ゴミ箱の保持日数
# ---------------------------------------------------------------------------

def trash_retention(conn: sqlite3.Connection, user_id: int, default_days: int) -> int:
    r = conn.execute("SELECT value FROM user_prefs WHERE user_id = ? AND key = 'trash_retention_days'",
                     (user_id,)).fetchone()
    try:
        days = int(r[0]) if r else 0
    except ValueError:
        days = 0
    return days if days in RETENTION_CHOICES else default_days


def has_own_retention(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM user_prefs WHERE user_id = ? AND key = 'trash_retention_days'",
                        (user_id,)).fetchone() is not None


def set_trash_retention(conn: sqlite3.Connection, user_id: int, days: int | None) -> None:
    """days が None なら全体の既定に戻す。"""
    if days is not None and days not in RETENTION_CHOICES:
        raise ValidationFailed("ゴミ箱の保持日数は " + "・".join(map(str, RETENTION_CHOICES)) + " 日のどれかです")
    with Tx(conn):
        if days is None:
            conn.execute("DELETE FROM user_prefs WHERE user_id = ? AND key = 'trash_retention_days'", (user_id,))
        else:
            conn.execute("INSERT INTO user_prefs(user_id, key, value) VALUES (?, 'trash_retention_days', ?)"
                         " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (user_id, str(days)))


def retention_cutoffs(conn: sqlite3.Connection, default_days: int, *,
                      now_: dt.datetime | None = None) -> dict[int, str]:
    """{ユーザー ID: この日時より前に削除したものを完全に削除する}。"""
    t = now_ or dt.datetime.now(dt.UTC)
    out = {}
    for (uid,) in conn.execute("SELECT id FROM users"):
        days = trash_retention(conn, uid, default_days)
        out[uid] = (t - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out
