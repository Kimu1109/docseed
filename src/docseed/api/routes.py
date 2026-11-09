"""REST API（/api/v1）。docs/design/api.md。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import authn, security
from .. import documents as dm
from .. import folders as fo
from .. import users as um
from ..audit import audit
from ..config import Settings
from ..db import Tx, now, schema_version
from ..deps import (
    Auth,
    client_ip,
    get_conn,
    get_settings,
    get_store,
    require_admin,
    require_auth,
    require_session,
)
from ..errors import AppError, NotFound, ValidationFailed
from ..files import derived_response, file_response
from ..storage import BlobStore

router = APIRouter(prefix="/api/v1")
limiter = authn.limiter


# ---------------------------------------------------------------------------
# 表現
# ---------------------------------------------------------------------------

def user_json(u: um.User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "password_login": u.password_login}


def doc_json(conn: sqlite3.Connection, d: sqlite3.Row, role: str | None = None) -> dict[str, Any]:
    warnings = json.loads(d["warnings"]) if d["warnings"] else []
    return {
        "id": d["id"], "folder_id": d["folder_id"], "filename": d["filename"],
        "mime_type": d["mime_type"], "size_bytes": d["size_bytes"], "sha256": d["sha256"],
        "page_count": d["page_count"],
        "processing": {"status": d["status"],
                       "progress": {"done": d["progress_done"], "total": d["progress_total"]},
                       "warnings": warnings, "error": d["error"] if "error" in d.keys() else None},
        "tags": dm.document_tags(conn, d["id"]),
        "created_by": d["created_by"], "created_at": d["created_at"], "updated_at": d["updated_at"],
        "my_role": role,
    }


def folder_json(conn: sqlite3.Connection, f: sqlite3.Row, role: str | None) -> dict[str, Any]:
    return {"id": f["id"], "parent_id": f["parent_id"], "name": f["name"],
            "path": [{"id": c.id, "name": c.name} for c in fo.path(conn, f["id"])],
            "owner_id": f["owner_id"], "my_role": role,
            "created_at": f["created_at"], "updated_at": f["updated_at"]}


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False


def do_login(conn: sqlite3.Connection, settings: Settings, request: Request, response: Response,
             username: str, password: str, remember: bool, next_url: str | None = None) -> authn.LoginResult:
    """パスワードでログインする（API と画面で共通）。確認コードを設定したユーザーは 2 段階目が要る。"""
    return authn.password_login(conn, settings, request, response, username, password, remember, next_url)


@router.post("/auth/login")
def api_login(body: LoginIn, request: Request, response: Response,
              conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings)):
    res = do_login(conn, settings, request, response, body.username, body.password, body.remember)
    if res.second_factor:
        # 確認コードを POST /auth/second-factor に送る（Cookie docseed_2fa、5 分）
        return {"second_factor_required": True, "methods": ["totp", "recovery"]}
    return {"user": user_json(res.user), "csrf_token": res.csrf_token}


@router.post("/auth/logout", status_code=204)
def api_logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn),
               auth: Auth = Depends(require_session)):
    raw = request.cookies.get(security.SESSION_COOKIE)
    if raw:
        security.delete_session(conn, raw)
    response.delete_cookie(security.SESSION_COOKIE, path="/")


# ---------------------------------------------------------------------------
# 自分
# ---------------------------------------------------------------------------

@router.get("/me")
def api_me(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return {**user_json(auth.user), "root_folder_id": um.root_folder_id(conn, auth.user.id),
            "usage_bytes": um.usage_bytes(conn, auth.user.id)}


class MePatch(BaseModel):
    display_name: str


@router.patch("/me")
def api_me_patch(body: MePatch, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_session)):
    return user_json(um.update_user(conn, auth.user.id, display_name=body.display_name))


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


def change_password(conn: sqlite3.Connection, auth: Auth, current: str, new: str, ip: str | None) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (auth.user.id,)).fetchone()
    if not security.verify_password(row["password_hash"], current):
        raise ValidationFailed("現在のパスワードが違います",
                               errors=[{"field": "current_password", "message": "違います"}])
    um.set_password(conn, auth.user.id, new)
    security.delete_other_sessions(conn, auth.user.id, auth.session.id_hash if auth.session else None)
    with Tx(conn):
        audit(conn, "password_change", user_id=auth.user.id, ip=ip)


@router.post("/me/password", status_code=204)
def api_password(body: PasswordIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_session)):
    change_password(conn, auth, body.current_password, body.new_password, client_ip(request))


def token_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL ORDER BY id DESC",
                        (user_id,)).fetchall()


@router.get("/me/tokens")
def api_tokens(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    return {"tokens": [{"id": r["id"], "name": r["name"], "token_prefix": r["token_prefix"],
                        "scopes": r["scopes"].split(","), "last_used_at": r["last_used_at"],
                        "expires_at": r["expires_at"], "created_at": r["created_at"]}
                       for r in token_rows(conn, auth.user.id)]}


class TokenIn(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    expires_at: str | None = None


@router.post("/me/tokens", status_code=201)
def api_token_create(body: TokenIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    raw, tid = security.create_api_token(conn, auth.user.id, body.name, ",".join(sorted(set(body.scopes))),
                                         body.expires_at)
    with Tx(conn):
        audit(conn, "token_create", user_id=auth.user.id, target_type="api_token", target_id=tid,
              ip=client_ip(request))
    return {"id": tid, "token": raw}


def revoke_token(conn: sqlite3.Connection, user_id: int, token_id: int) -> None:
    with Tx(conn):
        cur = conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                           (now(), token_id, user_id))
        if cur.rowcount == 0:
            raise NotFound("トークンが見つかりません")
        audit(conn, "token_revoke", user_id=user_id, target_type="api_token", target_id=token_id)


@router.delete("/me/tokens/{token_id}", status_code=204)
def api_token_revoke(token_id: int, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    revoke_token(conn, auth.user.id, token_id)


# ---------------------------------------------------------------------------
# フォルダ
# ---------------------------------------------------------------------------

@router.get("/folders/shared-with-me")
def api_shared(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return {"folders": [{"id": r["id"], "name": r["name"], "role": r["role"], "owner_name": r["owner_name"]}
                        for r in fo.shared_with(conn, auth.user)]}


@router.get("/folders/{folder_id}")
def api_folder(folder_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    role = fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    return folder_json(conn, fo.get_folder_row(conn, folder_id), role)


@router.get("/folders/{folder_id}/children")
def api_children(folder_id: int, sort: str = "name", order: str = "asc",
                 conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    role = fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    folders, docs = fo.list_children(conn, folder_id, sort=sort, order=order)
    return {
        "folders": [{"id": f["id"], "name": f["name"], "item_count": f["item_count"],
                     "updated_at": f["updated_at"]} for f in folders],
        "documents": [doc_json(conn, d, role) for d in docs],
    }


class FolderIn(BaseModel):
    parent_id: int
    name: str


@router.post("/folders", status_code=201)
def api_folder_create(body: FolderIn, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    fid = fo.create_folder(conn, auth.user, body.parent_id, body.name)
    return folder_json(conn, fo.get_folder_row(conn, fid), fo.effective_role(conn, auth.user, fid))


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None


@router.patch("/folders/{folder_id}")
def api_folder_patch(folder_id: int, body: FolderPatch, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    fo.update_folder(conn, auth.user, folder_id, name=body.name, parent_id=body.parent_id)
    return folder_json(conn, fo.get_folder_row(conn, folder_id), fo.effective_role(conn, auth.user, folder_id))


@router.delete("/folders/{folder_id}", status_code=204)
def api_folder_delete(folder_id: int, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    fo.delete_folder(conn, auth.user, folder_id)


@router.get("/folders/{folder_id}/members")
def api_members(folder_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return {"members": fo.list_members(conn, auth.user, folder_id)}


class MemberIn(BaseModel):
    role: str


@router.put("/folders/{folder_id}/members/{user_id}", status_code=204)
def api_member_set(folder_id: int, user_id: int, body: MemberIn, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    fo.set_member(conn, auth.user, folder_id, user_id, body.role)


@router.delete("/folders/{folder_id}/members/{user_id}", status_code=204)
def api_member_remove(folder_id: int, user_id: int, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    fo.remove_member(conn, auth.user, folder_id, user_id)


def zip_response(conn: sqlite3.Connection, store: BlobStore, folder_id: int, name: str):
    from starlette.background import BackgroundTask

    from ..archive import build_zip
    path = build_zip(conn, store, folder_id)
    return FileResponse(path, media_type="application/zip", filename=f"{name or 'マイファイル'}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/folders/{folder_id}/archive")
def api_folder_archive(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    f = fo.get_folder_row(conn, folder_id)
    with Tx(conn):
        audit(conn, "download", user_id=auth.user.id, target_type="folder", target_id=folder_id,
              ip=client_ip(request), archive=True)
    return zip_response(conn, store, folder_id, f["name"])


@router.get("/users")
def api_user_directory(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """フォルダを共有する相手を選ぶための一覧（有効なユーザーの表示名だけ）。"""
    rows = conn.execute("SELECT id, username, display_name FROM users WHERE is_active = 1 AND id <> ?"
                        " ORDER BY display_name", (auth.user.id,)).fetchall()
    return {"users": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 共有リンク
# ---------------------------------------------------------------------------

def share_json(request: Request, r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "url": str(request.base_url).rstrip("/") + "/s/" + r["token"],
            "target_type": r["target_type"], "target_id": r["target_id"],
            "target_name": r["target_name"] if "target_name" in r.keys() else None,
            "permission": r["permission"], "has_password": bool(r["password_hash"]),
            "expires_at": r["expires_at"], "max_downloads": r["max_downloads"],
            "download_count": r["download_count"], "created_by": r["created_by"], "created_at": r["created_at"],
            "revoked_at": r["revoked_at"]}


@router.get("/shares")
def api_shares(request: Request, target_type: str | None = None, target_id: int | None = None,
               conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    from ..shares import list_shares
    if (target_type is None) != (target_id is None):
        raise ValidationFailed("target_type と target_id は両方指定してください")
    return {"shares": [share_json(request, r)
                       for r in list_shares(conn, auth.user, target_type=target_type, target_id=target_id)]}


class ShareIn(BaseModel):
    target_type: str
    target_id: int
    permission: str = "read"
    expires: str | None = "7d"
    password: str | None = None
    max_downloads: int | None = None


@router.post("/shares", status_code=201)
def api_share_create(body: ShareIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    from ..shares import create_share
    r = create_share(conn, auth.user, body.target_type, body.target_id, permission=body.permission,
                     expires=body.expires, password=body.password, max_downloads=body.max_downloads)
    return share_json(request, r)


class SharePatch(BaseModel):
    expires: str | None = None
    password: str | None = None
    clear_password: bool = False
    max_downloads: int | None = None
    clear_max_downloads: bool = False


@router.patch("/shares/{share_id}")
def api_share_patch(share_id: int, body: SharePatch, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_session)):
    from ..shares import get_share_for_manage, update_share
    update_share(conn, auth.user, share_id, expires=body.expires, password=body.password,
                 clear_password=body.clear_password, max_downloads=body.max_downloads,
                 clear_max=body.clear_max_downloads)
    return share_json(request, get_share_for_manage(conn, auth.user, share_id))


@router.delete("/shares/{share_id}", status_code=204)
def api_share_revoke(share_id: int, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    from ..shares import revoke_share
    revoke_share(conn, auth.user, share_id)


# ---------------------------------------------------------------------------
# 文書
# ---------------------------------------------------------------------------

@router.post("/documents")
def api_upload(request: Request, folder_id: int = Form(...), on_conflict: str = Form("rename"),
               files: list[UploadFile] = File(...), relative_paths: list[str] | None = Form(None),
               conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
               store: BlobStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    """複数ファイルのアップロード。一部だけ失敗することがあるので、結果はファイルごとに返す。"""
    results = []
    for i, f in enumerate(files):
        rel = relative_paths[i] if relative_paths and i < len(relative_paths) and relative_paths[i] else None
        try:
            r = dm.upload(conn, store, auth.user, folder_id, f.filename or "無題", f.file,
                          max_bytes=settings.max_upload_bytes, on_conflict=on_conflict, relative_path=rel,
                          ip=client_ip(request))
            d = dm.get_document(conn, auth.user, r.document_id)
            results.append({"filename": f.filename, "status": "replaced" if r.replaced else "created",
                            "deduplicated": r.deduplicated, "document": doc_json(conn, d)})
        except AppError as e:
            results.append({"filename": f.filename, "status": "error",
                            "error": {"code": e.code, "detail": e.detail}})
    return {"results": results}


@router.get("/documents/{doc_id}")
def api_document(doc_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    d = dm.get_document(conn, auth.user, doc_id)
    return doc_json(conn, d, fo.effective_role(conn, auth.user, d["folder_id"]))


@router.get("/documents/{doc_id}/content")
def api_content(doc_id: int, request: Request, inline: int = 0, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    d = dm.get_document(conn, auth.user, doc_id)
    if not inline and "range" not in request.headers:
        with Tx(conn):
            audit(conn, "download", user_id=auth.user.id, target_type="document", target_id=doc_id,
                  ip=client_ip(request))
    return file_response(store.path(d["storage_path"]), d, inline=bool(inline))


@router.get("/documents/{doc_id}/thumbnail")
def api_thumbnail(doc_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                  store: BlobStore = Depends(get_store)):
    """サムネイル（WebP、長辺 256px）。無ければ 404（画面は種類のアイコンを出す）。"""
    d = dm.get_document(conn, auth.user, doc_id)
    return derived_response(store.derived_path(d["thumb_path"]) if d["thumb_path"] else None)


@router.get("/documents/{doc_id}/view")
def api_view(doc_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
             store: BlobStore = Depends(get_store)):
    """表示用のプレビュー（HEIC などは WebP）。無ければ原本の inline 表示（許可した形式だけ）。"""
    d = dm.get_document(conn, auth.user, doc_id)
    if d["view_path"]:
        return derived_response(store.derived_path(d["view_path"]))
    return file_response(store.path(d["storage_path"]), d, inline=True)


class DocPatch(BaseModel):
    filename: str | None = None
    folder_id: int | None = None
    tags: list[str] | None = None


@router.patch("/documents/{doc_id}")
def api_doc_patch(doc_id: int, body: DocPatch, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth)):
    dm.update_document(conn, auth.user, doc_id, filename=body.filename, folder_id=body.folder_id, tags=body.tags)
    return api_document(doc_id, conn, auth)


class CopyIn(BaseModel):
    folder_id: int
    filename: str | None = None


@router.post("/documents/{doc_id}/copy", status_code=201)
def api_doc_copy(doc_id: int, body: CopyIn, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_auth)):
    new_id = dm.copy_document(conn, auth.user, doc_id, body.folder_id, body.filename)
    return api_document(new_id, conn, auth)


@router.delete("/documents/{doc_id}", status_code=204)
def api_doc_delete(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    dm.delete_document(conn, auth.user, doc_id, ip=client_ip(request))


@router.get("/documents/{doc_id}/text")
def api_doc_text(doc_id: int, page: int | None = None, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_auth)):
    d = dm.get_document(conn, auth.user, doc_id)
    q = "SELECT page_no, source, text, ocr_confidence FROM pages WHERE blob_id = ?"
    args: list[Any] = [d["blob_id"]]
    if page is not None:
        q += " AND page_no = ?"
        args.append(page)
    rows = conn.execute(q + " ORDER BY page_no", args).fetchall()
    return {"status": d["status"], "pages": [dict(r) for r in rows]}


@router.get("/documents/{doc_id}/ocr")
def api_doc_ocr(doc_id: int, page: int = 1, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    d = dm.get_document(conn, auth.user, doc_id)
    pg = conn.execute("SELECT * FROM pages WHERE blob_id = ? AND page_no = ?", (d["blob_id"], page)).fetchone()
    if pg is None or pg["source"] != "ocr":
        raise NotFound("このページの OCR 結果はありません")
    lines = conn.execute("SELECT line_no, text, confidence, bbox, vertical FROM ocr_lines WHERE page_id = ?"
                         " ORDER BY line_no", (pg["id"],)).fetchall()
    return {"page_no": page, "width": pg["width"], "height": pg["height"],
            "lines": [{"line_no": r["line_no"], "text": r["text"], "confidence": r["confidence"],
                       "bbox": json.loads(r["bbox"]), "vertical": bool(r["vertical"])} for r in lines]}


class ReprocessIn(BaseModel):
    force_ocr: bool = False


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def api_doc_reprocess(doc_id: int, body: ReprocessIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    from ..reprocess import enqueue_reprocess
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    queued = enqueue_reprocess(conn, [d["blob_id"]], force_ocr=body.force_ocr)
    with Tx(conn):
        audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
              ip=client_ip(request), force_ocr=body.force_ocr)
    return {"queued": bool(queued)}


# ---------------------------------------------------------------------------
# 検索
# ---------------------------------------------------------------------------

def run_search(request: Request, conn: sqlite3.Connection, auth: Auth, q: str, mode: str, limit: int, offset: int,
               filters: dict[str, str], sem_min: float | None = None):
    from ..search import search
    embedder, err = request.app.state.query_embedder.get() if mode != "keyword" else (None, None)
    floor = request.app.state.settings.search_sem_min if sem_min is None else max(0.0, min(sem_min, 1.0))
    return search(conn, auth.user, q, mode=mode, embedder=embedder, embed_error=err, limit=limit, offset=offset,
                  filters=filters, sem_min=floor)


def folder_label(conn: sqlite3.Connection, folder_id: int, cache: dict[int, str]) -> str:
    if folder_id not in cache:
        cache[folder_id] = "/" + "/".join(c.name for c in fo.path(conn, folder_id) if c.name)
    return cache[folder_id]


@router.get("/search")
def api_search(request: Request, q: str = "", mode: str = "hybrid", type: str | None = None,
               tag: str | None = None, folder: str | None = None, after: str | None = None,
               before: str | None = None, limit: int = 20, offset: int = 0, sem_min: float | None = None,
               conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """sem_min: 意味検索の類似度の下限を一時的に変える（評価スクリプト用。省略時は設定値）。"""
    filters = {"type": type, "tag": tag, "folder": folder, "after": after, "before": before}
    r = run_search(request, conn, auth, q, mode, limit, offset, {k: v for k, v in filters.items() if v}, sem_min)
    labels: dict[int, str] = {}
    return {
        "query": r.query, "mode": r.mode, "total": r.total, "took_ms": r.took_ms, "notices": r.notices,
        "results": [{"document": {**h.document, "path": folder_label(conn, h.document["folder_id"], labels)},
                     "score": h.score,
                     "snippets": [{"chunk_id": s.chunk_id, "page_no": s.page_no, "text": s.html, "match": s.match}
                                  for s in h.snippets]}
                    for h in r.hits],
    }


# ---------------------------------------------------------------------------
# ゴミ箱
# ---------------------------------------------------------------------------

@router.get("/trash")
def api_trash(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return {"items": dm.list_trash(conn, auth.user)}


@router.post("/trash/{item_type}/{item_id}/restore", status_code=204)
def api_restore(item_type: str, item_id: int, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    dm.restore(conn, auth.user, item_type, item_id)


@router.delete("/trash/{item_type}/{item_id}", status_code=204)
def api_purge(item_type: str, item_id: int, conn: sqlite3.Connection = Depends(get_conn),
              auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    dm.purge(conn, store, auth.user, item_type, item_id)


@router.delete("/trash")
def api_empty_trash(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                    store: BlobStore = Depends(get_store)):
    return {"purged": dm.empty_trash(conn, store, auth.user)}


# ---------------------------------------------------------------------------
# 管理
# ---------------------------------------------------------------------------

class UserIn(BaseModel):
    username: str
    display_name: str = ""
    password: str
    role: str = "user"


@router.get("/admin/users")
def api_users(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    return {"users": [user_json(u) for u in um.list_users(conn)]}


@router.post("/admin/users", status_code=201)
def api_user_create(body: UserIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_admin)):
    u = um.create_user(conn, body.username, body.display_name, body.password, body.role)
    with Tx(conn):
        audit(conn, "user_create", user_id=auth.user.id, target_type="user", target_id=u.id, ip=client_ip(request))
    return user_json(u)


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.patch("/admin/users/{user_id}")
def api_user_patch(user_id: int, body: UserPatch, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_admin)):
    u = um.update_user(conn, user_id, display_name=body.display_name, role=body.role, is_active=body.is_active)
    if body.password:
        um.set_password(conn, user_id, body.password)
        security.delete_other_sessions(conn, user_id, None)
    with Tx(conn):
        audit(conn, "user_update", user_id=auth.user.id, target_type="user", target_id=user_id,
              ip=client_ip(request), fields=[k for k, v in body.model_dump().items() if v is not None
                                             and k != "password"] + (["password"] if body.password else []))
    return user_json(u)


def job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT status, count(*) FROM jobs GROUP BY status")}


def failed_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.*, (SELECT filename FROM documents d WHERE d.blob_id = j.blob_id ORDER BY d.id LIMIT 1) AS filename"
        " FROM jobs j WHERE j.status = 'failed' ORDER BY j.finished_at DESC LIMIT ?", (limit,)).fetchall()


@router.get("/admin/jobs")
def api_jobs(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    return {"counts": job_counts(conn),
            "failed": [{"id": r["id"], "type": r["type"], "blob_id": r["blob_id"], "filename": r["filename"],
                        "attempts": r["attempts"], "error": r["error"], "finished_at": r["finished_at"]}
                       for r in failed_jobs(conn)]}


def retry_job(conn: sqlite3.Connection, job_id: int) -> None:
    with Tx(conn):
        cur = conn.execute("UPDATE jobs SET status = 'pending', attempts = 0, run_after = ?, error = NULL,"
                           " finished_at = NULL WHERE id = ? AND status = 'failed'", (now(), job_id))
        if cur.rowcount == 0:
            raise NotFound("失敗したジョブが見つかりません")
        conn.execute("UPDATE blobs SET status = 'pending', updated_at = ? WHERE id ="
                     " (SELECT blob_id FROM jobs WHERE id = ?)", (now(), job_id))


@router.post("/admin/jobs/{job_id}/retry", status_code=204)
def api_job_retry(job_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    retry_job(conn, job_id)


class BulkReprocessIn(BaseModel):
    scope: str
    force_ocr: bool = False


@router.post("/admin/reprocess")
def api_bulk_reprocess(body: BulkReprocessIn, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_admin)):
    from ..reprocess import reprocess
    if body.scope not in ("failed", "outdated", "unsupported", "all"):
        raise ValidationFailed("scope は failed / outdated / unsupported / all のいずれかです")
    return {"queued": reprocess(conn, body.scope, force_ocr=body.force_ocr)}


@router.get("/admin/status")
def api_status(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    """状態と警告（deployment.md 6 章）。外部の監視から使う場合は API トークンで呼ぶ。"""
    from ..monitor import status
    return status(conn, request.app.state.settings)


# ---------------------------------------------------------------------------

health = APIRouter()


@health.get("/healthz")
def healthz(conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    hb = conn.execute("SELECT value FROM settings WHERE key = 'worker.heartbeat'").fetchone()
    return {"status": "ok", "db": "ok", "schema_version": schema_version(conn),
            "worker_last_seen": hb[0] if hb else None}
