"""ログインとログアウト。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import i18n, security
from ...api.routes import do_login
from ...config import Settings
from ...deps import (
    Auth,
    get_conn,
    get_settings,
    optional_auth,
    require_session,
)
from ...errors import AppError
from ..common import (
    _check_origin,
    _safe_next,
    router,
    templates,
)


@router.get("/lang/{lang}")
def choose_lang(lang: str, next: str | None = None, settings: Settings = Depends(get_settings)):
    """ログイン前の画面の言語の切り替え（この端末の Cookie に覚える。ログイン後は設定の言語が優先）。"""
    from ..common import set_lang_cookie
    resp = RedirectResponse(_safe_next(next), status_code=303)
    set_lang_cookie(resp, settings, lang if lang in i18n.LANGS else "auto")
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, auth: Auth | None = Depends(optional_auth),
               settings: Settings = Depends(get_settings)):
    from ..auth import login_context
    if auth:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", login_context(request, settings, next=next))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 remember: str | None = Form(None), next: str | None = Form(None),
                 conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings)):
    from ..auth import login_context
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
    if auth.session is not None:   # 使っていたセッション（同じ名前の古い Cookie があっても、こちらを消す）
        security.delete_session_by_id(conn, auth.session.id_hash)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp
