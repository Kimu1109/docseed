"""検索（保存した検索・検索の履歴）。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import folders as fo
from ...deps import (
    Auth,
    get_conn,
    require_auth,
)
from ...i18n import N_
from ..common import (
    base_context,
    display_location,
    router,
    templates,
)

PER_PAGE = 20


MODE_LABEL = {"hybrid": N_("すべて"), "semantic": N_("意味が近い"), "keyword": N_("キーワード")}


TYPE_OPTIONS = ["pdf", "image", "text", "doc", "sheet", "slide", "media", "webclip"]


def folder_options(conn: sqlite3.Connection, auth: Auth) -> list[tuple[int, str]]:
    """絞り込みの「フォルダ」の候補。見られるフォルダを (id, 「マイファイル / 経理」のような表記) で返す。

    表記は検索結果の場所と同じ。自分のフォルダを先に、共有されたフォルダを後に、それぞれ表記の順に並べる。
    """
    from ...search import accessible_folders
    labels: dict[int, str] = {}
    mine = {r[0] for r in conn.execute("SELECT id FROM folders WHERE owner_id = ? AND deleted_at IS NULL",
                                       (auth.user.id,))}
    items = [(fid not in mine, display_location(conn, auth, fid, labels), fid)
             for fid in accessible_folders(conn, auth.user)]
    return [(fid, label) for _, label, fid in sorted(items)]


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", mode: str = "hybrid", type: str = "", tag: str = "",
                after: str = "", before: str = "", folder: str = "", taken_from: str = "", taken_to: str = "",
                page: int = 1, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ...api.routes import folder_label, run_search
    from ...search import LIST_FILTERS
    page = max(1, min(page, 1 + 500 // PER_PAGE))
    taken = f"{taken_from}..{taken_to}" if taken_from or taken_to else ""
    filters = {k: v for k, v in {"type": type, "tag": tag, "after": after, "before": before,
                                 "folder": folder, "taken": taken}.items() if v}
    # 検索語が無くても、種類・タグ・日付・撮影日で絞り込めば一覧を出す（フォルダだけでも出す）
    listing = any(k in filters for k in LIST_FILTERS)
    result = run_search(request, conn, auth, q, mode, PER_PAGE, (page - 1) * PER_PAGE, filters) \
        if q.strip() or listing else None
    from ... import personal as ps
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
        from ...suggest import get_prefs
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
        folder_options=folder_options(conn, auth),
        summaries=summaries, taken_from=taken_from, taken_to=taken_to,
        ask_enabled=settings.llm_enabled and bool(q.strip()), ask_filters=filters,
        saved=ps.list_saved(conn, auth.user), history=ps.list_history(conn, auth.user) if result is None else [],
        current=ps.normalize_params(current),
        params={k: v for k, v in {"q": q, "mode": mode, "taken_from": taken_from, "taken_to": taken_to,
                                  **{k: v for k, v in filters.items() if k != "taken"}}.items() if v}))


@router.post("/search/saved")
async def search_save(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_auth)):
    """今の検索に名前を付けて保存する（フォームの p_* が条件、name が名前）。"""
    from ... import personal as ps
    form = await request.form()
    params = {k[2:]: str(v) for k, v in form.items() if k.startswith("p_")}
    ps.save_search(conn, auth.user, str(form.get("name") or ""), params)
    return RedirectResponse(ps.search_url(ps.normalize_params(params) | {"done": "search_saved"}), status_code=303)


@router.post("/search/saved/{saved_id}/rename")
def search_saved_rename(saved_id: int, name: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    ps.rename_saved(conn, auth.user, saved_id, name)
    return RedirectResponse("/search?done=search_renamed", status_code=303)


@router.post("/search/saved/{saved_id}/delete")
def search_saved_delete(saved_id: int, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    ps.delete_saved(conn, auth.user, saved_id)
    return RedirectResponse("/search?done=search_deleted", status_code=303)


@router.post("/search/history/{history_id}/delete")
def search_history_delete(history_id: int, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    ps.delete_history(conn, auth.user, history_id)
    return RedirectResponse("/search?done=history_deleted", status_code=303)


@router.post("/search/history/clear")
def search_history_clear(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ... import personal as ps
    ps.delete_history(conn, auth.user)
    return RedirectResponse("/search?done=history_cleared", status_code=303)
