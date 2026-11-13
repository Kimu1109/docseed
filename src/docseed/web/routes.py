"""画面（Jinja2 + htmx）。docs/design/ui.md。

データの処理は API と同じサービス関数（documents / folders / users）を使う。
htmx からの部分更新は HTML の断片を返し、結果のお知らせは #toast への OOB swap で表示する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

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
from ..errors import AppError, NotFound, ValidationFailed
from ..files import can_preview, image_src
from ..storage import BlobStore
from ..versions import list_versions
from . import helpers as h

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    version=__version__, kind_of=h.kind_of, kind_label=h.KIND_LABEL, status_label=h.STATUS_LABEL,
    can_preview=can_preview, image_src=image_src, icon=h.icon, kind_icon=h.KIND_ICON, status_icon=h.STATUS_ICON,
)
templates.env.filters.update(filesize=h.filesize, localtime=h.localtime, shortdate=h.shortdate, taken_at=h.taken_at,
                             duration=h.duration)

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
    "reprocess": "再処理を受け付けました",
    "reprocess_page": "このページの読み直しを受け付けました",
    "tag_renamed": "タグの名前を変えました",
    "tag_deleted": "タグを外しました",
    "unrejected": "却下を取り消しました",
    "unrejected_cleared": "却下の記録を消しました（今の状態では候補に出ません）",
    "retried": "ジョブをやり直します",
    "share_revoked": "共有リンクを無効にしました",
    "clip": "URL を受け付けました",
    "clip_dup": "URL を受け付けました（このページは既に保存されています）",
    "refetch": "再取得を受け付けました",
    "suggest_accept": "候補を採用しました",
    "suggest_reject": "候補を却下しました",
    "prefs": "整理の補助の設定を保存しました",
    "summaries": "古い要約の作り直しを受け付けました",
    "redo": "要約とタグの候補の作り直しを受け付けました",
    "summary_saved": "要約を保存しました",
    "summary_hidden": "この文書では要約を出さないようにしました",
    "summary_restored": "自動で作った要約に戻しました",
    "redo_suggest": "タグとフォルダの候補を作り直しました",
    "redo_short": "タグとフォルダの候補を作り直しました（本文が短いため、要約は作りません）",
    "summaries_none": "作り直す要約はありません",
    "reauthed": "本人確認ができました",
    "totp_disabled": "確認コードを無効にしました",
    "passkey_added": "パスキーを登録しました",
    "passkey_removed": "パスキーを削除しました",
    "identity_linked": "外部アカウントを連携しました",
    "identity_removed": "外部アカウントの連携を解除しました",
    "password_login_on": "パスワードでのログインを有効にしました",
    "password_login_off": "パスワードでのログインを止めました",
    "sessions_revoked": "ほかの端末をすべてログアウトしました",
    "auth_settings": "ログインの設定を保存しました",
    "version_uploaded": "新しい版にしました（前の版は「過去の版」に残しています）",
    "version_same": "今の版と同じ内容でした（版は増やしていません）",
    "version_restored": "過去の版に戻しました（戻す前の版も「過去の版」に残しています）",
    "version_deleted": "過去の版を削除しました",
    "notify_sent": "テスト通知を送りました。届いたか確かめてください",
    "recent_cleared": "最近開いたファイルの記録を消しました",
    "search_saved": "検索を保存しました",
    "search_deleted": "保存した検索を消しました",
    "search_renamed": "保存した検索の名前を変えました",
    "history_deleted": "検索の履歴から消しました",
    "history_cleared": "検索の履歴をすべて消しました",
    "notify_failed": "テスト通知を送れませんでした。下の「最近の通知」のエラーを確認してください",
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
        if auth.user.is_admin:
            from ..monitor import alert_count
            ctx["alert_count"] = alert_count(conn, request.app.state.settings)
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
def login_page(request: Request, next: str | None = None, auth: Auth | None = Depends(optional_auth),
               settings: Settings = Depends(get_settings)):
    from .auth import login_context
    if auth:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", login_context(request, settings, next=next))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 remember: str | None = Form(None), next: str | None = Form(None),
                 conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings)):
    from .auth import login_context
    _check_origin(request)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    try:
        res = do_login(conn, settings, request, resp, username, password, bool(remember), _safe_next(next))
    except AppError as e:
        return templates.TemplateResponse(request, "login.html", login_context(
            request, settings, next=next, error=e.detail, username=username), status_code=e.status)
    if res.second_factor:
        resp.headers["location"] = "/login/second-factor"
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


LISTING_PAGE_SIZE = 100   # 1 ページに出す項目（フォルダと文書の合計）


def _view_params(request: Request, folder_id: int) -> tuple[str, str, int]:
    """一覧の並べ方とページ。操作の後に一覧を描き直すときは、今見ている画面（HX-Current-URL）に合わせる。"""
    q = request.query_params
    if request.url.path != f"/f/{folder_id}":
        cur = urlparse(request.headers.get("hx-current-url") or "")
        q = dict(parse_qsl(cur.query)) if cur.path == f"/f/{folder_id}" else {}
    sort = q.get("sort") if q.get("sort") in ("name", "updated_at", "size") else "name"
    order = "desc" if q.get("order") == "desc" else "asc"
    page = q.get("page") or "1"
    return sort, order, int(page) if page.isdigit() and int(page) > 0 else 1


def listing_context(request: Request, conn: sqlite3.Connection, auth: Auth, folder_id: int,
                    sort: str = "name", order: str = "asc", page: int = 1) -> dict:
    role = fo.require_folder_role(conn, auth.user, folder_id, "viewer")
    folder = fo.get_folder_row(conn, folder_id)
    n_folders, n_docs = fo.count_children(conn, folder_id)
    total = n_folders + n_docs
    pages = max((total - 1) // LISTING_PAGE_SIZE + 1, 1)
    page = min(max(page, 1), pages)
    folders, docs = fo.list_children(conn, folder_id, sort=sort, order=order, limit=LISTING_PAGE_SIZE,
                                     offset=(page - 1) * LISTING_PAGE_SIZE)
    owner = um.get_user(conn, folder["owner_id"])
    from ..webclip.service import error_message, pending_clips
    clips = [dict(c) | {"error_message": error_message(c)} for c in pending_clips(conn, folder_id)] if page == 1 else []
    settings = request.app.state.settings
    reco, auto_moves, in_inbox = {}, [], False
    if settings.suggest_enabled:
        from .. import suggest
        in_inbox = folder_id in suggest.inbox_folder_ids(conn, folder["owner_id"])
        if in_inbox and role in ("editor", "owner"):
            shown = {d["id"] for d in docs}
            reco = {k: v for k, v in suggest.recommended_for_folder(conn, folder_id).items() if k in shown}
            if folder["owner_id"] == auth.user.id and page == 1:
                auto_moves = suggest.auto_moved(conn, auth.user.id, 5)
    return base_context(request, conn, auth, folder=folder, crumbs=fo.path(conn, folder_id), role=role,
                        can_edit=role in ("editor", "owner"), folders=folders, docs=docs, sort=sort,
                        order=order, owner=owner, nav="files" if owner.id == auth.user.id else "shared",
                        clips=clips, webclip_enabled=settings.webclip_enabled, reco=reco, in_inbox=in_inbox,
                        auto_moves=auto_moves, page=page, pages=pages, total=total,
                        page_size=LISTING_PAGE_SIZE, favs=_favorite_ids(conn, auth))


def _favorite_ids(conn: sqlite3.Connection, auth: Auth) -> dict[str, set[int]]:
    from ..personal import favorite_ids
    return favorite_ids(conn, auth.user)


def render_listing(request: Request, conn: sqlite3.Connection, auth: Auth, folder_id: int,
                   message: str | None = None) -> HTMLResponse:
    ctx = listing_context(request, conn, auth, folder_id, *_view_params(request, folder_id))
    html = templates.get_template("_listing.html").render(ctx)
    if message:
        html += str(toast(message))
    return HTMLResponse(html)


@router.get("/f/{folder_id}", response_class=HTMLResponse)
def folder_page(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    if is_htmx(request) and request.headers.get("hx-target") == "listing":
        return render_listing(request, conn, auth, folder_id)
    return templates.TemplateResponse(request, "folder.html",
                                      listing_context(request, conn, auth, folder_id,
                                                      *_view_params(request, folder_id)))


@router.post("/f/{folder_id}/folders", response_class=HTMLResponse)
def folder_create(folder_id: int, request: Request, name: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    fo.create_folder(conn, auth.user, folder_id, name)
    return render_listing(request, conn, auth, folder_id, f"フォルダ「{name.strip()}」を作成しました")


# ---------------------------------------------------------------------------
# 整理の補助（フォルダ・タグの候補）
# ---------------------------------------------------------------------------

@router.post("/x/suggestions/{doc_id}/{sid}/accept", response_class=HTMLResponse)
def listing_suggestion_accept(doc_id: int, sid: int, request: Request, current: int = Form(...),
                              conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..suggest import accept
    accept(conn, auth.user, doc_id, sid)
    return render_listing(request, conn, auth, current, "おすすめのフォルダへ移動しました")


@router.post("/f/{folder_id}/suggestions/apply", response_class=HTMLResponse)
async def listing_suggestions_apply(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                                    auth: Auth = Depends(require_auth)):
    """チェックした文書を、おすすめのフォルダへまとめて移動する。"""
    from ..suggest import accept, recommended_for_folder
    form = await request.form()
    ids = {int(v) for v in form.getlist("doc") if str(v).isdigit()}
    reco = recommended_for_folder(conn, folder_id)
    moved, failed = 0, 0
    for doc_id in ids:
        r = reco.get(doc_id)
        if r is None:
            continue
        try:
            accept(conn, auth.user, doc_id, r["id"])
            moved += 1
        except AppError:
            failed += 1
    msg = f"{moved} 件をおすすめのフォルダへ移動しました" + (f"（{failed} 件は移動できませんでした）" if failed else "")
    if not moved and not failed:
        msg = "おすすめのある文書を選んでください"
    return render_listing(request, conn, auth, folder_id, msg)


@router.post("/x/automove/{audit_id}/undo", response_class=HTMLResponse)
def auto_move_undo(audit_id: int, request: Request, current: int = Form(...),
                   conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..suggest import undo_auto_move
    undo_auto_move(conn, auth.user, audit_id)
    return render_listing(request, conn, auth, current, "元のフォルダに戻しました")


@router.post("/d/{doc_id}/suggestions/{sid}/{action}")
def document_suggestion(doc_id: int, sid: int, action: str, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ..suggest import accept, reject
    if action == "accept":
        accept(conn, auth.user, doc_id, sid)
    elif action == "reject":
        reject(conn, auth.user, doc_id, sid)
    else:
        raise AppError("不明な操作です")
    return RedirectResponse(f"/d/{doc_id}?done={'suggest_' + action}", status_code=303)


@router.post("/settings/prefs")
async def settings_prefs(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                         auth: Auth = Depends(require_session)):
    from ..suggest import PREFS, set_prefs
    form = await request.form()
    set_prefs(conn, auth.user.id, {k: form.get(k) == "1" for k in PREFS})
    return RedirectResponse("/settings?done=prefs", status_code=303)


@router.post("/admin/summaries/rebuild")
def admin_summaries_rebuild(scope: str = Form("outdated"), conn: sqlite3.Connection = Depends(get_conn),
                            auth: Auth = Depends(require_admin), settings: Settings = Depends(get_settings)):
    from ..llm import REBUILD_SCOPES, enqueue_rebuild
    if scope not in REBUILD_SCOPES:
        raise AppError("不明な範囲です")
    n = enqueue_rebuild(conn, settings.llm_model, scope) if settings.llm_enabled else 0
    return RedirectResponse(f"/admin?done={'summaries' if n else 'summaries_none'}", status_code=303)


# ---------------------------------------------------------------------------
# Web クリップ（URL の取り込み）
# ---------------------------------------------------------------------------

def _dup_message(conn: sqlite3.Connection, auth: Auth, dups: list[dict]) -> str:
    if not dups:
        return "URL を受け付けました"
    return (f"URL を受け付けました。このページは既に「{dups[0]['filename']}」として保存されています"
            + (f"（ほか {len(dups) - 1} 件）" if len(dups) > 1 else ""))


@router.post("/f/{folder_id}/webclips", response_class=HTMLResponse)
def webclip_create(folder_id: int, request: Request, url: str = Form(""),
                   conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                   settings: Settings = Depends(get_settings)):
    from ..webclip.service import create_clip
    _cid, dups = create_clip(conn, settings, auth.user, folder_id, url, client_ip(request))
    return render_listing(request, conn, auth, folder_id, _dup_message(conn, auth, dups))


@router.post("/x/webclips/{clip_id}/retry", response_class=HTMLResponse)
def webclip_retry(clip_id: int, request: Request, current: int = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                  settings: Settings = Depends(get_settings)):
    from ..webclip.service import refetch
    refetch(conn, settings, auth.user, clip_id, client_ip(request))
    return render_listing(request, conn, auth, current, "もう一度取得します")


@router.post("/x/webclips/{clip_id}/delete", response_class=HTMLResponse)
def webclip_delete(clip_id: int, request: Request, current: int = Form(...),
                   conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..webclip.service import delete_pending
    delete_pending(conn, auth.user, clip_id)
    return render_listing(request, conn, auth, current, "取り消しました")


@router.post("/d/{doc_id}/refetch")
def document_refetch(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    from ..webclip.service import clip_for_document, refetch
    dm.get_document(conn, auth.user, doc_id, "editor")
    c = clip_for_document(conn, doc_id)
    if c is None:
        raise AppError("この文書は URL から取り込んだものではありません")
    refetch(conn, settings, auth.user, c["id"], client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done=refetch", status_code=303)


@router.get("/clip", response_class=HTMLResponse)
def clip_page(request: Request, url: str = "", conn: sqlite3.Connection = Depends(get_conn),
              auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    """ブックマークレットの行き先。保存先を選んで「保存」を押す（GET だけでは保存しない）。"""
    targets = [(fid, label) for fid, label in fo.folders_of_owner(conn, auth.user.id)]
    return templates.TemplateResponse(request, "clip.html", base_context(
        request, conn, auth, nav="files", url=url[:2048], targets=targets, enabled=settings.webclip_enabled,
        error=None))


@router.post("/clip", response_class=HTMLResponse)
def clip_submit(request: Request, url: str = Form(""), folder_id: int = Form(...),
                conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                settings: Settings = Depends(get_settings)):
    from ..webclip.service import create_clip
    try:
        _cid, dups = create_clip(conn, settings, auth.user, folder_id, url, client_ip(request))
    except AppError as e:
        targets = [(fid, label) for fid, label in fo.folders_of_owner(conn, auth.user.id)]
        return templates.TemplateResponse(request, "clip.html", base_context(
            request, conn, auth, nav="files", url=url, targets=targets, enabled=settings.webclip_enabled,
            error=e.detail), status_code=e.status)
    return RedirectResponse(f"/f/{folder_id}?done={'clip_dup' if dups else 'clip'}", status_code=303)


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


def _parse_items(form) -> list[tuple[str, int]]:
    """item=folder:12 / item=document:34 の並び（重複は除く・順序は保つ）。"""
    out: list[tuple[str, int]] = []
    for v in form.getlist("item"):
        kind, _, num = str(v).partition(":")
        if kind in ("folder", "document") and num.isdigit() and (kind, int(num)) not in out:
            out.append((kind, int(num)))
    return out


def _batch_message(done: int, verb: str, errors: list[str], particle: str = "を") -> str:
    if not errors:
        return f"{done} 件{particle}{verb}しました"
    first = errors[0] + (f" ほか {len(errors) - 1} 件" if len(errors) > 1 else "")
    if not done:
        return first
    return f"{done} 件{particle}{verb}しました（{len(errors)} 件はできませんでした: {first}）"


@router.post("/x/items/move", response_class=HTMLResponse)
async def items_move(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    """選んだフォルダ・文書をまとめて移動する（ドラッグ＆ドロップ・一括の「移動」）。"""
    form = await request.form()
    try:
        target, current = int(form.get("target", "")), int(form.get("current", ""))
    except ValueError:
        raise ValidationFailed("移動先を指定してください") from None
    items = _parse_items(form)
    if not items:
        raise ValidationFailed("移動するものを選んでください")
    moved, errors = 0, []
    for kind, item_id in items:
        if kind == "folder" and item_id == target:
            continue  # 自分自身へのドロップは何もしない
        try:
            if kind == "folder":
                if fo.get_folder_row(conn, item_id)["parent_id"] == target:
                    continue
                fo.update_folder(conn, auth.user, item_id, parent_id=target)
            else:
                dm.update_document(conn, auth.user, item_id, folder_id=target)
            moved += 1
        except AppError as e:
            errors.append(e.detail)
    if not moved and not errors:
        return render_listing(request, conn, auth, current)
    if not moved:
        return render_error(request, 400, _batch_message(0, "移動", errors))
    return render_listing(request, conn, auth, current, _batch_message(moved, "移動", errors))


@router.post("/x/items/delete", response_class=HTMLResponse)
async def items_delete(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_auth)):
    """選んだフォルダ・文書をまとめてゴミ箱へ移動する。"""
    form = await request.form()
    try:
        current = int(form.get("current", ""))
    except ValueError:
        raise ValidationFailed("現在のフォルダを指定してください") from None
    items = _parse_items(form)
    if not items:
        raise ValidationFailed("ゴミ箱に移動するものを選んでください")
    deleted, errors = 0, []
    for kind, item_id in items:
        try:
            if kind == "folder":
                fo.delete_folder(conn, auth.user, item_id)
            else:
                dm.delete_document(conn, auth.user, item_id, ip=client_ip(request))
            deleted += 1
        except AppError as e:
            errors.append(e.detail)
    if not deleted:
        return render_error(request, 400, _batch_message(0, "ゴミ箱に移動", errors))
    return render_listing(request, conn, auth, current, _batch_message(deleted, "ゴミ箱に移動", errors))


def _batch_docs(form) -> tuple[int, list[int], int]:
    """一括の操作の対象: (今のフォルダ, 文書の ID, 選んだフォルダの数)。文書だけが対象（フォルダは中身に及ばない）。"""
    try:
        current = int(form.get("current", ""))
    except ValueError:
        raise ValidationFailed("現在のフォルダを指定してください") from None
    items = _parse_items(form)
    docs = [i for k, i in items if k == "document"]
    if not docs:
        raise ValidationFailed("ファイルを選んでください（フォルダにはこの操作はできません）")
    if len(docs) > BATCH_MAX:
        raise ValidationFailed(f"一度に操作できるのは {BATCH_MAX} 件までです")
    return current, docs, len(items) - len(docs)


BATCH_MAX = 500


def _skipped_folders(msg: str, folders: int) -> str:
    return msg + (f"（フォルダ {folders} 件は対象外です）" if folders else "")


def _split_tags(v) -> list[str]:
    return [t.strip() for t in str(v or "").replace("、", ",").split(",") if t.strip()]


@router.post("/x/items/tags", response_class=HTMLResponse)
async def items_tags(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    """選んだファイルにまとめてタグを付ける・外す（ほかのタグはそのまま）。"""
    form = await request.form()
    current, docs, folders = _batch_docs(form)
    add, remove = _split_tags(form.get("add")), _split_tags(form.get("remove"))
    if not add and not remove:
        raise ValidationFailed("付けるタグか外すタグを入力してください")
    rm = {t.lower() for t in remove}
    done, errors = 0, []
    for doc_id in docs:
        try:
            dm.get_document(conn, auth.user, doc_id, "editor")
            have = dm.document_tags(conn, doc_id)
            new = [t for t in have if t.lower() not in rm]
            new += [t for t in add if t.lower() not in {x.lower() for x in new}]
            if [t.lower() for t in new] != [t.lower() for t in have]:
                dm.update_document(conn, auth.user, doc_id, tags=new)
            done += 1
        except AppError as e:
            errors.append(e.detail)
    if not done:
        return render_error(request, 400, _batch_message(0, "タグを変更", errors))
    return render_listing(request, conn, auth, current,
                          _skipped_folders(_batch_message(done, "タグを変更", errors, particle="の"), folders))


@router.post("/x/items/reprocess", response_class=HTMLResponse)
async def items_reprocess(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_auth)):
    """選んだファイルの文字の読み取りをまとめてやり直す（処理待ち・処理中のものは飛ばす）。"""
    from ..reprocess import enqueue_reprocess
    form = await request.form()
    current, docs, folders = _batch_docs(form)
    force_ocr = form.get("force_ocr") == "1"
    blobs, errors = [], []
    for doc_id in docs:
        try:
            d = dm.get_document(conn, auth.user, doc_id, "editor")
            if d["status"] in ("pending", "processing"):
                errors.append(f"「{d['filename']}」は処理中です")
                continue
            blobs.append(d["blob_id"])
            with Tx(conn):
                audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
                      ip=client_ip(request), force_ocr=force_ocr, batch=True)
        except AppError as e:
            errors.append(e.detail)
    enqueue_reprocess(conn, list(dict.fromkeys(blobs)), force_ocr=force_ocr)
    if not blobs:
        return render_error(request, 400, _batch_message(0, "再処理", errors))
    return render_listing(request, conn, auth, current,
                          _skipped_folders(_batch_message(len(blobs), "再処理に登録", errors), folders))


@router.post("/x/items/redo", response_class=HTMLResponse)
async def items_redo(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    """選んだファイルの要約とタグ（フォルダの候補も）をまとめてやり直す。"""
    from ..api.suggestions import embedder_of
    from ..suggest import redo
    form = await request.form()
    current, docs, folders = _batch_docs(form)
    s = request.app.state.settings
    if not (s.suggest_enabled or s.llm_enabled):
        raise ValidationFailed("整理の補助は有効になっていません")
    done, errors = 0, []
    emb = embedder_of(request)
    for doc_id in docs:
        d = None
        try:
            d = dm.get_document(conn, auth.user, doc_id, "editor")
            redo(conn, auth.user, d, embedder=emb, enabled=s.suggest_enabled, llm_enabled=s.llm_enabled,
                 priority=15)   # まとめて頼んだものは、1 件ずつ頼んだもの（5）より後にする
            done += 1
        except AppError as e:
            errors.append(f"「{d['filename']}」: {e.detail}" if d is not None else e.detail)
    if not done:
        return render_error(request, 400, _batch_message(0, "やり直し", errors))
    return render_listing(request, conn, auth, current,
                          _skipped_folders(_batch_message(done, "やり直しに登録", errors), folders))


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
# 共有ダイアログ（ユーザー・リンク）
# ---------------------------------------------------------------------------

ROLE_LABEL = {"owner": "所有者", "editor": "編集できる", "viewer": "閲覧のみ"}


def gps_photo_count(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    """共有の対象に含まれる、位置情報つきの写真の数（extensions.md 4.3）。"""
    gps = "json_extract(b.image_meta, '$.has_gps') = 1"
    if target_type == "document":
        return conn.execute(f"SELECT count(*) FROM documents d JOIN blobs b ON b.id = d.blob_id"
                            f" WHERE d.id = ? AND {gps}", (target_id,)).fetchone()[0]
    ids = fo.subtree_ids(conn, target_id)
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT count(*) FROM documents d JOIN blobs b ON b.id = d.blob_id"
                        f" WHERE d.folder_id IN ({marks}) AND d.deleted_at IS NULL AND {gps}", ids).fetchone()[0]


def render_share(request: Request, conn: sqlite3.Connection, auth: Auth, target_type: str, target_id: int,
                 message: str | None = None, new_id: int | None = None) -> HTMLResponse:
    from ..shares import list_shares
    if target_type == "folder":
        my_role = fo.require_folder_role(conn, auth.user, target_id, "viewer")
        members = fo.list_members(conn, auth.user, target_id)
        taken = {m["user_id"] for m in members}
        candidates = [dict(r) for r in conn.execute(
            "SELECT id, username, display_name FROM users WHERE is_active = 1 ORDER BY display_name")
            if r["id"] not in taken]
    elif target_type == "document":
        d = dm.get_document(conn, auth.user, target_id)
        my_role = fo.effective_role(conn, auth.user, d["folder_id"])
        members, candidates = [], []
    else:
        raise AppError("共有の対象が正しくありません")
    can_link = my_role in ("editor", "owner")
    links = list_shares(conn, auth.user, target_type=target_type, target_id=target_id) if can_link else []
    gps_count = gps_photo_count(conn, target_type, target_id) if can_link else 0
    html = templates.get_template("_share.html").render(
        request=request, target_type=target_type, target_id=target_id, members=members, candidates=candidates,
        my_role=my_role, links=links, can_link=can_link, role_label=ROLE_LABEL, new_id=new_id, gps_count=gps_count,
        base_url=str(request.base_url).rstrip("/"))
    if message:
        html += str(toast(message))
    return HTMLResponse(html)


@router.get("/x/share", response_class=HTMLResponse)
def share_dialog(request: Request, type: str, id: int, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_session)):
    return render_share(request, conn, auth, type, id)


@router.post("/x/folders/{folder_id}/members", response_class=HTMLResponse)
def share_member_set(folder_id: int, request: Request, user_id: int = Form(...), role: str = Form("viewer"),
                     conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    fo.set_member(conn, auth.user, folder_id, user_id, role)
    name = um.get_user(conn, user_id).display_name
    return render_share(request, conn, auth, "folder", folder_id, f"{name} さんを「{ROLE_LABEL[role]}」にしました")


@router.post("/x/folders/{folder_id}/members/{user_id}/remove", response_class=HTMLResponse)
def share_member_remove(folder_id: int, user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_session)):
    fo.remove_member(conn, auth.user, folder_id, user_id)
    return render_share(request, conn, auth, "folder", folder_id, "共有をやめました")


@router.post("/x/shares", response_class=HTMLResponse)
def share_link_create(request: Request, target_type: str = Form(...), target_id: int = Form(...),
                      permission: str = Form("read"), expires: str = Form("7d"), password: str = Form(""),
                      max_downloads: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_session)):
    from ..shares import create_share
    mx = int(max_downloads) if max_downloads.strip().isdigit() else None
    r = create_share(conn, auth.user, target_type, target_id, permission=permission, expires=expires,
                     password=password or None, max_downloads=mx)
    return render_share(request, conn, auth, target_type, target_id,
                        "リンクを作成しました。コピーして相手に送ってください", new_id=r["id"])


@router.post("/x/shares/{share_id}/revoke", response_class=HTMLResponse)
def share_link_revoke(share_id: int, request: Request, target_type: str = Form(...), target_id: int = Form(...),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    from ..shares import revoke_share
    revoke_share(conn, auth.user, share_id)
    return render_share(request, conn, auth, target_type, target_id, "リンクを無効にしました")


@router.post("/settings/shares/{share_id}/revoke")
def settings_share_revoke(share_id: int, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_session)):
    from ..shares import revoke_share
    revoke_share(conn, auth.user, share_id)
    return RedirectResponse("/settings?done=share_revoked", status_code=303)


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
        warnings=h.warning_messages(d["warnings"]), image_meta=json.loads(d["image_meta"] or "null"),
        clip=_clip_for(conn, d), webclip_enabled=request.app.state.settings.webclip_enabled,
        suggest_enabled=request.app.state.settings.suggest_enabled,
        live=_live_state(conn, request.app.state.settings, d),
        llm_enabled=request.app.state.settings.llm_enabled,
        sugg=_suggestions_for(request, conn, d) if role in ("editor", "owner") else None,
        versions=list_versions(conn, auth.user, doc_id), versions_keep=request.app.state.settings.versions_keep,
        fav=_opened(conn, auth, doc_id),
        nav="files" if d["owner_id"] == auth.user.id else "shared"))


def _opened(conn: sqlite3.Connection, auth: Auth, doc_id: int) -> bool:
    """文書の画面を開いた: 最近開いたファイルに記録し、お気に入りかを返す。"""
    from .. import personal as ps
    ps.record_open(conn, auth.user, doc_id)
    return ps.is_favorite(conn, auth.user, "document", doc_id)


@router.post("/d/{doc_id}/versions")
async def document_version_upload(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                                  auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                                  settings: Settings = Depends(get_settings)):
    from ..versions import upload_new_version
    form = await request.form()
    f = form.get("file")
    if f is None or isinstance(f, str) or not f.filename:
        raise ValidationFailed("ファイルを選んでください")
    changed = upload_new_version(conn, store, auth.user, doc_id, f.filename, f.file,
                                 max_bytes=settings.max_upload_bytes, keep_versions=settings.versions_keep,
                                 ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done={'version_uploaded' if changed else 'version_same'}", status_code=303)


@router.post("/d/{doc_id}/versions/{version_id}/restore")
def document_version_restore(doc_id: int, version_id: int, request: Request,
                             conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                             store: BlobStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    from ..versions import restore_version
    restore_version(conn, store, auth.user, doc_id, version_id, keep_versions=settings.versions_keep,
                    ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done=version_restored", status_code=303)


@router.post("/d/{doc_id}/versions/{version_id}/delete")
def document_version_delete(doc_id: int, version_id: int, request: Request,
                            conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                            store: BlobStore = Depends(get_store)):
    from ..versions import delete_version
    delete_version(conn, store, auth.user, doc_id, version_id, ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done=version_deleted", status_code=303)


def _suggestions_for(request: Request, conn: sqlite3.Connection, d: sqlite3.Row) -> dict:
    from ..api.suggestions import embedder_of
    from ..suggest import for_document
    s = request.app.state.settings
    return for_document(conn, d, embedder=embedder_of(request), enabled=s.suggest_enabled, llm_enabled=s.llm_enabled,
                        llm_model=s.llm_model)


def _clip_for(conn: sqlite3.Connection, d: sqlite3.Row) -> dict | None:
    from ..webclip.service import clip_for_document, error_message
    c = clip_for_document(conn, d["id"])
    return dict(c) | {"error_message": error_message(c)} if c else None


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


@router.post("/d/{doc_id}/redo")
def document_redo(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth)):
    """要約とタグ（と、フォルダの候補）をやり直す。docs/design/extensions.md 7.4。"""
    from ..api.suggestions import embedder_of
    from ..suggest import redo
    s = request.app.state.settings
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    r = redo(conn, auth.user, d, embedder=embedder_of(request), enabled=s.suggest_enabled, llm_enabled=s.llm_enabled)
    done = {"queued": "redo", "too_short": "redo_short"}.get(r["summary"], "redo_suggest")
    return RedirectResponse(f"/d/{doc_id}?done={done}", status_code=303)


@router.post("/d/{doc_id}/summary")
def document_summary_edit(doc_id: int, request: Request, action: str = Form(...), summary: str = Form(""),
                          conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """要約を手で直す（save）・消す（hide）・自動の要約に戻す（restore）。この文書だけに効く。"""
    from .. import llm
    if not request.app.state.settings.llm_enabled:
        raise AppError("要約は有効になっていません")
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    if action == "save":
        if not summary.strip():
            raise AppError("要約を入力してください（消すときは「要約を消す」を押してください）")
        llm.set_manual_summary(conn, d, auth.user.id, summary)
        done = "summary_saved"
    elif action == "hide":
        llm.set_manual_summary(conn, d, auth.user.id, "")
        done = "summary_hidden"
    elif action == "restore":
        llm.clear_manual_summary(conn, d, auth.user.id)
        done = "summary_restored"
    else:
        raise AppError("不明な操作です")
    return RedirectResponse(f"/d/{doc_id}?done={done}", status_code=303)


def _live_state(conn: sqlite3.Connection, settings: Settings, d: sqlite3.Row) -> dict:
    """文書の画面の自動更新に使う状態。key（状態・要約の作り直し中か・要約の日時）が変われば画面を読み直す。"""
    from ..llm import summary_pending
    pending = settings.llm_enabled and summary_pending(conn, d["blob_id"])
    summ = conn.execute("SELECT created_at FROM blob_summaries WHERE blob_id = ?", (d["blob_id"],)).fetchone()
    return {"key": f"{d['status']}|{int(pending)}|{summ[0] if summ else ''}",
            "busy": d["status"] in ("pending", "processing") or pending,
            "status": d["status"], "done": d["progress_done"], "total": d["progress_total"]}


@router.get("/x/d/{doc_id}/related", response_class=HTMLResponse)
def document_related(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    """文書の画面の「関連する文書」（開いた後に読み込む。似ている文書が無ければ何も出さない）。"""
    from ..related import related_documents
    d = dm.get_document(conn, auth.user, doc_id)
    items = related_documents(conn, auth.user, d)
    labels: dict[int, str] = {}
    paths = {r["folder_id"]: display_location(conn, auth, r["folder_id"], labels) for r in items}
    return templates.TemplateResponse(request, "_related.html", {"request": request, "items": items,
                                                                 "paths": paths})


@router.get("/x/d/{doc_id}/state")
def document_state(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    """文書の画面の自動更新（app.js）。進み具合（done/total）は書き換えるだけ。"""
    return _live_state(conn, request.app.state.settings, dm.get_document(conn, auth.user, doc_id))


@router.post("/d/{doc_id}/pages/{page_no}/reprocess")
def document_page_reprocess(doc_id: int, page_no: int, request: Request, force_ocr: str | None = Form(None),
                            conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """このページだけを読み直す（PDF・画像）。"""
    reprocess_one_page(request, conn, auth, doc_id, page_no, bool(force_ocr))
    return RedirectResponse(f"/d/{doc_id}?done=reprocess_page#p{page_no}", status_code=303)


def reprocess_one_page(request: Request, conn: sqlite3.Connection, auth: Auth, doc_id: int, page_no: int,
                       force_ocr: bool) -> None:
    from ..pipeline.extract import kind
    from ..reprocess import enqueue_pages
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    if kind(d["mime_type"]) not in ("pdf", "image"):
        raise ValidationFailed("ページごとの読み直しは PDF と画像だけです")
    if d["status"] in ("pending", "processing"):
        raise ValidationFailed("処理が終わってから読み直してください")
    if not 1 <= page_no <= (d["page_count"] or 0):
        raise NotFound("そのページはありません")
    if not enqueue_pages(conn, d["blob_id"], [page_no], force_ocr=force_ocr):
        raise ValidationFailed("処理が終わってから読み直してください")
    with Tx(conn):
        audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
              ip=client_ip(request), page=page_no, force_ocr=force_ocr)


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
# タグの管理・却下した候補
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# お気に入り・最近開いたファイル
# ---------------------------------------------------------------------------

@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    favs, recent = ps.list_favorites(conn, auth.user), ps.list_recent(conn, auth.user)
    labels: dict[int, str] = {}
    paths = {x["folder_id"]: display_location(conn, auth, x["folder_id"], labels) for x in favs + recent}
    return templates.TemplateResponse(request, "favorites.html", base_context(
        request, conn, auth, nav="favorites", favs=favs, recent=recent, paths=paths))


@router.post("/x/favorite", response_class=HTMLResponse)
def favorite_toggle(request: Request, type: str = Form(...), id: int = Form(...), on: str = Form("1"),
                    small: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    fav = ps.set_favorite(conn, auth.user, type, id, on == "1")
    html = templates.get_template("_fav.html").render({"fav": fav, "fav_type": type, "fav_id": id,
                                                        "small": bool(small)})
    return HTMLResponse(html + str(toast("お気に入りに入れました" if fav else "お気に入りから外しました")))


@router.post("/favorites/recent/clear")
def recent_clear(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    ps.clear_recent(conn, auth.user)
    return RedirectResponse("/favorites?done=recent_cleared", status_code=303)


@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, tag: str = "", conn: sqlite3.Connection = Depends(get_conn),
              auth: Auth = Depends(require_auth)):
    from .. import tags as tg
    docs = tg.documents_with(conn, auth.user.id, tag) if tag else []
    return templates.TemplateResponse(request, "tags.html", base_context(
        request, conn, auth, nav="tags", tags=tg.list_tags(conn, auth.user.id), current=tag or None, docs=docs))


@router.post("/tags/rename")
def tags_rename(old: str = Form(...), new: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    from .. import tags as tg
    tg.rename(conn, auth.user, old, new)
    return RedirectResponse("/tags?done=tag_renamed", status_code=303)


@router.post("/tags/delete")
def tags_delete(name: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    from .. import tags as tg
    tg.delete(conn, auth.user, name)
    return RedirectResponse("/tags?done=tag_deleted", status_code=303)


@router.get("/suggestions/rejected", response_class=HTMLResponse)
def rejected_page(request: Request, doc: int | None = None, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth)):
    from ..suggest import rejected_list
    doc_filter = dm.get_document(conn, auth.user, doc) if doc else None
    return templates.TemplateResponse(request, "rejected.html", base_context(
        request, conn, auth, nav="settings", items=rejected_list(conn, auth.user.id, doc), doc_filter=doc_filter))


@router.post("/suggestions/{sid}/unreject")
def suggestion_unreject(sid: int, doc: int | None = Form(None), conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ..suggest import unreject
    r = unreject(conn, auth.user, sid)
    back = f"/suggestions/rejected?doc={doc}&" if doc else "/suggestions/rejected?"
    return RedirectResponse(back + ("done=unrejected" if r == "restored" else "done=unrejected_cleared"),
                            status_code=303)


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
TYPE_OPTIONS = ["pdf", "image", "text", "doc", "sheet", "slide", "webclip"]


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", mode: str = "hybrid", type: str = "", tag: str = "",
                after: str = "", before: str = "", folder: str = "", taken_from: str = "", taken_to: str = "",
                page: int = 1, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ..api.routes import folder_label, run_search
    from ..search import LIST_FILTERS
    page = max(1, min(page, 1 + 500 // PER_PAGE))
    taken = f"{taken_from}..{taken_to}" if taken_from or taken_to else ""
    filters = {k: v for k, v in {"type": type, "tag": tag, "after": after, "before": before,
                                 "folder": folder, "taken": taken}.items() if v}
    # 検索語が無くても、種類・タグ・日付・撮影日で絞り込めば一覧を出す（フォルダだけでは出さない）
    listing = any(k in filters for k in LIST_FILTERS)
    result = run_search(request, conn, auth, q, mode, PER_PAGE, (page - 1) * PER_PAGE, filters) \
        if q.strip() or listing else None
    from .. import personal as ps
    current = {"q": q, "mode": mode, "type": type, "tag": tag, "after": after, "before": before,
               "taken_from": taken_from, "taken_to": taken_to, "folder": folder}
    if result is not None and page == 1:
        ps.record_search(conn, auth.user, current)
    labels: dict[int, str] = {}
    paths = {h.document["folder_id"]: display_location(conn, auth, h.document["folder_id"], labels)
             for h in (result.hits if result else [])}
    tags = [r[0] for r in conn.execute(
        "SELECT DISTINCT t.name FROM tags t JOIN document_tags dt ON dt.tag_id = t.id"
        " JOIN documents d ON d.id = dt.document_id AND d.deleted_at IS NULL ORDER BY t.name")]
    summaries: dict[int, str] = {}
    settings = request.app.state.settings
    if result and result.hits and settings.llm_enabled:
        from ..suggest import get_prefs
        if get_prefs(conn, auth.user.id)["show_summary"]:
            ids = [h.document["id"] for h in result.hits]
            # 手で直した要約があればそちら（'' は消したので出さない）
            summaries = {r[0]: r[1] for r in conn.execute(
                f"SELECT d.id, coalesce(m.summary, s.summary) FROM documents d"
                f" LEFT JOIN blob_summaries s ON s.blob_id = d.blob_id"
                f" LEFT JOIN document_summaries m ON m.document_id = d.id"
                f" WHERE d.id IN ({','.join('?' * len(ids))})", ids) if r[1]}
    folder_name = None
    if folder.isdigit() and fo.effective_role(conn, auth.user, int(folder)):
        folder_name = folder_label(conn, int(folder), labels)
    return templates.TemplateResponse(request, "search.html", base_context(
        request, conn, auth, nav="search", q=q, mode=mode if mode in MODE_LABEL else "hybrid", result=result,
        paths=paths, page=page, per_page=PER_PAGE, mode_label=MODE_LABEL, type_options=TYPE_OPTIONS,
        type=type, tag=tag, after=after, before=before, folder=folder, folder_name=folder_name, tags=tags,
        summaries=summaries, taken_from=taken_from, taken_to=taken_to,
        saved=ps.list_saved(conn, auth.user), history=ps.list_history(conn, auth.user) if result is None else [],
        current=ps.normalize_params(current),
        params={k: v for k, v in {"q": q, "mode": mode, "taken_from": taken_from, "taken_to": taken_to,
                                  **{k: v for k, v in filters.items() if k != "taken"}}.items() if v}))


@router.post("/search/saved")
async def search_save(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    """今の検索に名前を付けて保存する（フォームの p_* が条件、name が名前）。"""
    from .. import personal as ps
    form = await request.form()
    params = {k[2:]: str(v) for k, v in form.items() if k.startswith("p_")}
    ps.save_search(conn, auth.user, str(form.get("name") or ""), params)
    return RedirectResponse(ps.search_url(ps.normalize_params(params) | {"done": "search_saved"}), status_code=303)


@router.post("/search/saved/{saved_id}/rename")
def search_saved_rename(saved_id: int, name: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    ps.rename_saved(conn, auth.user, saved_id, name)
    return RedirectResponse("/search?done=search_renamed", status_code=303)


@router.post("/search/saved/{saved_id}/delete")
def search_saved_delete(saved_id: int, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    ps.delete_saved(conn, auth.user, saved_id)
    return RedirectResponse("/search?done=search_deleted", status_code=303)


@router.post("/search/history/{history_id}/delete")
def search_history_delete(history_id: int, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    ps.delete_history(conn, auth.user, history_id)
    return RedirectResponse("/search?done=history_deleted", status_code=303)


@router.post("/search/history/clear")
def search_history_clear(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from .. import personal as ps
    ps.delete_history(conn, auth.user)
    return RedirectResponse("/search?done=history_cleared", status_code=303)


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

def _prefs(conn: sqlite3.Connection, user_id: int) -> dict:
    from ..suggest import get_prefs
    return get_prefs(conn, user_id)


def settings_context(request: Request, conn: sqlite3.Connection, auth: Auth, **extra) -> dict:
    from ..shares import list_shares
    settings = request.app.state.settings
    base = settings.public_url or str(request.base_url).rstrip("/")
    bookmarklet = ("javascript:location.href='" + base + "/clip?url='+encodeURIComponent(location.href)"
                   if settings.webclip_enabled else None)
    return base_context(request, conn, auth, nav="settings", tokens=token_rows(conn, auth.user.id),
                        my_shares=list_shares(conn, auth.user), base_url=str(request.base_url).rstrip("/"),
                        bookmarklet=bookmarklet, prefs=_prefs(conn, auth.user.id),
                        suggest_enabled=settings.suggest_enabled,
                        llm_enabled=settings.llm_enabled,
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

def _llm_status(conn: sqlite3.Connection, settings: Settings) -> dict | None:
    if not settings.llm_enabled:
        return None
    from ..llm import llm_stats
    return llm_stats(conn, settings.llm_model) | {"model": settings.llm_model}


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
    from ..authn import get_auth_settings
    from ..monitor import status, system_info
    from ..notify import EVENT_LABEL, describe_destination, recent
    st = status(conn, settings)
    dests = [describe_destination(u) for u in settings.notify_urls]
    if settings.smtp_host and settings.notify_email:
        dests.append(f"メール（{', '.join(settings.notify_email)}）")
    notify = {"enabled": settings.notify_enabled, "destinations": dests, "recent": recent(conn, 10),
              "events": [EVENT_LABEL[e] for e in sorted(settings.notify_events) if e in EVENT_LABEL],
              "event_label": EVENT_LABEL}
    return base_context(request, conn, auth, nav="admin", users=um.list_users(conn), stats=stats,
                        system=system_info(conn, settings), notify=notify,
                        disk=disk, db_bytes=db_bytes, worker_seen=st["worker"]["last_seen"],
                        worker_alive=st["worker"]["alive"], sys=st, thermal_pause_c=settings.thermal_pause_c,
                        job_counts=job_counts(conn), failed=failed_jobs(conn, 20), error=None, form={},
                        llm=_llm_status(conn, settings),
                        **({"reset_done": None, "auth_settings": get_auth_settings(conn),
                            "oidc_enabled": bool(settings.oidc_providers)} | extra))


@router.post("/admin/notify/test")
def admin_notify_test(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    from ..notify import send_test
    try:
        r = send_test(conn, settings)
    except ValueError as e:
        raise AppError(str(e)) from None
    return RedirectResponse(f"/admin?done={'notify_sent' if r['status'] == 'sent' else 'notify_failed'}#notify",
                            status_code=303)


@router.post("/admin/jobs/{job_id}/retry")
def admin_job_retry(job_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    from ..api.routes import retry_job
    retry_job(conn, job_id)
    return RedirectResponse("/admin?done=retried", status_code=303)


@router.post("/admin/reprocess")
def admin_reprocess(scope: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_admin)):
    from ..reprocess import reprocess
    if scope not in ("failed", "outdated", "unsupported"):
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
