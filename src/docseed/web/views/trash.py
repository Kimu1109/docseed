"""ゴミ箱。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

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
    router,
    templates,
    toast,
)


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
