"""パスキー（WebAuthn）。docs/design/extensions.md 5.2。py_webauthn を使う。"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from typing import Any

from fastapi import Request
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import authn
from .audit import audit
from .config import Settings
from .db import Tx, now
from .errors import AppError, Unauthenticated, ValidationFailed
from .users import User

RP_NAME = "docseed"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def request_host(request: Request) -> str:
    host = request.headers.get("host", "")
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0].lower() if ":" in host else host.lower()


def unavailable_reason(settings: Settings, request: Request) -> str | None:
    """このリクエストでパスキーを使えないときの説明。使えるなら None。"""
    if not settings.rp_id:
        return ("パスキーは、名前（ドメイン名）で開いたときに使えます。"
                "DOCSEED_HOST（または DOCSEED_RP_ID）を設定してください")
    if request_host(request) != settings.rp_id:
        return f"パスキーは {settings.public_url} で開いたときに使えます"
    return None


def _require_available(settings: Settings, request: Request) -> None:
    reason = unavailable_reason(settings, request)
    if reason:
        raise AppError(reason)


def default_name(user_agent: str | None) -> str:
    ua = user_agent or ""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                      ("Windows", "Windows"), ("CrOS", "Chromebook"), ("Linux", "Linux")):
        if key in ua:
            return name
    return "パスキー"


def _handle(conn: sqlite3.Connection, user_id: int) -> bytes:
    """users.webauthn_handle（32 バイトの乱数）。無ければ作る。"""
    r = conn.execute("SELECT webauthn_handle FROM users WHERE id = ?", (user_id,)).fetchone()
    if r and r[0]:
        return base64url_to_bytes(r[0])
    h = secrets.token_bytes(32)
    with Tx(conn):
        conn.execute("UPDATE users SET webauthn_handle = ? WHERE id = ? AND webauthn_handle IS NULL",
                     (b64url(h), user_id))
    return base64url_to_bytes(conn.execute("SELECT webauthn_handle FROM users WHERE id = ?", (user_id,)).fetchone()[0])


# ---------------------------------------------------------------------------
# 登録
# ---------------------------------------------------------------------------

def registration_options(conn: sqlite3.Connection, settings: Settings, request: Request,
                         user: User) -> dict[str, Any]:
    _require_available(settings, request)
    existing = conn.execute("SELECT credential_id FROM webauthn_credentials WHERE user_id = ?", (user.id,)).fetchall()
    opts = generate_registration_options(
        rp_id=settings.rp_id, rp_name=RP_NAME, user_name=user.username, user_id=_handle(conn, user.id),
        user_display_name=user.display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        attestation=AttestationConveyancePreference.NONE,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(r[0])) for r in existing],
    )
    cid = authn.challenge_create(conn, "passkey_register", user.id, opts.challenge)
    return {"challenge_id": cid, "publicKey": json.loads(options_to_json(opts))}


def register_verify(conn: sqlite3.Connection, settings: Settings, request: Request, user: User,
                    challenge_id: str, credential: dict, name: str | None, ip: str | None) -> int:
    _require_available(settings, request)
    row = authn.challenge_take(conn, challenge_id, "passkey_register")
    if row is None or row["user_id"] != user.id:
        raise ValidationFailed("登録の有効期限が切れました。もう一度お試しください")
    try:
        v = verify_registration_response(
            credential=credential, expected_challenge=bytes(row["challenge"]), expected_rp_id=settings.rp_id,
            expected_origin=settings.public_url, require_user_verification=True)
    except (WebAuthnException, ValueError, KeyError, TypeError) as e:
        raise ValidationFailed(f"パスキーを確認できませんでした（{e}）") from None
    transports = (credential.get("response") or {}).get("transports")
    name = (name or "").strip()[:50] or default_name(request.headers.get("user-agent"))
    with Tx(conn):
        if conn.execute("SELECT 1 FROM webauthn_credentials WHERE credential_id = ?", (v.credential_id,)).fetchone():
            raise ValidationFailed("このパスキーは既に登録されています")
        cur = conn.execute(
            "INSERT INTO webauthn_credentials(user_id, credential_id, public_key, sign_count, transports, backed_up,"
            " aaguid, name, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user.id, v.credential_id, v.credential_public_key, v.sign_count,
             json.dumps(transports) if isinstance(transports, list) else None, int(v.credential_backed_up),
             str(v.aaguid) if v.aaguid else None, name, now()))
        pid = int(cur.lastrowid)
        authn._set_setting(conn, "auth.rp_id", settings.rp_id)
        audit(conn, "passkey_add", user_id=user.id, target_type="passkey", target_id=pid, ip=ip, name=name)
    return pid


# ---------------------------------------------------------------------------
# ログイン・再認証
# ---------------------------------------------------------------------------

def authentication_options(conn: sqlite3.Connection, settings: Settings, request: Request) -> dict[str, Any]:
    """ユーザー名を入れずに選べるパスキーで認証する options（allow_credentials は空）。"""
    _require_available(settings, request)
    opts = generate_authentication_options(rp_id=settings.rp_id,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    cid = authn.challenge_create(conn, "passkey_login", None, opts.challenge)
    return {"challenge_id": cid, "publicKey": json.loads(options_to_json(opts))}


def authenticate(conn: sqlite3.Connection, settings: Settings, request: Request, challenge_id: str,
                 credential: dict, ip: str | None) -> int:
    """パスキーを確かめて、そのユーザーの ID を返す。失敗はログイン試行の制限に数える。"""
    keys = (f"ip:{ip or '-'}",)
    authn.limiter.check(*keys)
    try:
        user_id = _authenticate(conn, settings, request, challenge_id, credential, ip)
    except Unauthenticated:
        authn.limiter.failed(*keys)
        from .ratelimit import security_event
        security_event("passkey_failed", ip)
        raise
    authn.limiter.succeeded(*keys)
    return user_id


def _authenticate(conn: sqlite3.Connection, settings: Settings, request: Request, challenge_id: str,
                  credential: dict, ip: str | None) -> int:
    _require_available(settings, request)
    row = authn.challenge_take(conn, challenge_id, "passkey_login")
    if row is None:
        raise Unauthenticated("時間が経ちすぎました。もう一度お試しください")
    try:
        raw_id = base64url_to_bytes(credential.get("rawId") or credential["id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise Unauthenticated("パスキーを確認できませんでした") from None
    cred = conn.execute(
        "SELECT c.*, u.webauthn_handle, u.is_active FROM webauthn_credentials c JOIN users u ON u.id = c.user_id"
        " WHERE c.credential_id = ?", (raw_id,)).fetchone()
    if cred is None:
        raise Unauthenticated("このパスキーは登録されていません。削除された可能性があります")
    handle = (credential.get("response") or {}).get("userHandle")
    if not handle or handle.rstrip("=") != (cred["webauthn_handle"] or "").rstrip("="):
        raise Unauthenticated("パスキーを確認できませんでした")
    try:
        # sign_count の後退は自前で確かめる（同期されるパスキーは常に 0 なので、両方 0 より大きいときだけ）
        v = verify_authentication_response(
            credential=credential, expected_challenge=bytes(row["challenge"]), expected_rp_id=settings.rp_id,
            expected_origin=settings.public_url, credential_public_key=bytes(cred["public_key"]),
            credential_current_sign_count=0, require_user_verification=True)
    except (WebAuthnException, ValueError, KeyError, TypeError) as e:
        raise Unauthenticated(f"パスキーを確認できませんでした（{e}）") from None
    if cred["sign_count"] > 0 and v.new_sign_count > 0 and v.new_sign_count <= cred["sign_count"]:
        with Tx(conn):
            audit(conn, "passkey_clone_suspected", user_id=cred["user_id"], target_type="passkey",
                  target_id=cred["id"], ip=ip, stored=cred["sign_count"], received=v.new_sign_count)
        raise Unauthenticated("このパスキーは使えません（複製された可能性があります）。管理者に連絡してください")
    if not cred["is_active"]:
        raise Unauthenticated("このアカウントは無効になっています")
    with Tx(conn):
        conn.execute("UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ?, backed_up = ? WHERE id = ?",
                     (v.new_sign_count, now(), int(v.credential_backed_up), cred["id"]))
    return int(cred["user_id"])
