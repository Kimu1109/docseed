"""画面（Jinja2 + htmx）。docs/design/ui.md。

データの処理は API と同じサービス関数（documents / folders / users）を使う。
htmx からの部分更新は HTML の断片を返し、結果のお知らせは #toast への OOB swap で表示する。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import __version__, security
from .. import documents as dm
from .. import folders as fo
from .. import users as um
from ..api.routes import change_password, do_login, revoke_token, token_rows
from ..audit import audit
from ..config import Settings
from ..db import Tx
from ..deps import (
    Auth,
    client_ip,
    get_conn,
    get_settings,
    get_store,
    optional_auth,
    require_admin,
    require_auth,
    require_session,
)
from ..errors import AppError
from ..files import can_preview
from ..storage import BlobStore
from . import helpers as h

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    version=__version__, kind_of=h.kind_of, kind_label=h.KIND_LABEL, status_label=h.STATUS_LABEL,
    can_preview=can_preview,
)
templates.env.filters.update(filesize=h.filesize, localtime=h.localtime, shortdate=h.shortdate)

router = APIRouter(include_in_schema=False)

DONE_MESSAGES = {
    "renamed": "名前を変更しました",
    "moved": "移動しました",
    "saved": "保存しました",
    "deleted": "ゴミ箱に移動しました",
    "password": "パスワードを変更しました。他の端末ではログアウトされます",
    "user_created": "ユーザーを作成しました",
    "user_updated": "ユーザーを更新しました",
    "token_revoked": "トークンを無効にしました",
    "reprocess": "再処理を受け付けました。順番に処理されます",
    "retried": "ジョブをやり直します",
}


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def toast(message: str, kind: str = "ok") -> Markup:
    return Markup(f'<div id="toast" hx-swap-oob="true" class="toast toast-{kind}" role="status">'
                  f'{escape(message)}</div>')


def base_context(request: Request, conn: sqlite3.Connection, auth: Auth | None, **extra) -> dict:
    ctx = {"request": request, "user": auth.user if auth else None,
           "csrf_token": getattr(request.state, "csrf_token", ""), "done": None}
    if auth:
        ctx["root_id"] = um.root_folder_id(conn, auth.user.id)
        ctx["usage"] = um.usage_bytes(conn, auth.user.id)
        ctx["pending"] = conn.execute(
            "SELECT count(DISTINCT b.id) FROM documents d JOIN blobs b ON b.id = d.blob_id"
            " JOIN folders f ON f.id = d.folder_id"
            " WHERE f.owner_id = ? AND d.deleted_at IS NULL AND b.status IN ('pending', 'processing')",
            (auth.user.id,)).fetchone()[0]
    done = request.query_params.get("done")
    if done in DONE_MESSAGES:
        ctx["done"] = DONE_MESSAGES[done]
    ctx.update(extra)
    return ctx


def render_error(request: Request, status: int, detail: str) -> Response:
    if is_htmx(request):
        # htmx は 4xx/5xx の応答を差し込まないため、200 でお知らせだけを差し込む
        return HTMLResponse(str(toast(detail, "error")), status_code=200,
                            headers={"HX-Reswap": "none"})
    return templates.TemplateResponse(request, "error.html", {"request": request, "status": status,
                                                              "detail": detail, "user": None}, status_code=status)


def _safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


def _check_origin(request: Request) -> None:
    """ログインフォームの CSRF 対策（セッションが無いので Origin で確認する）。"""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise AppError("別のサイトからのログインは受け付けていません")


# ---------------------------------------------------------------------------
# ログイン
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, auth: Auth | None = Depends(optional_auth)):
    if auth:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request, "user": None, "next": next,
                                                              "error": None, "username": ""})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 remember: str | None = Form(None), next: str | None = Form(None),
                 conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings)):
    _check_origin(request)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    try:
        do_login(conn, settings, request, resp, username, password, bool(remember))
    except AppError as e:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "user": None, "next": next, "error": e.detail, "username": username},
            status_code=e.status)
    return resp


@router.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    raw = request.cookies.get(security.SESSION_COOKIE)
    if raw:
        security.delete_session(conn, raw)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------------------
# ファイル一覧
# ---------------------------------------------------------------------------

@router.get("/")
def home(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return RedirectResponse(f"/f/{um.root_folder_id(conn, auth.user.id)}", status_code=303)


def listing_context(request: Request, conn: sqlite3.Connection, auth: Auth, folder_id: int,
                    sort: str = "name", order: str = "asc") -> dict:
    role = fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    folder = fo.get_folder_row(conn, folder_id)
    folders, docs = fo.list_children(conn, folder_id, sort=sort, order=order)
    owner = um.get_user(conn, folder["owner_id"])
    return base_context(request, conn, auth, folder=folder, crumbs=fo.path(conn, folder_id), role=role,
                        can_edit=role in ("editor", "owner"), folders=folders, docs=docs, sort=sort,
                        order=order, owner=owner, nav="files" if owner.id == auth.user.id else "shared")


def render_listing(request: Request, conn: sqlite3.Connection, auth: Auth, folder_id: int,
                   message: str | None = None, sort: str = "name", order: str = "asc") -> HTMLResponse:
    ctx = listing_context(request, conn, auth, folder_id, sort, order)
    html = templates.get_template("_listing.html").render(ctx)
    if message:
        html += str(toast(message))
    return HTMLResponse(html)


@router.get("/f/{folder_id}", response_class=HTMLResponse)
def folder_page(folder_id: int, request: Request, sort: str = "name", order: str = "asc",
                conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    if is_htmx(request) and request.headers.get("hx-target") == "listing":
        return render_listing(request, conn, auth, folder_id, sort=sort, order=order)
    return templates.TemplateResponse(request, "folder.html",
                                      listing_context(request, conn, auth, folder_id, sort, order))


@router.post("/f/{folder_id}/folders", response_class=HTMLResponse)
def folder_create(folder_id: int, request: Request, name: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    fo.create_folder(conn, auth.user, folder_id, name)
    return render_listing(request, conn, auth, folder_id, f"フォルダ「{name.strip()}」を作成しました")


@router.post("/x/folders/{item_id}/rename", response_class=HTMLResponse)
def folder_rename(item_id: int, request: Request, name: str = Form(""), current: int = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    fo.update_folder(conn, auth.user, item_id, name=name)
    return render_listing(request, conn, auth, current, "名前を変更しました")


@router.post("/x/folders/{item_id}/move", response_class=HTMLResponse)
def folder_move(item_id: int, request: Request, target: int = Form(...), current: int = Form(...),
                conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    fo.update_folder(conn, auth.user, item_id, parent_id=target)
    return render_listing(request, conn, auth, current, "移動しました")


@router.post("/x/folders/{item_id}/delete", response_class=HTMLResponse)
def folder_delete(item_id: int, request: Request, current: int = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    fo.delete_folder(conn, auth.user, item_id)
    return render_listing(request, conn, auth, current, "ゴミ箱に移動しました")


@router.post("/x/documents/{item_id}/rename", response_class=HTMLResponse)
def doc_rename(item_id: int, request: Request, name: str = Form(""), current: int = Form(...),
               conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    dm.update_document(conn, auth.user, item_id, filename=name)
    return render_listing(request, conn, auth, current, "名前を変更しました")


@router.post("/x/documents/{item_id}/move", response_class=HTMLResponse)
def doc_move(item_id: int, request: Request, target: int = Form(...), current: int = Form(...),
             conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    dm.update_document(conn, auth.user, item_id, folder_id=target)
    return render_listing(request, conn, auth, current, "移動しました")


@router.post("/x/documents/{item_id}/delete", response_class=HTMLResponse)
def doc_delete(item_id: int, request: Request, current: int = Form(...),
               conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    dm.delete_document(conn, auth.user, item_id, ip=client_ip(request))
    return render_listing(request, conn, auth, current, "ゴミ箱に移動しました")


@router.get("/x/folders/{folder_id}/targets", response_class=HTMLResponse)
def move_targets(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_auth)):
    """移動先の候補（同じ所有者のフォルダで、編集できるもの）。"""
    fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    owner_id = fo.get_folder_row(conn, folder_id)["owner_id"]
    options = [(fid, label) for fid, label in fo.folders_of_owner(conn, owner_id)
               if fo.effective_role(conn, auth.user, fid) in ("editor", "owner")]
    return HTMLResponse("".join(f'<option value="{fid}">{escape(label)}</option>' for fid, label in options))


@router.get("/shared", response_class=HTMLResponse)
def shared_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return templates.TemplateResponse(request, "shared.html", base_context(
        request, conn, auth, nav="shared", shared=fo.shared_with(conn, auth.user)))


# ---------------------------------------------------------------------------
# 文書詳細
# ---------------------------------------------------------------------------

TEXT_PREVIEW_BYTES = 200_000
PAGES_SHOWN = 30


@router.get("/d/{doc_id}", response_class=HTMLResponse)
def document_page(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    d = dm.get_document(conn, auth.user, doc_id)
    role = fo.effective_role(conn, auth.user, d["folder_id"])
    text_preview = None
    truncated = False
    if d["mime_type"].startswith("text/") and can_preview(d["mime_type"]):
        p = store.path(d["storage_path"])
        if p.exists():
            with p.open("rb") as fh:
                raw = fh.read(TEXT_PREVIEW_BYTES + 1)
            truncated = len(raw) > TEXT_PREVIEW_BYTES
            text_preview = raw[:TEXT_PREVIEW_BYTES].decode("utf-8", "replace")
    targets = [(fid, label) for fid, label in fo.folders_of_owner(conn, d["owner_id"])
               if fo.effective_role(conn, auth.user, fid) in ("editor", "owner")]
    pages = conn.execute("SELECT page_no, source, text, ocr_confidence FROM pages WHERE blob_id = ?"
                         " ORDER BY page_no LIMIT ?", (d["blob_id"], PAGES_SHOWN)).fetchall()
    return templates.TemplateResponse(request, "document.html", base_context(
        request, conn, auth, doc=d, role=role, can_edit=role in ("editor", "owner"),
        crumbs=fo.path(conn, d["folder_id"]), tags=dm.document_tags(conn, doc_id), text_preview=text_preview,
        truncated=truncated, targets=targets, owner=um.get_user(conn, d["owner_id"]),
        pages=pages, pages_shown=PAGES_SHOWN, error=h.problem_message(d["error"]),
        warnings=h.warning_messages(d["warnings"]),
        nav="files" if d["owner_id"] == auth.user.id else "shared"))


@router.post("/d/{doc_id}/reprocess")
def document_reprocess(doc_id: int, request: Request, force_ocr: str | None = Form(None),
                       conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..reprocess import enqueue_reprocess
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    enqueue_reprocess(conn, [d["blob_id"]], force_ocr=bool(force_ocr))
    with Tx(conn):
        audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
              ip=client_ip(request), force_ocr=bool(force_ocr))
    return RedirectResponse(f"/d/{doc_id}?done=reprocess", status_code=303)


@router.post("/d/{doc_id}")
def document_save(doc_id: int, filename: str = Form(...), folder_id: int = Form(...), tags: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    dm.update_document(conn, auth.user, doc_id, filename=filename, folder_id=folder_id,
                       tags=[t for t in tags.replace("、", ",").split(",")])
    return RedirectResponse(f"/d/{doc_id}?done=saved", status_code=303)


@router.post("/d/{doc_id}/delete")
def document_delete(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth)):
    folder_id = dm.get_document(conn, auth.user, doc_id)["folder_id"]
    dm.delete_document(conn, auth.user, doc_id, ip=client_ip(request))
    return RedirectResponse(f"/f/{folder_id}?done=deleted", status_code=303)


# ---------------------------------------------------------------------------
# 検索
# ---------------------------------------------------------------------------

PER_PAGE = 20


def display_location(conn: sqlite3.Connection, auth: Auth, folder_id: int, cache: dict[int, str]) -> str:
    """「マイファイル / 経理 / 2026」や「Hanako さんのファイル / 共有」のような場所の表示。"""
    if folder_id not in cache:
        crumbs = fo.path(conn, folder_id)
        owner_id = fo.get_folder_row(conn, folder_id)["owner_id"]
        if owner_id == auth.user.id:
            root = "マイファイル"
        else:
            root = um.get_user(conn, owner_id).display_name + " さんのファイル"
        cache[folder_id] = " / ".join([root] + [c.name for c in crumbs if c.name])
    return cache[folder_id]
MODE_LABEL = {"hybrid": "すべて", "semantic": "意味が近い", "keyword": "キーワード"}
TYPE_OPTIONS = ["pdf", "image", "text", "doc", "sheet", "slide"]


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", mode: str = "hybrid", type: str = "", tag: str = "",
                after: str = "", before: str = "", folder: str = "", page: int = 1,
                conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..api.routes import folder_label, run_search
    page = max(1, min(page, 1 + 500 // PER_PAGE))
    filters = {k: v for k, v in {"type": type, "tag": tag, "after": after, "before": before,
                                 "folder": folder}.items() if v}
    result = run_search(request, conn, auth, q, mode, PER_PAGE, (page - 1) * PER_PAGE, filters) if q.strip() else None
    labels: dict[int, str] = {}
    paths = {h.document["folder_id"]: display_location(conn, auth, h.document["folder_id"], labels)
             for h in (result.hits if result else [])}
    tags = [r[0] for r in conn.execute(
        "SELECT DISTINCT t.name FROM tags t JOIN document_tags dt ON dt.tag_id = t.id"
        " JOIN documents d ON d.id = dt.document_id AND d.deleted_at IS NULL ORDER BY t.name")]
    folder_name = None
    if folder.isdigit() and fo.effective_role(conn, auth.user, int(folder)):
        folder_name = folder_label(conn, int(folder), labels)
    return templates.TemplateResponse(request, "search.html", base_context(
        request, conn, auth, nav="search", q=q, mode=mode if mode in MODE_LABEL else "hybrid", result=result,
        paths=paths, page=page, per_page=PER_PAGE, mode_label=MODE_LABEL, type_options=TYPE_OPTIONS,
        type=type, tag=tag, after=after, before=before, folder=folder, folder_name=folder_name, tags=tags,
        params={k: v for k, v in {"q": q, "mode": mode, **filters}.items() if v}))


# ---------------------------------------------------------------------------
# ゴミ箱
# ---------------------------------------------------------------------------

def render_trash(request: Request, conn: sqlite3.Connection, auth: Auth, message: str | None = None,
                 partial: bool = False, settings: Settings | None = None) -> HTMLResponse:
    ctx = base_context(request, conn, auth, nav="trash", items=dm.list_trash(conn, auth.user),
                       retention=settings.trash_retention_days if settings else 30)
    if partial:
        html = templates.get_template("_trash_list.html").render(ctx) + (str(toast(message)) if message else "")
        return HTMLResponse(html)
    return templates.TemplateResponse(request, "trash.html", ctx)


@router.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
               settings: Settings = Depends(get_settings)):
    return render_trash(request, conn, auth, settings=settings)


@router.post("/trash/{item_type}/{item_id}/restore", response_class=HTMLResponse)
def trash_restore(item_type: str, item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    dm.restore(conn, auth.user, item_type, item_id)
    return render_trash(request, conn, auth, "元の場所に戻しました", partial=True, settings=settings)


@router.post("/trash/{item_type}/{item_id}/purge", response_class=HTMLResponse)
def trash_purge(item_type: str, item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                settings: Settings = Depends(get_settings)):
    dm.purge(conn, store, auth.user, item_type, item_id)
    return render_trash(request, conn, auth, "完全に削除しました", partial=True, settings=settings)


@router.post("/trash/empty", response_class=HTMLResponse)
def trash_empty(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                store: BlobStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    n = dm.empty_trash(conn, store, auth.user)
    return render_trash(request, conn, auth, f"{n} 件を完全に削除しました", partial=True, settings=settings)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def settings_context(request: Request, conn: sqlite3.Connection, auth: Auth, **extra) -> dict:
    return base_context(request, conn, auth, nav="settings", tokens=token_rows(conn, auth.user.id),
                        new_token=None, error=None, **extra)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_session)):
    return templates.TemplateResponse(request, "settings.html", settings_context(request, conn, auth))


@router.post("/settings/profile")
def settings_profile(display_name: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    um.update_user(conn, auth.user.id, display_name=display_name)
    return RedirectResponse("/settings?done=saved", status_code=303)


@router.post("/settings/password", response_class=HTMLResponse)
def settings_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                      new_password2: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_session)):
    try:
        if new_password != new_password2:
            raise AppError("新しいパスワードが確認用と一致しません")
        change_password(conn, auth, current_password, new_password, client_ip(request))
    except AppError as e:
        return templates.TemplateResponse(request, "settings.html",
                                          settings_context(request, conn, auth) | {"error": e.detail},
                                          status_code=e.status)
    return RedirectResponse("/settings?done=password", status_code=303)


@router.post("/settings/tokens", response_class=HTMLResponse)
def settings_token_create(request: Request, name: str = Form(""), scope: str = Form("read,write"),
                          conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    try:
        raw, tid = security.create_api_token(conn, auth.user.id, name, scope, None)
        with Tx(conn):
            audit(conn, "token_create", user_id=auth.user.id, target_type="api_token", target_id=tid,
                  ip=client_ip(request))
    except AppError as e:
        return templates.TemplateResponse(request, "settings.html",
                                          settings_context(request, conn, auth) | {"error": e.detail},
                                          status_code=e.status)
    return templates.TemplateResponse(request, "settings.html",
                                      settings_context(request, conn, auth) | {"new_token": raw})


@router.post("/settings/tokens/{token_id}/revoke")
def settings_token_revoke(token_id: int, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_session)):
    revoke_token(conn, auth.user.id, token_id)
    return RedirectResponse("/settings?done=token_revoked", status_code=303)


# ---------------------------------------------------------------------------
# 管理
# ---------------------------------------------------------------------------

def admin_context(request: Request, conn: sqlite3.Connection, auth: Auth, settings: Settings, **extra) -> dict:
    import shutil
    disk = shutil.disk_usage(settings.data_dir)
    stats = conn.execute(
        "SELECT (SELECT count(*) FROM documents WHERE deleted_at IS NULL) AS docs,"
        " (SELECT count(*) FROM blobs) AS blobs,"
        " (SELECT coalesce(sum(size_bytes), 0) FROM blobs) AS blob_bytes,"
        " (SELECT count(*) FROM jobs WHERE status IN ('pending', 'running')) AS jobs_waiting,"
        " (SELECT count(*) FROM jobs WHERE status = 'failed') AS jobs_failed").fetchone()
    db_bytes = sum(p.stat().st_size for p in settings.db_path.parent.glob(settings.db_path.name + "*"))
    from ..api.routes import failed_jobs, job_counts
    hb = conn.execute("SELECT value FROM settings WHERE key = 'worker.heartbeat'").fetchone()
    worker_seen = hb[0] if hb else None
    worker_alive = bool(worker_seen) and (_dt.datetime.now(_dt.UTC) - _dt.datetime.strptime(
        worker_seen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.UTC)).total_seconds() < 120
    return base_context(request, conn, auth, nav="admin", users=um.list_users(conn), stats=stats,
                        disk=disk, db_bytes=db_bytes, worker_seen=worker_seen, worker_alive=worker_alive,
                        job_counts=job_counts(conn), failed=failed_jobs(conn, 20), error=None, form={}, **extra)


@router.post("/admin/jobs/{job_id}/retry")
def admin_job_retry(job_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    from ..api.routes import retry_job
    retry_job(conn, job_id)
    return RedirectResponse("/admin?done=retried", status_code=303)


@router.post("/admin/reprocess")
def admin_reprocess(scope: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_admin)):
    from ..reprocess import reprocess
    if scope not in ("failed", "outdated"):
        raise AppError("不明な範囲です")
    reprocess(conn, scope)
    return RedirectResponse("/admin?done=reprocess", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
               settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(request, "admin.html", admin_context(request, conn, auth, settings))


@router.post("/admin/users", response_class=HTMLResponse)
def admin_user_create(request: Request, username: str = Form(""), display_name: str = Form(""),
                      password: str = Form(""), role: str = Form("user"),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    try:
        u = um.create_user(conn, username, display_name, password, role)
        with Tx(conn):
            audit(conn, "user_create", user_id=auth.user.id, target_type="user", target_id=u.id,
                  ip=client_ip(request))
    except AppError as e:
        ctx = admin_context(request, conn, auth, settings) | {
            "error": e.detail, "form": {"username": username, "display_name": display_name, "role": role}}
        return templates.TemplateResponse(request, "admin.html", ctx, status_code=e.status)
    return RedirectResponse("/admin?done=user_created", status_code=303)


@router.post("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_update(user_id: int, request: Request, action: str = Form(...), password: str = Form(""),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    try:
        if action == "disable":
            um.update_user(conn, user_id, is_active=False)
        elif action == "enable":
            um.update_user(conn, user_id, is_active=True)
        elif action == "make_admin":
            um.update_user(conn, user_id, role="admin")
        elif action == "make_user":
            um.update_user(conn, user_id, role="user")
        elif action == "password":
            um.set_password(conn, user_id, password)
            security.delete_other_sessions(conn, user_id, None)
        else:
            raise AppError("不明な操作です")
        with Tx(conn):
            audit(conn, "user_update", user_id=auth.user.id, target_type="user", target_id=user_id,
                  ip=client_ip(request), action=action)
    except AppError as e:
        return templates.TemplateResponse(request, "admin.html",
                                          admin_context(request, conn, auth, settings) | {"error": e.detail},
                                          status_code=e.status)
    return RedirectResponse("/admin?done=user_updated", status_code=303)
