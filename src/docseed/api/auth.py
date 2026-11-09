"""自分のログイン方法と、ログインの管理（/api/v1）。docs/design/extensions.md 8.2・8.3。

すべてセッションでだけ使える（API トークンでは使えない）。変更には再認証（5.6 節）が要る。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .. import authn, passkeys, totp
from .. import users as um
from ..audit import audit
from ..config import Settings
from ..db import Tx
from ..deps import Auth, client_ip, get_conn, get_settings, require_admin, require_session

router = APIRouter(prefix="/api/v1")


def _recent(auth: Auth) -> None:
    authn.require_recent_auth(auth.session)


@router.get("/me/security")
def api_security(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)) -> dict[str, Any]:
    out = authn.security_summary(conn, settings, auth.user.id, auth.session.id_hash)
    out.update({
        "reauth_valid": auth.session.recently_authenticated(),
        "require_2fa": authn.get_auth_settings(conn).require_2fa,
        "second_factor_setup_required": authn.needs_second_factor_setup(conn, auth.user, auth.session),
        "passkey_unavailable_reason": passkeys.unavailable_reason(settings, request),
        "oidc_providers": [{"name": p.name, "label": p.label} for p in settings.oidc_providers],
    })
    return out


class SecurityPatch(BaseModel):
    password_login: bool


@router.patch("/me/security", status_code=204)
def api_security_patch(body: SecurityPatch, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_session)):
    _recent(auth)
    authn.set_password_login(conn, auth.user, body.password_login, client_ip(request))


# ---- パスキー ---------------------------------------------------------------

@router.post("/me/passkeys/register/options")
def api_passkey_options(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    _recent(auth)
    return passkeys.registration_options(conn, settings, request, auth.user)


class PasskeyRegisterIn(BaseModel):
    challenge_id: str
    credential: dict[str, Any]
    name: str | None = None


@router.post("/me/passkeys/register/verify", status_code=201)
def api_passkey_verify(body: PasskeyRegisterIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       settings: Settings = Depends(get_settings), auth: Auth = Depends(require_session)):
    _recent(auth)
    pid = passkeys.register_verify(conn, settings, request, auth.user, body.challenge_id, body.credential, body.name,
                                   client_ip(request))
    return {"id": pid}


class PasskeyPatch(BaseModel):
    name: str


@router.patch("/me/passkeys/{passkey_id}", status_code=204)
def api_passkey_rename(passkey_id: int, body: PasskeyPatch, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_session)):
    authn.rename_passkey(conn, auth.user, passkey_id, body.name)


@router.delete("/me/passkeys/{passkey_id}", status_code=204)
def api_passkey_delete(passkey_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_session)):
    _recent(auth)
    authn.remove_passkey(conn, auth.user, passkey_id, client_ip(request))


# ---- 確認コード（TOTP）と復旧コード -------------------------------------------

@router.post("/me/totp", status_code=201)
def api_totp_begin(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    _recent(auth)
    eid, secret = authn.totp_begin(conn, auth.user)
    uri = totp.otpauth_uri(secret, auth.user.username)
    return {"enroll_id": eid, "secret": secret, "otpauth_uri": uri, "qr_svg": totp.qr_data_url(uri)}


class TotpConfirmIn(BaseModel):
    enroll_id: str
    code: str


@router.post("/me/totp/confirm")
def api_totp_confirm(body: TotpConfirmIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    return {"recovery_codes": authn.totp_confirm(conn, auth.user, body.enroll_id, body.code, client_ip(request))}


@router.delete("/me/totp", status_code=204)
def api_totp_disable(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_session)):
    _recent(auth)
    authn.totp_disable(conn, auth.user, client_ip(request))


@router.post("/me/recovery-codes")
def api_recovery_codes(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                       auth: Auth = Depends(require_session)):
    _recent(auth)
    return {"recovery_codes": authn.regenerate_recovery_codes(conn, auth.user, client_ip(request))}


# ---- OIDC の連携 -------------------------------------------------------------

@router.delete("/me/identities/{identity_id}", status_code=204)
def api_identity_delete(identity_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_session)):
    _recent(auth)
    authn.remove_identity(conn, auth.user, identity_id, client_ip(request))


# ---- 管理 --------------------------------------------------------------------

@router.post("/admin/users/{user_id}/reset-auth")
def api_reset_auth(user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_admin)):
    temp = authn.reset_auth(conn, user_id, actor_id=auth.user.id, ip=client_ip(request))
    return {"user_id": um.get_user(conn, user_id).id, "temporary_password": temp}


def _auth_settings_json(s: authn.AuthSettings) -> dict[str, Any]:
    return {"require_2fa": s.require_2fa, "oidc_allowed_domains": list(s.oidc_allowed_domains)}


@router.get("/admin/settings")
def api_admin_settings(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    return _auth_settings_json(authn.get_auth_settings(conn))


class AdminSettingsPatch(BaseModel):
    require_2fa: bool | None = None
    oidc_allowed_domains: list[str] | None = None


@router.patch("/admin/settings")
def api_admin_settings_patch(body: AdminSettingsPatch, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                             auth: Auth = Depends(require_admin)):
    s = authn.set_auth_settings(conn, require_2fa=body.require_2fa, oidc_allowed_domains=body.oidc_allowed_domains)
    with Tx(conn):
        audit(conn, "auth_settings_change", user_id=auth.user.id, ip=client_ip(request),
              **_auth_settings_json(s))
    return _auth_settings_json(s)
