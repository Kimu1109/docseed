"""URL の取り込み（Web クリップ）。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import documents as dm
from ... import folders as fo
from ...config import Settings
from ...deps import (
    Auth,
    client_ip,
    get_conn,
    get_settings,
    require_auth,
)
from ...errors import AppError
from ..common import (
    base_context,
    router,
    templates,
)
from .files import render_listing


def _dup_message(conn: sqlite3.Connection, auth: Auth, dups: list[dict]) -> str:
    if not dups:
        return "URL を受け付けました"
    return (f"URL を受け付けました。このページは既に「{dups[0]['filename']}」として保存されています"
            + (f"（ほか {len(dups) - 1} 件）" if len(dups) > 1 else ""))


@router.post("/f/{folder_id}/webclips", response_class=HTMLResponse)
def webclip_create(folder_id: int, request: Request, url: str = Form(""),
                   conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                   settings: Settings = Depends(get_settings)):
    from ...webclip.service import create_clip
    _cid, dups = create_clip(conn, settings, auth.user, folder_id, url, client_ip(request))
    return render_listing(request, conn, auth, folder_id, _dup_message(conn, auth, dups))


@router.post("/x/webclips/{clip_id}/retry", response_class=HTMLResponse)
def webclip_retry(clip_id: int, request: Request, current: int = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                  settings: Settings = Depends(get_settings)):
    from ...webclip.service import refetch
    refetch(conn, settings, auth.user, clip_id, client_ip(request))
    return render_listing(request, conn, auth, current, "もう一度取得します")


@router.post("/x/webclips/{clip_id}/delete", response_class=HTMLResponse)
def webclip_delete(clip_id: int, request: Request, current: int = Form(...),
                   conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ...webclip.service import delete_pending
    delete_pending(conn, auth.user, clip_id)
    return render_listing(request, conn, auth, current, "取り消しました")


@router.post("/d/{doc_id}/refetch")
def document_refetch(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth), settings: Settings = Depends(get_settings)):
    from ...webclip.service import clip_for_document, refetch
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
    from ...webclip.service import create_clip
    try:
        clip_id, dups = create_clip(conn, settings, auth.user, folder_id, url, client_ip(request))
    except AppError as e:
        targets = [(fid, label) for fid, label in fo.folders_of_owner(conn, auth.user.id)]
        return templates.TemplateResponse(request, "clip.html", base_context(
            request, conn, auth, nav="files", url=url, targets=targets, enabled=settings.webclip_enabled,
            error=e.detail), status_code=e.status)
    # 保存したあとは、もとのページに戻れる確認の画面を出す（ブックマークレットから来た人はもとのページを離れている）
    clip_url = conn.execute("SELECT url FROM web_clips WHERE id = ?", (clip_id,)).fetchone()[0]
    return templates.TemplateResponse(request, "clip_saved.html", base_context(
        request, conn, auth, nav="files", url=clip_url, folder_id=folder_id, message=_dup_message(conn, auth, dups),
        doc_id=dups[0]["id"] if dups else None))
