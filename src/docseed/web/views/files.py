"""ファイルの一覧・フォルダ・項目の操作（名前の変更・移動・削除・まとめての操作）・共有されたフォルダ。"""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qsl, urlparse

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from ... import documents as dm
from ... import folders as fo
from ... import users as um
from ...audit import audit
from ...db import Tx
from ...deps import (
    Auth,
    client_ip,
    get_conn,
    require_auth,
)
from ...errors import AppError, ValidationFailed
from ...urls import href
from ..common import (
    base_context,
    is_htmx,
    render_error,
    router,
    templates,
    toast,
)


@router.get("/")
def home(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return RedirectResponse(f"/f/{um.root_folder_id(conn, auth.user.id)}", status_code=303)


LISTING_PAGE_SIZE = 100   # 1 ページに出す項目（フォルダと文書の合計）


def _view_params(request: Request, folder_id: int) -> tuple[str, str, int]:
    """一覧の並べ方とページ。操作の後に一覧を描き直すときは、今見ている画面（HX-Current-URL）に合わせる。"""
    q = request.query_params
    if request.url.path != f"/f/{folder_id}":
        cur = urlparse(request.headers.get("hx-current-url") or "")
        q = dict(parse_qsl(cur.query)) if cur.path == href(f"/f/{folder_id}") else {}
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
    from ...webclip.service import error_message, pending_clips
    clips = [dict(c) | {"error_message": error_message(c)} for c in pending_clips(conn, folder_id)] if page == 1 else []
    settings = request.app.state.settings
    reco, auto_moves, in_inbox = {}, [], False
    if settings.suggest_enabled:
        from ... import suggest
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
    from ...personal import favorite_ids
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


@router.post("/x/suggestions/{doc_id}/{sid}/accept", response_class=HTMLResponse)
def listing_suggestion_accept(doc_id: int, sid: int, request: Request, current: int = Form(...),
                              conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ...suggest import accept
    accept(conn, auth.user, doc_id, sid)
    return render_listing(request, conn, auth, current, "おすすめのフォルダへ移動しました")


@router.post("/f/{folder_id}/suggestions/apply", response_class=HTMLResponse)
async def listing_suggestions_apply(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                                    auth: Auth = Depends(require_auth)):
    """チェックした文書を、おすすめのフォルダへまとめて移動する。"""
    from ...suggest import accept, recommended_for_folder
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
    from ...suggest import undo_auto_move
    undo_auto_move(conn, auth.user, audit_id)
    return render_listing(request, conn, auth, current, "元のフォルダに戻しました")


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
    from ...reprocess import enqueue_reprocess
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
    from ...api.suggestions import embedder_of
    from ...suggest import redo
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
