"""設定（プロフィール・パスワード・API トークン・画面の色・容量とゴミ箱・整理の補助）。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import security
from ... import users as um
from ...api.routes import change_password, revoke_token, token_rows
from ...audit import audit
from ...config import Settings
from ...db import Tx
from ...deps import (
    Auth,
    client_ip,
    get_conn,
    require_session,
)
from ...errors import AppError
from ...urls import public_root
from ..common import (
    base_context,
    router,
    templates,
)


@router.post("/settings/theme")
def settings_theme(theme: str = Form("auto"), conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_session)):
    from ...personal import set_theme
    set_theme(conn, auth.user, theme)
    return RedirectResponse("/settings?done=theme#theme", status_code=303)


@router.post("/settings/prefs")
async def settings_prefs(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                         auth: Auth = Depends(require_session)):
    from ...suggest import PREFS, set_prefs
    form = await request.form()
    set_prefs(conn, auth.user.id, {k: form.get(k) == "1" for k in PREFS})
    return RedirectResponse("/settings?done=prefs", status_code=303)


def _prefs(conn: sqlite3.Connection, user_id: int) -> dict:
    from ...suggest import get_prefs
    return get_prefs(conn, user_id)


def settings_context(request: Request, conn: sqlite3.Connection, auth: Auth, **extra) -> dict:
    from ...shares import list_shares
    settings = request.app.state.settings
    base = settings.public_base or public_root(request)
    bookmarklet = ("javascript:location.href='" + base + "/clip?url='+encodeURIComponent(location.href)"
                   if settings.webclip_enabled else None)
    return base_context(request, conn, auth, nav="settings", tokens=token_rows(conn, auth.user.id),
                        my_shares=list_shares(conn, auth.user), base_url=public_root(request),
                        bookmarklet=bookmarklet, prefs=_prefs(conn, auth.user.id),
                        suggest_enabled=settings.suggest_enabled,
                        llm_enabled=settings.llm_enabled, retention=_retention_ctx(conn, auth, settings),
                        new_token=None, error=None, **extra)


def _retention_ctx(conn: sqlite3.Connection, auth: Auth, settings: Settings) -> dict:
    from ... import limits
    return {"days": limits.trash_retention(conn, auth.user.id, settings.trash_retention_days),
            "own": limits.has_own_retention(conn, auth.user.id), "default": settings.trash_retention_days,
            "choices": limits.RETENTION_CHOICES}


@router.post("/settings/trash-retention")
def settings_trash_retention(days: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                             auth: Auth = Depends(require_session)):
    from ...limits import set_trash_retention
    set_trash_retention(conn, auth.user.id, int(days) if days.isdigit() else None)
    return RedirectResponse("/settings?done=trash_retention#storage", status_code=303)


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
