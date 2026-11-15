"""利用者ごとの記録: お気に入り・最近開いたファイル・検索の履歴・保存した検索（docs/design/ui.md S11）。

どれも本人だけが見る。一覧を出すときは今の権限で確かめ、消えたもの・見られなくなったものは出さない。
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlencode

from . import folders as fo
from .db import Tx, now
from .errors import NotFound, ValidationFailed
from .users import User

RECENT_KEEP = 50
HISTORY_KEEP = 30
SAVED_MAX = 100
TARGETS = ("document", "folder")
# 検索の条件として残すもの（画面の /search のパラメータ）
SEARCH_KEYS = ("q", "mode", "type", "tag", "after", "before", "taken_from", "taken_to", "folder")


# ---------------------------------------------------------------------------
# お気に入り
# ---------------------------------------------------------------------------

def _check_target(conn: sqlite3.Connection, user: User, target_type: str, target_id: int) -> None:
    if target_type not in TARGETS:
        raise ValidationFailed("種類は document か folder です")
    if target_type == "document":
        from .documents import get_document
        get_document(conn, user, target_id, "viewer")
    else:
        r = conn.execute("SELECT 1 FROM folders WHERE id = ? AND deleted_at IS NULL", (target_id,)).fetchone()
        if r is None:
            raise NotFound("フォルダが見つかりません")
        fo.require_folder_role(conn, user, target_id, "viewer")


def is_favorite(conn: sqlite3.Connection, user: User, target_type: str, target_id: int) -> bool:
    return conn.execute("SELECT 1 FROM favorites WHERE user_id = ? AND target_type = ? AND target_id = ?",
                        (user.id, target_type, target_id)).fetchone() is not None


def set_favorite(conn: sqlite3.Connection, user: User, target_type: str, target_id: int, on: bool) -> bool:
    """お気に入りに入れる・外す。結果（入っているか）を返す。"""
    if on:
        _check_target(conn, user, target_type, target_id)
        with Tx(conn):
            conn.execute("INSERT OR IGNORE INTO favorites(user_id, target_type, target_id, created_at)"
                         " VALUES (?,?,?,?)", (user.id, target_type, target_id, now()))
    else:
        if target_type not in TARGETS:
            raise ValidationFailed("種類は document か folder です")
        with Tx(conn):
            conn.execute("DELETE FROM favorites WHERE user_id = ? AND target_type = ? AND target_id = ?",
                         (user.id, target_type, target_id))
    return on


def _visible(conn: sqlite3.Connection, user: User, folder_id: int, cache: dict[int, bool]) -> bool:
    if folder_id not in cache:
        cache[folder_id] = fo.effective_role(conn, user, folder_id) is not None
    return cache[folder_id]


def list_favorites(conn: sqlite3.Connection, user: User) -> list[dict]:
    """新しく入れた順。ゴミ箱の中・見られなくなったものは出さない（完全に消えたものは記録も消す）。"""
    out: list[dict] = []
    gone: list[tuple[str, int]] = []
    seen: dict[int, bool] = {}
    for r in conn.execute("SELECT target_type, target_id, created_at FROM favorites WHERE user_id = ?"
                          " ORDER BY created_at DESC, rowid DESC", (user.id,)).fetchall():
        if r["target_type"] == "document":
            d = conn.execute(
                "SELECT d.id, d.filename, d.kind, d.folder_id, d.updated_at, d.deleted_at, b.mime_type, b.thumb_path"
                " FROM documents d JOIN blobs b ON b.id = d.blob_id WHERE d.id = ?", (r["target_id"],)).fetchone()
            if d is None:
                gone.append((r["target_type"], r["target_id"]))
                continue
            if d["deleted_at"] is None and _visible(conn, user, d["folder_id"], seen):
                out.append({"type": "document", "favorited_at": r["created_at"]} | dict(d))
        else:
            f = conn.execute("SELECT id, name, parent_id, owner_id, updated_at, deleted_at FROM folders WHERE id = ?",
                             (r["target_id"],)).fetchone()
            if f is None:
                gone.append((r["target_type"], r["target_id"]))
                continue
            if f["deleted_at"] is None and _visible(conn, user, f["id"], seen):
                out.append({"type": "folder", "favorited_at": r["created_at"], "id": f["id"],
                            "name": f["name"] or "マイファイル", "folder_id": f["parent_id"] or f["id"],
                            "updated_at": f["updated_at"], "owner_id": f["owner_id"]})
    if gone:
        with Tx(conn):
            conn.executemany("DELETE FROM favorites WHERE user_id = ? AND target_type = ? AND target_id = ?",
                             [(user.id, t, i) for t, i in gone])
    return out


def favorite_ids(conn: sqlite3.Connection, user: User) -> dict[str, set[int]]:
    """一覧で印を付けるための {"document": {...}, "folder": {...}}。"""
    out: dict[str, set[int]] = {"document": set(), "folder": set()}
    for t, i in conn.execute("SELECT target_type, target_id FROM favorites WHERE user_id = ?", (user.id,)):
        out[t].add(i)
    return out


# ---------------------------------------------------------------------------
# 最近開いたファイル
# ---------------------------------------------------------------------------

def record_open(conn: sqlite3.Connection, user: User, doc_id: int) -> None:
    # 消してから入れ直す: rowid が新しくなるので、同じ秒の中でも開いた順に並ぶ（opened_at は秒まで）
    with Tx(conn):
        conn.execute("DELETE FROM recent_documents WHERE user_id = ? AND document_id = ?", (user.id, doc_id))
        conn.execute("INSERT INTO recent_documents(user_id, document_id, opened_at) VALUES (?,?,?)",
                     (user.id, doc_id, now()))
        conn.execute("DELETE FROM recent_documents WHERE user_id = ? AND document_id NOT IN"
                     " (SELECT document_id FROM recent_documents WHERE user_id = ? ORDER BY rowid DESC LIMIT ?)",
                     (user.id, user.id, RECENT_KEEP))


def list_recent(conn: sqlite3.Connection, user: User, limit: int = 20) -> list[dict]:
    out: list[dict] = []
    seen: dict[int, bool] = {}
    for d in conn.execute(
            "SELECT d.id, d.filename, d.kind, d.folder_id, d.updated_at, b.mime_type, b.thumb_path, r.opened_at"
            " FROM recent_documents r JOIN documents d ON d.id = r.document_id JOIN blobs b ON b.id = d.blob_id"
            " WHERE r.user_id = ? AND d.deleted_at IS NULL ORDER BY r.rowid DESC",
            (user.id,)).fetchall():
        if _visible(conn, user, d["folder_id"], seen):
            out.append({"type": "document"} | dict(d))
            if len(out) >= limit:
                break
    return out


def clear_recent(conn: sqlite3.Connection, user: User) -> None:
    with Tx(conn):
        conn.execute("DELETE FROM recent_documents WHERE user_id = ?", (user.id,))


# ---------------------------------------------------------------------------
# 検索の履歴・保存した検索
# ---------------------------------------------------------------------------

def normalize_params(params: dict) -> dict[str, str]:
    """残す条件だけにする（空の値と、既定の mode=hybrid は落とす）。"""
    out = {k: str(params[k]).strip() for k in SEARCH_KEYS if params.get(k) not in (None, "")}
    out = {k: v for k, v in out.items() if v}
    if out.get("mode") == "hybrid":
        out.pop("mode")
    if any(len(v) > 500 for v in out.values()):
        raise ValidationFailed("検索の条件が長すぎます")
    return out


def search_url(params: dict) -> str:
    return "/search?" + urlencode(params)


MODE_LABEL = {"semantic": "意味が近い", "keyword": "キーワード"}


def describe_search(p: dict) -> str:
    """「請求書 ・ 種類: pdf ・ 撮影日: 2026-05-01〜」のような説明。"""
    parts = [f"「{p['q']}」"] if p.get("q") else []
    if p.get("mode") in MODE_LABEL:
        parts.append(MODE_LABEL[p["mode"]])
    if p.get("type"):
        parts.append(f"種類: {p['type']}")
    if p.get("tag"):
        parts.append(f"タグ: {p['tag']}")
    if p.get("after") or p.get("before"):
        parts.append(f"更新日: {p.get('after', '')}〜{p.get('before', '')}")
    if p.get("taken_from") or p.get("taken_to"):
        parts.append(f"撮影日: {p.get('taken_from', '')}〜{p.get('taken_to', '')}")
    if p.get("folder"):
        parts.append("フォルダの中")
    return " ・ ".join(parts) or "（条件なし）"


def _row(r: sqlite3.Row, at_key: str) -> dict:
    p = json.loads(r["params"])
    return {"id": r["id"], "params": p, "url": search_url(p), "label": describe_search(p), at_key: r[at_key]} | (
        {"name": r["name"]} if "name" in r.keys() else {})


def record_search(conn: sqlite3.Connection, user: User, params: dict) -> None:
    p = normalize_params(params)
    if not p.get("q") and not (set(p) - {"mode", "folder"}):
        return
    key = json.dumps(p, ensure_ascii=False, sort_keys=True)
    with Tx(conn):   # 消してから入れ直す（id が新しくなり、同じ秒の中でも検索した順に並ぶ）
        conn.execute("DELETE FROM search_history WHERE user_id = ? AND params = ?", (user.id, key))
        conn.execute("INSERT INTO search_history(user_id, params, searched_at) VALUES (?,?,?)",
                     (user.id, key, now()))
        conn.execute("DELETE FROM search_history WHERE user_id = ? AND id NOT IN"
                     " (SELECT id FROM search_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
                     (user.id, user.id, HISTORY_KEEP))


def list_history(conn: sqlite3.Connection, user: User, limit: int = 10) -> list[dict]:
    return [_row(r, "searched_at") for r in conn.execute(
        "SELECT id, params, searched_at FROM search_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user.id, limit))]


def delete_history(conn: sqlite3.Connection, user: User, history_id: int | None = None) -> int:
    """1 件（history_id）かすべてを消す。"""
    with Tx(conn):
        if history_id is None:
            return conn.execute("DELETE FROM search_history WHERE user_id = ?", (user.id,)).rowcount
        return conn.execute("DELETE FROM search_history WHERE user_id = ? AND id = ?",
                            (user.id, history_id)).rowcount


def save_search(conn: sqlite3.Connection, user: User, name: str, params: dict) -> int:
    p = normalize_params(params)
    if not p.get("q") and not (set(p) - {"mode", "folder"}):
        raise ValidationFailed("検索語か絞り込みを指定してください")
    name = (name or "").strip() or p.get("q") or "保存した検索"
    if len(name) > 100:
        raise ValidationFailed("名前は 100 文字以内にしてください")
    with Tx(conn):
        n = conn.execute("SELECT count(*) FROM saved_searches WHERE user_id = ?", (user.id,)).fetchone()[0]
        if n >= SAVED_MAX:
            raise ValidationFailed(f"保存できる検索は {SAVED_MAX} 件までです。使わないものを消してください")
        cur = conn.execute("INSERT INTO saved_searches(user_id, name, params, created_at) VALUES (?,?,?,?)",
                           (user.id, name, json.dumps(p, ensure_ascii=False, sort_keys=True), now()))
    return int(cur.lastrowid)


def list_saved(conn: sqlite3.Connection, user: User) -> list[dict]:
    return [_row(r, "created_at") for r in conn.execute(
        "SELECT id, name, params, created_at FROM saved_searches WHERE user_id = ? ORDER BY name COLLATE NOCASE, id",
        (user.id,))]


def rename_saved(conn: sqlite3.Connection, user: User, saved_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise ValidationFailed("名前は 1〜100 文字にしてください")
    with Tx(conn):
        if conn.execute("UPDATE saved_searches SET name = ? WHERE id = ? AND user_id = ?",
                        (name, saved_id, user.id)).rowcount == 0:
            raise NotFound("保存した検索が見つかりません")


def delete_saved(conn: sqlite3.Connection, user: User, saved_id: int) -> None:
    with Tx(conn):
        if conn.execute("DELETE FROM saved_searches WHERE id = ? AND user_id = ?",
                        (saved_id, user.id)).rowcount == 0:
            raise NotFound("保存した検索が見つかりません")


# ---------------------------------------------------------------------------
# 画面の色（自動 / ライト / ダーク）
# ---------------------------------------------------------------------------

THEMES = ("auto", "light", "dark")


def get_theme(conn: sqlite3.Connection, user: User) -> str:
    r = conn.execute("SELECT value FROM user_prefs WHERE user_id = ? AND key = 'theme'", (user.id,)).fetchone()
    return r[0] if r and r[0] in THEMES else "auto"


def set_theme(conn: sqlite3.Connection, user: User, theme: str) -> str:
    if theme not in THEMES:
        raise ValidationFailed("画面の色は auto / light / dark のいずれかです")
    with Tx(conn):
        conn.execute("INSERT INTO user_prefs(user_id, key, value) VALUES (?, 'theme', ?)"
                     " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (user.id, theme))
    return theme
