"""お気に入り・最近開いたファイル・タグ・却下した候補。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import documents as dm
from ...deps import (
    Auth,
    get_conn,
    require_auth,
)
from ..common import (
    base_context,
    display_location,
    router,
    templates,
    toast,
)


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    favs, recent = ps.list_favorites(conn, auth.user), ps.list_recent(conn, auth.user)
    labels: dict[int, str] = {}
    paths = {x["folder_id"]: display_location(conn, auth, x["folder_id"], labels) for x in favs + recent}
    return templates.TemplateResponse(request, "favorites.html", base_context(
        request, conn, auth, nav="favorites", favs=favs, recent=recent, paths=paths))


@router.post("/x/favorite", response_class=HTMLResponse)
def favorite_toggle(request: Request, type: str = Form(...), id: int = Form(...), on: str = Form("1"),
                    small: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    fav = ps.set_favorite(conn, auth.user, type, id, on == "1")
    html = templates.get_template("_fav.html").render({"fav": fav, "fav_type": type, "fav_id": id,
                                                        "small": bool(small)})
    return HTMLResponse(html + str(toast("お気に入りに入れました" if fav else "お気に入りから外しました")))


@router.post("/favorites/recent/clear")
def recent_clear(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    ps.clear_recent(conn, auth.user)
    return RedirectResponse("/favorites?done=recent_cleared", status_code=303)


@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, tag: str = "", conn: sqlite3.Connection = Depends(get_conn),
              auth: Auth = Depends(require_auth)):
    from ... import tags as tg
    docs = tg.documents_with(conn, auth.user.id, tag) if tag else []
    return templates.TemplateResponse(request, "tags.html", base_context(
        request, conn, auth, nav="tags", tags=tg.list_tags(conn, auth.user.id), current=tag or None, docs=docs))


@router.post("/tags/rename")
def tags_rename(old: str = Form(...), new: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    from ... import tags as tg
    tg.rename(conn, auth.user, old, new)
    return RedirectResponse("/tags?done=tag_renamed", status_code=303)


@router.post("/tags/delete")
def tags_delete(name: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                auth: Auth = Depends(require_auth)):
    from ... import tags as tg
    tg.delete(conn, auth.user, name)
    return RedirectResponse("/tags?done=tag_deleted", status_code=303)


@router.get("/suggestions/rejected", response_class=HTMLResponse)
def rejected_page(request: Request, doc: int | None = None, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth)):
    from ...suggest import rejected_list
    doc_filter = dm.get_document(conn, auth.user, doc) if doc else None
    return templates.TemplateResponse(request, "rejected.html", base_context(
        request, conn, auth, nav="settings", items=rejected_list(conn, auth.user.id, doc), doc_filter=doc_filter))


@router.post("/suggestions/{sid}/unreject")
def suggestion_unreject(sid: int, doc: int | None = Form(None), conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ...suggest import unreject
    r = unreject(conn, auth.user, sid)
    back = f"/suggestions/rejected?doc={doc}&" if doc else "/suggestions/rejected?"
    return RedirectResponse(back + ("done=unrejected" if r == "restored" else "done=unrejected_cleared"),
                            status_code=303)
