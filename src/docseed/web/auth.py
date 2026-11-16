"""ログイン方法の画面と、Cookie を使う認証の流れ（/auth/*）。docs/design/extensions.md 5 章・8.1 節。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import authn, oidc, passkeys, security, totp
from .. import users as um
from ..api.routes import user_json
from ..audit import audit
from ..config import Settings
from ..db import Tx
from ..deps import Auth, client_ip, get_conn, get_settings, optional_auth, require_admin, require_session
from ..errors import AppError, ReauthRequired, Unauthenticated
from .routes import _check_origin, _safe_next, admin_context, base_context, templates

router = APIRouter(include_in_schema=False)


def _wants_json(request: Request) -> bool:
    return request.headers.get("content-type", "").startswith("application/json")


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise AppError("JSON を送ってください") from None
    if not isinstance(body, dict):
        raise AppError("JSON のオブジェクトを送ってください")
    return body


def login_context(request: Request, settings: Settings, **extra) -> dict:
    ctx = {"request": request, "user": None, "next": None, "error": None, "username": "",
           "passkey_reason": passkeys.unavailable_reason(settings, request),
           "oidc_providers": settings.oidc_providers}
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# 2 段階目（確認コード・復旧コード）
# ---------------------------------------------------------------------------

@router.get("/login/second-factor", response_class=HTMLResponse)
def second_factor_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if authn.pending_second_factor(conn, request) is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login_second_factor.html",
                                      {"request": request, "user": None, "error": None})


@router.post("/auth/second-factor")
async def second_factor_submit(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn),
                               settings: Settings = Depends(get_settings)):
    _check_origin(request)
    if _wants_json(request):
        code = str((await _json_body(request)).get("code") or "")
        res, _ = authn.finish_second_factor(conn, settings, request, response, code)
        return {"user": user_json(res.user), "csrf_token": res.csrf_token}
    form = await request.form()
    resp = RedirectResponse("/", status_code=303)
    try:
        _, nxt = authn.finish_second_factor(conn, settings, request, resp, str(form.get("code") or ""))
    except AppError as e:
        if authn.pending_second_factor(conn, request) is None:
            return templates.TemplateResponse(request, "login.html", login_context(request, settings, error=e.detail),
                                              status_code=e.status)
        return templates.TemplateResponse(request, "login_second_factor.html",
                                          {"request": request, "user": None, "error": e.detail}, status_code=e.status)
    resp.headers["location"] = _safe_next(nxt)
    return resp


# ---------------------------------------------------------------------------
# パスキーでのログイン
# ---------------------------------------------------------------------------

@router.post("/auth/passkeys/login/options")
def passkey_login_options(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                          settings: Settings = Depends(get_settings)):
    _check_origin(request)
    return passkeys.authentication_options(conn, settings, request)


@router.post("/auth/passkeys/login/verify")
async def passkey_login_verify(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn),
                               settings: Settings = Depends(get_settings)):
    _check_origin(request)
    body = await _json_body(request)
    cred = body.get("credential")
    if not isinstance(cred, dict):
        raise AppError("credential がありません")
    uid = passkeys.authenticate(conn, settings, request, str(body.get("challenge_id") or ""), cred,
                                client_ip(request))
    csrf = authn.start_session(conn, settings, request, response, uid, "passkey", bool(body.get("remember")))
    return {"user": user_json(um.get_user(conn, uid)), "csrf_token": csrf,
            "redirect": _safe_next(body.get("next") if isinstance(body.get("next"), str) else None)}


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------

@router.get("/auth/oidc/{provider}/start")
def oidc_start(provider: str, request: Request, purpose: str = "login", next: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
               auth: Auth | None = Depends(optional_auth)):
    p = oidc.provider_or_404(settings, provider)
    if purpose not in ("login", "link"):
        raise AppError("purpose は login か link です")
    user_id = None
    if purpose == "link":
        if auth is None or auth.kind != "session":
            raise Unauthenticated()
        if not auth.session.recently_authenticated():
            return RedirectResponse("/settings/security?reauth=1", status_code=303)
        user_id = auth.user.id
    url, cookie = oidc.start(settings, p, purpose, _safe_next(next) if next else None, user_id)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(oidc.COOKIE, cookie, max_age=oidc.COOKIE_MAX_AGE, httponly=True, secure=settings.cookie_secure,
                    samesite="lax", path=oidc.COOKIE_PATH)
    return resp


@router.get("/auth/oidc/{provider}/callback", response_class=HTMLResponse)
def oidc_callback(provider: str, request: Request, state: str | None = None, code: str | None = None,
                  error: str | None = None, conn: sqlite3.Connection = Depends(get_conn),
                  settings: Settings = Depends(get_settings), auth: Auth | None = Depends(optional_auth)):
    p = oidc.provider_or_404(settings, provider)
    ip = client_ip(request)
    purpose = "login"
    try:
        # 同じ名前の Cookie が複数届いたら、署名の合うものを先から（security.cookie_values）
        cookie = next((v for v in security.cookie_values(request, oidc.COOKIE)
                       if security.unsign_value(settings.secret_key, "oidc", v) is not None), None)
        data, claims = oidc.callback(settings, p, cookie, state, code, error)
        purpose = data.get("purpose", "login")
        if purpose == "link":
            # 開始したときと同じユーザーがログインしたままで、再認証が有効なこと
            if auth is None or auth.kind != "session" or auth.user.id != data.get("uid"):
                raise Unauthenticated("ログインし直してから、もう一度連携してください")
            authn.require_recent_auth(auth.session)
            oidc.link_user(conn, p, claims, auth.user.id, ip)
            resp = RedirectResponse("/settings/security?done=identity_linked", status_code=303)
        else:
            uid = oidc.login_user(conn, p, claims, ip)
            resp = RedirectResponse(_safe_next(data.get("next")), status_code=303)
            authn.start_session(conn, settings, request, resp, uid, f"oidc:{p.name}", False)
    except AppError as e:
        if purpose == "link" and auth is not None:
            r = templates.TemplateResponse(request, "security.html", security_context(
                request, conn, settings, auth, error=e.detail), status_code=e.status)
        else:
            r = templates.TemplateResponse(request, "login.html", login_context(request, settings, error=e.detail),
                                           status_code=e.status)
        r.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH)
        return r
    resp.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH)
    return resp


# ---------------------------------------------------------------------------
# 再認証
# ---------------------------------------------------------------------------

@router.post("/auth/reauth")
async def reauth(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    ip = client_ip(request)
    if _wants_json(request):
        body = await _json_body(request)
        if isinstance(body.get("credential"), dict):
            uid = passkeys.authenticate(conn, settings, request, str(body.get("challenge_id") or ""),
                                        body["credential"], ip)
            if uid != auth.user.id:
                raise AppError("ログイン中のユーザーのパスキーではありません")
            security.mark_reauthenticated(conn, auth.session)
        else:
            authn.reauth_with_password(conn, auth.user, auth.session, str(body.get("password") or ""),
                                       str(body.get("code") or ""), ip)
        return Response(status_code=204)
    form = await request.form()
    try:
        authn.reauth_with_password(conn, auth.user, auth.session, str(form.get("password") or ""),
                                   str(form.get("code") or ""), ip)
    except AppError as e:
        return templates.TemplateResponse(request, "security.html",
                                          security_context(request, conn, settings, auth, error=e.detail),
                                          status_code=e.status)
    return RedirectResponse("/settings/security?done=reauthed", status_code=303)


# ---------------------------------------------------------------------------
# 設定 → セキュリティ
# ---------------------------------------------------------------------------

def security_context(request: Request, conn: sqlite3.Connection, settings: Settings, auth: Auth, **extra) -> dict:
    summary = authn.security_summary(conn, settings, auth.user.id, auth.session.id_hash if auth.session else None)
    linked = {i["provider"] for i in summary["identities"]}
    ctx = base_context(
        request, conn, auth, nav="settings", sec=summary, recent=auth.session.recently_authenticated(),
        has_totp=summary["totp"]["enabled"], passkey_reason=passkeys.unavailable_reason(settings, request),
        providers=[{"name": p.name, "label": p.label, "linked": p.name in linked} for p in settings.oidc_providers],
        setup_required=authn.needs_second_factor_setup(conn, auth.user, auth.session),
        require_2fa=authn.get_auth_settings(conn).require_2fa,
        enroll=None, recovery_codes=None, error=None)
    if request.query_params.get("reauth") and not ctx["recent"]:
        ctx["error"] = "この操作の前に、もう一度本人確認をしてください"
    ctx.update(extra)
    return ctx


def _render_security(request: Request, conn: sqlite3.Connection, settings: Settings, auth: Auth,
                     status: int = 200, **extra) -> HTMLResponse:
    return templates.TemplateResponse(request, "security.html",
                                      security_context(request, conn, settings, auth, **extra), status_code=status)


def _guarded(request: Request, conn: sqlite3.Connection, settings: Settings, auth: Auth, fn, done: str | None = None):
    """再認証が必要な操作を行い、エラーなら画面にメッセージを出す。"""
    try:
        authn.require_recent_auth(auth.session)
        out = fn()
    except ReauthRequired as e:
        return _render_security(request, conn, settings, auth, status=e.status, error=e.detail)
    except AppError as e:
        return _render_security(request, conn, settings, auth, status=e.status, error=e.detail)
    if done:
        return RedirectResponse(f"/settings/security?done={done}", status_code=303)
    return out


@router.get("/settings/security", response_class=HTMLResponse)
def security_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    return _render_security(request, conn, settings, auth)


@router.post("/settings/security/totp", response_class=HTMLResponse)
def security_totp_start(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    def run():
        eid, secret = authn.totp_begin(conn, auth.user)
        enroll = {"id": eid, "secret": totp.format_secret(secret),
                  "qr": totp.qr_data_url(totp.otpauth_uri(secret, auth.user.username))}
        return _render_security(request, conn, settings, auth, enroll=enroll)
    return _guarded(request, conn, settings, auth, run)


@router.post("/settings/security/totp/confirm", response_class=HTMLResponse)
def security_totp_confirm(request: Request, enroll_id: str = Form(""), code: str = Form(""),
                          conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                          auth: Auth = Depends(require_session)):
    try:
        codes = authn.totp_confirm(conn, auth.user, enroll_id, code, client_ip(request))
    except AppError as e:
        secret = authn.totp_pending_secret(conn, auth.user, enroll_id)
        enroll = None
        if secret:
            enroll = {"id": enroll_id, "secret": totp.format_secret(secret),
                      "qr": totp.qr_data_url(totp.otpauth_uri(secret, auth.user.username))}
        return _render_security(request, conn, settings, auth, status=e.status, error=e.detail, enroll=enroll)
    return _render_security(request, conn, settings, auth, recovery_codes=codes,
                            done="確認コードを有効にしました")


@router.post("/settings/security/totp/disable", response_class=HTMLResponse)
def security_totp_disable(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                          settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    return _guarded(request, conn, settings, auth, lambda: authn.totp_disable(conn, auth.user, client_ip(request)),
                    done="totp_disabled")


@router.post("/settings/security/recovery-codes", response_class=HTMLResponse)
def security_recovery(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                      settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    def run():
        codes = authn.regenerate_recovery_codes(conn, auth.user, client_ip(request))
        return _render_security(request, conn, settings, auth, recovery_codes=codes,
                                done="復旧コードを作り直しました。前のコードは使えません")
    return _guarded(request, conn, settings, auth, run)


@router.post("/settings/security/passkeys/{passkey_id}/delete", response_class=HTMLResponse)
def security_passkey_delete(passkey_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                            settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    return _guarded(request, conn, settings, auth,
                    lambda: authn.remove_passkey(conn, auth.user, passkey_id, client_ip(request)),
                    done="passkey_removed")


@router.post("/settings/security/identities/{identity_id}/delete", response_class=HTMLResponse)
def security_identity_delete(identity_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                             settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    return _guarded(request, conn, settings, auth,
                    lambda: authn.remove_identity(conn, auth.user, identity_id, client_ip(request)),
                    done="identity_removed")


@router.post("/settings/security/password-login", response_class=HTMLResponse)
def security_password_login(request: Request, enabled: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                            settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    on = enabled == "1"
    return _guarded(request, conn, settings, auth,
                    lambda: authn.set_password_login(conn, auth.user, on, client_ip(request)),
                    done="password_login_on" if on else "password_login_off")


@router.post("/settings/security/sessions/revoke-others")
def security_revoke_sessions(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                             auth: Auth = Depends(require_session)):
    security.delete_other_sessions(conn, auth.user.id, auth.session.id_hash)
    with Tx(conn):
        audit(conn, "sessions_revoke", user_id=auth.user.id, ip=client_ip(request))
    return RedirectResponse("/settings/security?done=sessions_revoked", status_code=303)


# ---------------------------------------------------------------------------
# 管理: ログイン方法のリセットと 2 段階認証の必須
# ---------------------------------------------------------------------------

@router.post("/admin/users/{user_id}/reset-auth", response_class=HTMLResponse)
def admin_reset_auth(user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     settings: Settings = Depends(get_settings), auth: Auth = Depends(require_admin)):
    temp = authn.reset_auth(conn, user_id, actor_id=auth.user.id, ip=client_ip(request))
    target = um.get_user(conn, user_id)
    if user_id == auth.user.id:
        # 自分をリセットした場合はセッションも消えているので、ログイン画面へ
        msg = f"ログイン方法をリセットしました。仮のパスワード: {temp}"
        resp = templates.TemplateResponse(request, "login.html", login_context(
            request, settings, error=msg, username=target.username))
        resp.delete_cookie(security.SESSION_COOKIE, path="/")
        return resp
    return templates.TemplateResponse(request, "admin.html", admin_context(
        request, conn, auth, settings, reset_done={"username": target.username, "password": temp}))


@router.post("/admin/auth-settings")
def admin_auth_settings(request: Request, require_2fa: str | None = Form(None), oidc_allowed_domains: str = Form(""),
                        conn: sqlite3.Connection = Depends(get_conn),
                        settings: Settings = Depends(get_settings), auth: Auth = Depends(require_admin)):
    try:
        authn.set_auth_settings(conn, require_2fa=bool(require_2fa), oidc_allowed_domains=oidc_allowed_domains)
        with Tx(conn):
            audit(conn, "auth_settings_change", user_id=auth.user.id, ip=client_ip(request),
                  require_2fa=bool(require_2fa), oidc_allowed_domains=oidc_allowed_domains[:200])
    except AppError as e:
        return templates.TemplateResponse(request, "admin.html",
                                          admin_context(request, conn, auth, settings) | {"error": e.detail},
                                          status_code=e.status)
    return RedirectResponse("/admin?done=auth_settings", status_code=303)
