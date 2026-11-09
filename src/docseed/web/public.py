"""共有リンクの公開ページ（ログイン不要）。docs/design/security.md 4 章、api.md 3.8、ui.md S10。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import documents as dm
from .. import folders as fo
from .. import security
from .. import shares as sh
from ..audit import audit
from ..config import Settings
from ..db import Tx
from ..deps import client_ip, get_conn, get_settings, get_store
from ..errors import AppError, NotFound
from ..files import can_preview, derived_response, file_response
from ..storage import BlobStore
from .routes import templates

router = APIRouter(prefix="/s", include_in_schema=False)
pass_limiter = security.LoginLimiter(max_failures=10, window_s=900)


def _invalid(request: Request, status: int = 404) -> HTMLResponse:
    return templates.TemplateResponse(request, "share_invalid.html", {"request": request, "user": None},
                                      status_code=status)


def _access(conn: sqlite3.Connection, settings: Settings, request: Request, token: str) -> sh.ShareAccess:
    acc = sh.resolve(conn, token)
    if acc.share["password_hash"] and not sh.check_pass_cookie(
            settings.secret_key, token, request.cookies.get(sh.cookie_name(token))):
        raise _NeedPassword()
    return acc


class _NeedPassword(Exception):
    pass


def _log(conn: sqlite3.Connection, acc: sh.ShareAccess, request: Request, what: str, **kw) -> None:
    with Tx(conn):
        audit(conn, "share_access", user_id=None, target_type=acc.share["target_type"],
              target_id=acc.share["target_id"], ip=client_ip(request), share_id=acc.share["id"], what=what, **kw)


def _guard(fn):
    """無効なリンク・パスワード未入力を共通の画面にする。"""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        request: Request = kwargs["request"]
        try:
            return fn(*args, **kwargs)
        except _NeedPassword:
            return RedirectResponse(f"/s/{kwargs['token']}", status_code=303)
        except NotFound:
            return _invalid(request)
    return wrapper


@router.get("/{token}", response_class=HTMLResponse)
def share_page(token: str, request: Request, folder: int | None = None, conn: sqlite3.Connection = Depends(get_conn),
               settings: Settings = Depends(get_settings)):
    try:
        acc = _access(conn, settings, request, token)
    except _NeedPassword:
        return templates.TemplateResponse(request, "share_password.html",
                                          {"request": request, "user": None, "token": token, "error": None})
    except NotFound:
        return _invalid(request)
    ctx = {"request": request, "user": None, "token": token, "share": acc.share, "creator": acc.creator,
           "can_preview": can_preview}
    if acc.document is not None:
        _log(conn, acc, request, "view")
        return templates.TemplateResponse(request, "share_page.html", ctx | {"doc": acc.document})
    if acc.share["permission"] == "upload":
        root = fo.get_folder_row(conn, acc.folder_id)
        return templates.TemplateResponse(request, "share_page.html", ctx | {"upload_to": root, "results": None})
    current = folder or acc.folder_id
    try:
        sh.check_in_share(conn, acc, folder_id=current)
    except NotFound:
        return _invalid(request)
    folders, docs = fo.list_children(conn, current)
    crumbs = fo.path(conn, current)
    idx = next(i for i, c in enumerate(crumbs) if c.id == acc.folder_id)
    _log(conn, acc, request, "view", folder=current)
    return templates.TemplateResponse(request, "share_page.html", ctx | {
        "folder": fo.get_folder_row(conn, current), "crumbs": crumbs[idx:], "folders": folders, "docs": docs})


@router.post("/{token}/auth", response_class=HTMLResponse)
def share_auth(token: str, request: Request, password: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
               settings: Settings = Depends(get_settings)):
    keys = (f"share:{token}:{client_ip(request)}",)
    try:
        acc = sh.resolve(conn, token)
        pass_limiter.check(*keys)
    except NotFound:
        return _invalid(request)
    except AppError as e:
        return templates.TemplateResponse(request, "share_password.html", {
            "request": request, "user": None, "token": token, "error": e.detail}, status_code=429)
    if not acc.share["password_hash"]:
        return RedirectResponse(f"/s/{token}", status_code=303)
    if not sh.check_password(acc.share, password):
        pass_limiter.failed(*keys)
        _log(conn, acc, request, "password_failed")
        return templates.TemplateResponse(request, "share_password.html", {
            "request": request, "user": None, "token": token, "error": "パスワードが違います"}, status_code=401)
    pass_limiter.succeeded(*keys)
    resp = RedirectResponse(f"/s/{token}", status_code=303)
    resp.set_cookie(sh.cookie_name(token), sh.make_pass_cookie(settings.secret_key, token), httponly=True,
                    secure=settings.cookie_secure, samesite="lax", path=f"/s/{token}",
                    max_age=sh.PASS_COOKIE_HOURS * 3600)
    return resp


def _doc_for(conn: sqlite3.Connection, acc: sh.ShareAccess, doc_id: int | None) -> sqlite3.Row:
    if acc.document is not None:
        sh.check_in_share(conn, acc, doc_id=doc_id or acc.document["id"])
        return acc.document
    if doc_id is None or acc.share["permission"] != "read":
        raise NotFound("見つかりません")
    sh.check_in_share(conn, acc, doc_id=doc_id)
    return sh.shared_document(conn, doc_id)


@router.get("/{token}/download")
@router.get("/{token}/download/{doc_id}")
@_guard
def share_download(token: str, request: Request, doc_id: int | None = None,
                   conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                   store: BlobStore = Depends(get_store)):
    acc = _access(conn, settings, request, token)
    d = _doc_for(conn, acc, doc_id)
    if "range" not in request.headers:
        sh.count_download(conn, acc.share["id"])
        _log(conn, acc, request, "download", document=d["id"])
    return file_response(store.path(d["storage_path"]), d, inline=False)


@router.get("/{token}/preview/{doc_id}")
@_guard
def share_preview(token: str, doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  settings: Settings = Depends(get_settings), store: BlobStore = Depends(get_store)):
    acc = _access(conn, settings, request, token)
    d = _doc_for(conn, acc, doc_id)
    return file_response(store.path(d["storage_path"]), d, inline=True)


@router.get("/{token}/thumb/{doc_id}")
@_guard
def share_thumb(token: str, doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                settings: Settings = Depends(get_settings), store: BlobStore = Depends(get_store)):
    acc = _access(conn, settings, request, token)
    d = _doc_for(conn, acc, doc_id)
    return derived_response(store.derived_path(d["thumb_path"]) if d["thumb_path"] else None)


@router.get("/{token}/view/{doc_id}")
@_guard
def share_view(token: str, doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
               settings: Settings = Depends(get_settings), store: BlobStore = Depends(get_store)):
    acc = _access(conn, settings, request, token)
    d = _doc_for(conn, acc, doc_id)
    if d["view_path"]:
        return derived_response(store.derived_path(d["view_path"]))
    return file_response(store.path(d["storage_path"]), d, inline=True)


@router.get("/{token}/archive")
@_guard
def share_archive(token: str, request: Request, folder: int | None = None,
                  conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                  store: BlobStore = Depends(get_store)):
    from ..api.routes import zip_response
    acc = _access(conn, settings, request, token)
    if acc.folder_id is None or acc.share["permission"] != "read":
        raise NotFound("見つかりません")
    target = folder or acc.folder_id
    sh.check_in_share(conn, acc, folder_id=target)
    sh.count_download(conn, acc.share["id"])
    _log(conn, acc, request, "archive", folder=target)
    return zip_response(conn, store, target, fo.get_folder_row(conn, target)["name"])


@router.post("/{token}/upload", response_class=HTMLResponse)
def share_upload(token: str, request: Request, files: list[UploadFile] = File(...),
                 conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                 store: BlobStore = Depends(get_store)) -> Response:
    try:
        acc = _access(conn, settings, request, token)
    except _NeedPassword:
        return RedirectResponse(f"/s/{token}", status_code=303)
    except NotFound:
        return _invalid(request)
    if acc.share["permission"] != "upload":
        return _invalid(request)
    results = []
    for f in files:
        if not f.filename:
            continue
        try:
            used = sh.uploaded_bytes(conn, acc.share["id"])
            if used >= sh.UPLOAD_LIMIT_PER_LINK:
                raise AppError("このリンクで受け取れる合計容量（10GB）を超えました")
            r = dm.upload(conn, store, acc.creator, acc.folder_id, f.filename, f.file,
                          max_bytes=min(settings.max_upload_bytes, sh.UPLOAD_LIMIT_PER_LINK - used),
                          on_conflict="rename", ip=client_ip(request), via_share_id=acc.share["id"])
            results.append({"name": f.filename, "ok": True, "saved_as": r.filename})
        except AppError as e:
            results.append({"name": f.filename, "ok": False, "error": e.detail})
    _log(conn, acc, request, "upload", count=sum(1 for r in results if r["ok"]))
    root = fo.get_folder_row(conn, acc.folder_id)
    return templates.TemplateResponse(request, "share_page.html", {
        "request": request, "user": None, "token": token, "share": acc.share, "creator": acc.creator,
        "upload_to": root, "results": results, "can_preview": can_preview})
