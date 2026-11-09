"""Web クリップの登録・取得・保存。docs/design/extensions.md 6.1〜6.8。

api は web_clips の行と fetch_url ジョブを作るだけで、外部へは通信しない。
取得は fetcher（`docseed fetcher`）が fetch_url ジョブを 1 件ずつ処理して行う。
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import filetype

from .. import documents as dm
from .. import folders as fo
from ..audit import audit
from ..config import Settings
from ..db import Tx, now
from ..errors import AppError, NotFound, ValidationFailed
from ..pipeline import derive
from ..storage import BlobStore
from ..users import User, get_user
from . import meta as mt
from .safefetch import Fetched, FetchError, SafeFetcher, check_url

log = logging.getLogger("docseed.webclip")

HTML_TYPES = {"text/html", "application/xhtml+xml"}
FILE_PREFIXES = ("image/", "text/")
FILE_TYPES = {"application/pdf"}
IMAGE_MAX = 5 * 1024 * 1024
OEMBED_MAX = 1024 * 1024
RETRY_DELAYS = (60, 600)            # 1 分後・10 分後（2 回まで）


class WebclipDisabled(AppError):
    status, code, title = 409, "webclip_disabled", "URL の取り込みは有効になっていません"


# ---------------------------------------------------------------------------
# 登録（api）
# ---------------------------------------------------------------------------

def _enqueue(conn: sqlite3.Connection, clip_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO jobs(type, blob_id, payload, priority, status, run_after, created_at)"
                 " VALUES ('fetch_url', NULL, ?, 0, 'pending', ?, ?)",
                 (json.dumps({"clip_id": clip_id}), now(), now()))


def create_clip(conn: sqlite3.Connection, settings: Settings, user: User, folder_id: int, url: str,
                ip: str | None = None) -> tuple[int, list[dict]]:
    """クリップを登録して取得のジョブを積む。(clip_id, 同じページを既に保存した文書の一覧) を返す。"""
    if not settings.webclip_enabled:
        raise WebclipDisabled("URL の取り込みは有効になっていません（.env の DOCSEED_WEBCLIP_ENABLED）")
    url = (url or "").strip()
    try:
        check_url(url)
    except FetchError as e:
        raise ValidationFailed(str(e), errors=[{"field": "url", "message": str(e)}]) from None
    fo.require_folder_role(conn, user, folder_id, "editor")
    dups = find_duplicates(conn, user, url)
    with Tx(conn):
        cur = conn.execute("INSERT INTO web_clips(folder_id, url, status, created_by, created_at)"
                           " VALUES (?, ?, 'pending', ?, ?)", (folder_id, url, user.id, now()))
        clip_id = int(cur.lastrowid)
        _enqueue(conn, clip_id)
        audit(conn, "webclip_create", user_id=user.id, target_type="web_clip", target_id=clip_id, ip=ip,
              url=url[:300])
    return clip_id, dups


def find_duplicates(conn: sqlite3.Connection, user: User, url: str) -> list[dict]:
    """同じユーザーのフォルダに、同じ URL（正規の URL・元の URL）のクリップが既にあるか。"""
    rows = conn.execute(
        "SELECT d.id, d.filename, d.folder_id FROM web_clips w JOIN documents d ON d.id = w.document_id"
        " JOIN folders f ON f.id = d.folder_id"
        " WHERE f.owner_id = ? AND d.deleted_at IS NULL AND (w.canonical_url = ? OR w.url = ? OR w.final_url = ?)"
        " ORDER BY d.id", (user.id, url, url, url)).fetchall()
    return [dict(r) for r in rows]


def get_clip(conn: sqlite3.Connection, user: User, clip_id: int, role: str = "viewer") -> sqlite3.Row:
    r = conn.execute("SELECT * FROM web_clips WHERE id = ?", (clip_id,)).fetchone()
    if r is None:
        raise NotFound("見つかりません")
    try:
        fo.require_folder_role(conn, user, r["folder_id"], role)
    except NotFound:
        raise NotFound("見つかりません") from None
    return r


def clip_for_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM web_clips WHERE document_id = ? ORDER BY id DESC LIMIT 1",
                        (doc_id,)).fetchone()


def pending_clips(conn: sqlite3.Connection, folder_id: int) -> list[sqlite3.Row]:
    """一覧に出す、まだ文書になっていない（取得待ち・取得中・失敗した）クリップ。"""
    return conn.execute("SELECT * FROM web_clips WHERE folder_id = ? AND document_id IS NULL ORDER BY id DESC",
                        (folder_id,)).fetchall()


def refetch(conn: sqlite3.Connection, settings: Settings, user: User, clip_id: int, ip: str | None = None) -> None:
    if not settings.webclip_enabled:
        raise WebclipDisabled()
    c = get_clip(conn, user, clip_id, "editor")
    with Tx(conn):
        conn.execute("UPDATE web_clips SET status = 'pending', error = NULL WHERE id = ?", (c["id"],))
        _enqueue(conn, c["id"])
        audit(conn, "webclip_refetch", user_id=user.id, target_type="web_clip", target_id=c["id"], ip=ip)


def delete_pending(conn: sqlite3.Connection, user: User, clip_id: int) -> None:
    """文書になる前のクリップを消す（文書になったものは文書を削除する）。"""
    c = get_clip(conn, user, clip_id, "editor")
    if c["document_id"] is not None:
        raise ValidationFailed("保存済みのクリップは、文書としてゴミ箱に移動してください")
    with Tx(conn):
        conn.execute("DELETE FROM jobs WHERE type = 'fetch_url' AND status = 'pending'"
                     " AND json_extract(payload, '$.clip_id') = ?", (c["id"],))
        conn.execute("DELETE FROM web_clips WHERE id = ?", (c["id"],))


def clip_json(c: sqlite3.Row, image_url: str | None = None) -> dict:
    err = json.loads(c["error"]) if c["error"] else None
    return {"id": c["id"], "folder_id": c["folder_id"], "document_id": c["document_id"], "url": c["url"],
            "final_url": c["final_url"], "canonical_url": c["canonical_url"], "title": c["title"],
            "description": c["description"], "site_name": c["site_name"], "author": c["author"],
            "published_at": c["published_at"], "status": c["status"], "error": err,
            "http_status": c["http_status"], "fetched_at": c["fetched_at"], "created_at": c["created_at"],
            "image_url": image_url}


def error_message(c: sqlite3.Row) -> str | None:
    if not c["error"]:
        return None
    try:
        return json.loads(c["error"]).get("message")
    except ValueError:
        return c["error"]


# ---------------------------------------------------------------------------
# 取得（fetcher）
# ---------------------------------------------------------------------------

def process(conn: sqlite3.Connection, settings: Settings, store: BlobStore, fetcher: SafeFetcher,
            clip_id: int) -> str:
    """fetch_url ジョブの本体。失敗は FetchError を投げる（呼び出し側がリトライを決める）。"""
    c = conn.execute("SELECT * FROM web_clips WHERE id = ?", (clip_id,)).fetchone()
    if c is None:
        return "gone"
    with Tx(conn):
        conn.execute("UPDATE web_clips SET status = 'fetching' WHERE id = ?", (clip_id,))
    user = get_user(conn, c["created_by"])
    html_max = settings.webclip_max_mb * 1024 * 1024

    def limit(mime: str) -> int:
        return html_max if mime in HTML_TYPES or not mime else settings.max_upload_bytes

    f = fetcher.fetch(c["url"], max_bytes=limit, accept="text/html,application/xhtml+xml,*/*;q=0.8",
                      tmp_dir=str(settings.tmp_dir))
    try:
        if _is_html(f):
            _save_page(conn, settings, store, fetcher, c, user, f)
        elif f.mime in FILE_TYPES or f.mime.startswith(FILE_PREFIXES):
            _save_file(conn, settings, store, c, user, f)
        else:
            raise FetchError("fetch_unsupported_type",
                             f"この種類（{f.mime or '不明'}）のページは保存できません（HTML・PDF・画像・テキストのみ）",
                             http_status=f.status)
    finally:
        f.close()
    return "done"


def mark_failed(conn: sqlite3.Connection, clip_id: int, e: FetchError, *, final: bool) -> None:
    with Tx(conn):
        conn.execute("UPDATE web_clips SET status = ?, error = ?, http_status = coalesce(?, http_status)"
                     " WHERE id = ?",
                     ("failed" if final else "pending",
                      json.dumps({"code": e.code, "message": str(e)}, ensure_ascii=False), e.http_status, clip_id))


def _is_html(f: Fetched) -> bool:
    if f.mime in HTML_TYPES:
        return True
    if not f.mime:   # Content-Type が無いときは中身で決める
        head = f.head(512).lstrip().lower()
        return head.startswith((b"<!doctype html", b"<html"))
    return False


def _save_page(conn: sqlite3.Connection, settings: Settings, store: BlobStore, fetcher: SafeFetcher,
               c: sqlite3.Row, user: User, f: Fetched) -> None:
    raw = f.body
    if b"\x00" in raw[:4096]:
        raise FetchError("fetch_unsupported_type", "HTML として読めませんでした（中身がバイナリです）",
                         http_status=f.status)
    text = mt.decode_html(raw, f.charset)
    m = mt.parse(text, f.url)
    tr_title = None
    if not m.author or not m.published_at or not m.title:
        try:
            from trafilatura.metadata import extract_metadata
            md = extract_metadata(text, default_url=f.url)
            if md is not None:
                tr_title = md.title
                m.author = m.author or md.author
                m.published_at = m.published_at or md.date
        except Exception:  # noqa: BLE001 - メタデータの補いが失敗しても保存は続ける
            pass
    mt.finish(m, f.url, tr_title)
    raw_meta = dict(m.raw)
    if m.og_type:
        raw_meta.setdefault("og:type", m.og_type)

    # oEmbed（X のように JavaScript で本文を描くサイトの補い）。使えなくても失敗にしない
    if m.oembed_url:
        try:
            of = fetcher.fetch(m.oembed_url, max_bytes=OEMBED_MAX, accept="application/json")
            try:
                data = json.loads(of.body.decode("utf-8", "replace"))
            finally:
                of.close()
            if isinstance(data, dict):
                raw_meta["oembed"] = {"title": data.get("title"), "author_name": data.get("author_name"),
                                      "provider_name": data.get("provider_name"), "text": mt.oembed_text(data)}
                m.author = m.author or (data.get("author_name") if isinstance(data.get("author_name"), str) else None)
        except (FetchError, ValueError) as e:
            log.info("oembed %s: %s", m.oembed_url, e)

    # og:image（同じ安全の確認。5MB まで。画像でなければ捨てる）
    image_blob_id, image_path, image_mime = None, None, None
    if m.image_url:
        try:
            image_blob_id, image_path, image_mime = _save_image(conn, store, fetcher, m.image_url, settings)
        except FetchError as e:
            log.info("og:image %s: %s", m.image_url, e)

    body = text.encode("utf-8")
    name = mt.file_name(m.title or f.url)
    with Tx(conn):
        doc_id = _store_document(conn, store, settings, c, user, io.BytesIO(body), name, "text/html", "webclip")
        conn.execute(
            "UPDATE web_clips SET document_id = ?, final_url = ?, canonical_url = ?, title = ?, description = ?,"
            " site_name = ?, author = ?, published_at = ?, image_blob_id = ?, meta = ?, http_status = ?,"
            " status = 'done', error = NULL, fetched_at = ? WHERE id = ?",
            (doc_id, f.url, m.canonical_url, m.title, m.description, m.site_name, m.author, m.published_at,
             image_blob_id, json.dumps(raw_meta, ensure_ascii=False), f.status, now(), c["id"]))
    dm.gc_blobs(conn, store)
    # サムネイルは og:image から作り、HTML の blob の thumb_path にする
    if image_path is not None:
        blob = conn.execute("SELECT b.id, b.sha256 FROM documents d JOIN blobs b ON b.id = d.blob_id WHERE d.id = ?",
                            (doc_id,)).fetchone()
        d = derive.derive(settings.data_dir, image_path, image_mime, blob["sha256"])
        if d.thumb_path:
            derive.remove(settings.data_dir, d.view_path)   # Web クリップにはプレビューは要らない
            with Tx(conn):
                conn.execute("UPDATE blobs SET thumb_path = ?, updated_at = ? WHERE id = ?",
                             (d.thumb_path, now(), blob["id"]))


def _save_image(conn: sqlite3.Connection, store: BlobStore, fetcher: SafeFetcher, url: str,
                settings: Settings):
    f = fetcher.fetch(url, max_bytes=IMAGE_MAX, accept="image/*", tmp_dir=str(settings.tmp_dir))
    try:
        kind = filetype.guess(f.head())
        if kind is None or not kind.mime.startswith("image/"):
            raise FetchError("fetch_unsupported_type", "og:image が画像ではありません")
        stored = store.receive(f.file, "og-image", IMAGE_MAX)
    finally:
        f.close()
    with Tx(conn):
        r = conn.execute("SELECT id FROM blobs WHERE sha256 = ?", (stored.sha256,)).fetchone()
        if r is not None:
            store.discard(stored)
            blob_id = int(r[0])
        else:
            store.commit(stored)
            t = now()
            # og:image は文字を読み取らない（ジョブを積まない）。どの文書からも直接は参照されない
            cur = conn.execute(
                "INSERT INTO blobs(sha256, size_bytes, mime_type, storage_path, status, page_count, created_at,"
                " updated_at, processed_at) VALUES (?,?,?,?, 'ready', 0, ?, ?, ?)",
                (stored.sha256, stored.size, kind.mime, stored.rel_path, t, t, t))
            blob_id = int(cur.lastrowid)
    row = conn.execute("SELECT storage_path, mime_type FROM blobs WHERE id = ?", (blob_id,)).fetchone()
    return blob_id, store.path(row["storage_path"]), row["mime_type"]


def _save_file(conn: sqlite3.Connection, settings: Settings, store: BlobStore, c: sqlite3.Row, user: User,
               f: Fetched) -> None:
    """PDF・画像・テキストの URL は、普通のファイルとして保存する（documents.kind = 'file'）。"""
    head = f.head()
    kind = filetype.guess(head)
    if f.mime == "application/pdf" and (kind is None or kind.mime != "application/pdf"):
        raise FetchError("fetch_unsupported_type", "PDF ではありませんでした（Content-Type と中身が違います）")
    if f.mime.startswith("image/") and (kind is None or not kind.mime.startswith("image/")):
        raise FetchError("fetch_unsupported_type", "画像ではありませんでした（Content-Type と中身が違います）")
    if f.mime.startswith("text/") and b"\x00" in head:
        raise FetchError("fetch_unsupported_type", "テキストではありませんでした（中身がバイナリです）")
    name = _name_from_url(f.url, f.mime)
    with Tx(conn):
        doc_id = _store_document(conn, store, settings, c, user, f.file, name, None, "file")
        conn.execute("UPDATE web_clips SET document_id = ?, final_url = ?, canonical_url = ?, title = ?,"
                     " site_name = ?, http_status = ?, status = 'done', error = NULL, fetched_at = ? WHERE id = ?",
                     (doc_id, f.url, f.url, name, urlsplit(f.url).hostname, f.status, now(), c["id"]))
    dm.gc_blobs(conn, store)


def _name_from_url(url: str, mime: str) -> str:
    base = unquote(PurePosixPath(urlsplit(url).path).name) or urlsplit(url).hostname or "download"
    if "." not in base:
        ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv", "text/markdown": ".md"}.get(mime)
        if ext is None and mime.startswith("image/"):
            ext = "." + mime.split("/", 1)[1].split("+")[0]
        base += ext or ""
    return mt.file_name(base).removesuffix(".url") if base else "download"


def _store_document(conn: sqlite3.Connection, store: BlobStore, settings: Settings, c: sqlite3.Row, user: User,
                    src, name: str, mime: str | None, kind: str) -> int:
    """初回は文書を作り、再取得なら既存の文書の中身（blob）を差し替える。呼び出し側のトランザクションの中で使う。"""
    if c["document_id"] is not None:
        d = conn.execute("SELECT id FROM documents WHERE id = ?", (c["document_id"],)).fetchone()
        if d is not None:
            stored = store.receive(src, name, settings.max_upload_bytes)
            if mime:
                stored.mime_type = mime
            blob_id, _dedup = dm._register_blob(conn, store, stored)
            conn.execute("UPDATE documents SET blob_id = ?, updated_at = ? WHERE id = ?", (blob_id, now(), d["id"]))
            return int(d["id"])
    res = dm.upload(conn, store, user, c["folder_id"], name, src, max_bytes=settings.max_upload_bytes,
                    mime_type=mime, kind=kind)
    return res.document_id
