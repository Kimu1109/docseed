"""ゴミ箱。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import documents as dm
from ...config import Settings
from ...deps import (
    Auth,
    get_conn,
    get_settings,
    get_store,
    require_auth,
)
from ...storage import BlobStore
from ..common import (
    base_context,
    is_htmx,
    router,
    templates,
    toast,
)
from .documents import PAGES_SHOWN, clip_for, markdown_preview_of, text_preview_of


def render_trash(request: Request, conn: sqlite3.Connection, auth: Auth, message: str | None = None,
                 partial: bool = False, settings: Settings | None = None) -> HTMLResponse:
    from ...limits import trash_retention
    default = (settings or request.app.state.settings).trash_retention_days
    ctx = base_context(request, conn, auth, nav="trash", items=dm.list_trash(conn, auth.user),
                       retention=trash_retention(conn, auth.user.id, default))
    if partial:
        html = templates.get_template("_trash_list.html").render(ctx) + (str(toast(message)) if message else "")
        return HTMLResponse(html)
    return templates.TemplateResponse(request, "trash.html", ctx)


@router.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
               settings: Settings = Depends(get_settings)):
    return render_trash(request, conn, auth, settings=settings)


@router.get("/trash/document/{doc_id}", response_class=HTMLResponse)
def trash_document_page(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                        settings: Settings = Depends(get_settings)):
    """ゴミ箱の中の文書のプレビュー（消してよいかを確かめる。編集の操作は出さない）。"""
    from ...limits import trash_retention
    d = dm.get_trashed_document(conn, auth.user, doc_id)
    text_preview, truncated = text_preview_of(store, d)
    pages = conn.execute("SELECT page_no, source, text FROM pages WHERE blob_id = ? ORDER BY page_no LIMIT ?",
                         (d["blob_id"], PAGES_SHOWN)).fetchall()
    folder = None
    if d["folder_deleted_at"] == d["deleted_at"]:
        # フォルダごと削除したもの: 戻り先はゴミ箱の中のそのフォルダ（いちばん上の削除単位）
        folder = _trashed_top(conn, d["folder_id"], d["deleted_at"])
    return templates.TemplateResponse(request, "trash_document.html", base_context(
        request, conn, auth, nav="trash", doc=d, text_preview=text_preview, truncated=truncated, pages=pages,
        md_html=markdown_preview_of(d, text_preview),
        pages_shown=PAGES_SHOWN, clip=clip_for(conn, d), location=dm.location_of(conn, d["folder_id"]), folder=folder,
        retention=trash_retention(conn, auth.user.id, settings.trash_retention_days)))


def _trashed_top(conn: sqlite3.Connection, folder_id: int, deleted_at: str) -> sqlite3.Row:
    """同じ削除で消えたフォルダのうち、いちばん上のもの（ゴミ箱の一覧に出ている項目）。"""
    f = conn.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (folder_id,)).fetchone()
    while f["parent_id"] is not None:
        p = conn.execute("SELECT id, name, parent_id, deleted_at FROM folders WHERE id = ?",
                         (f["parent_id"],)).fetchone()
        if p["deleted_at"] != deleted_at:
            break
        f = p
    return f


@router.get("/trash/folder/{folder_id}", response_class=HTMLResponse)
def trash_folder_page(folder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    """ゴミ箱の中のフォルダに入っていたファイルの一覧（それぞれプレビューできる）。"""
    from ...limits import trash_retention
    c = dm.trashed_folder_contents(conn, auth.user, folder_id)
    return templates.TemplateResponse(request, "trash_folder.html", base_context(
        request, conn, auth, nav="trash", folder=c["folder"], docs=c["docs"], subfolders=c["folders"],
        location=c["location"], retention=trash_retention(conn, auth.user.id, settings.trash_retention_days)))


@router.post("/trash/{item_type}/{item_id}/restore", response_class=HTMLResponse)
def trash_restore(item_type: str, item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    dm.restore(conn, auth.user, item_type, item_id)
    if not is_htmx(request):  # プレビューの画面から戻したときは、戻した先を開く
        to = f"/d/{item_id}" if item_type == "document" else f"/f/{item_id}"
        return RedirectResponse(f"{to}?done=restored", status_code=303)
    return render_trash(request, conn, auth, "元の場所に戻しました", partial=True, settings=settings)


@router.post("/trash/{item_type}/{item_id}/purge", response_class=HTMLResponse)
def trash_purge(item_type: str, item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                settings: Settings = Depends(get_settings)):
    dm.purge(conn, store, auth.user, item_type, item_id)
    if not is_htmx(request):
        return RedirectResponse("/trash?done=purged", status_code=303)
    return render_trash(request, conn, auth, "完全に削除しました", partial=True, settings=settings)


@router.post("/trash/empty", response_class=HTMLResponse)
def trash_empty(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                store: BlobStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    n = dm.empty_trash(conn, store, auth.user)
    return render_trash(request, conn, auth, f"{n} 件を完全に削除しました", partial=True, settings=settings)
