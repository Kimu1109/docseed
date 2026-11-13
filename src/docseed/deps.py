"""リクエストごとの DB 接続と認証（FastAPI の依存関係）。docs/design/security.md 2 章。"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request

from . import security
from .config import Settings
from .db import connect
from .errors import CsrfFailed, Forbidden, SecondFactorSetupRequired, Unauthenticated
from .storage import BlobStore
from .users import User, get_user

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> BlobStore:
    return request.app.state.store


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@dataclass
class Auth:
    user: User
    kind: str                         # "session" | "token"
    session: security.Session | None = None
    scopes: frozenset[str] = frozenset({"read", "write"})


async def optional_auth(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Auth | None:
    settings: Settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        row = security.load_api_token(conn, header[7:].strip())
        if row is None:
            raise Unauthenticated("API トークンが無効か期限切れです")
        auth = Auth(get_user(conn, row["user_id"]), "token", scopes=frozenset(row["scopes"].split(",")))
        need = "read" if request.method in SAFE_METHODS else "write"
        if need not in auth.scopes:
            raise Forbidden(f"この API トークンには {need} の権限がありません")
        return auth

    raw = request.cookies.get(security.SESSION_COOKIE)
    if not raw:
        return None
    sess = security.load_session(conn, raw, remember_days=settings.session_days)
    if sess is None:
        return None
    auth = Auth(get_user(conn, sess.user_id), "session", session=sess)
    if request.method not in SAFE_METHODS:
        await _check_csrf(request, sess.csrf_token)
    request.state.csrf_token = sess.csrf_token
    return auth


async def _check_csrf(request: Request, expected: str) -> None:
    token = request.headers.get("x-csrf-token")
    if token is None:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            v = form.get("csrf_token")
            token = v if isinstance(v, str) else None
    if not token or not hmac.compare_digest(token, expected):
        raise CsrfFailed()


# 2 段階認証が必須で未設定のときにも使える画面・API（設定・ログアウト）
SETUP_ALLOWED_PREFIXES = ("/settings/security", "/auth/", "/logout", "/api/v1/auth/", "/api/v1/me/security",
                          "/api/v1/me/totp", "/api/v1/me/passkeys", "/api/v1/me/recovery-codes")


async def require_auth(request: Request, auth: Auth | None = Depends(optional_auth),
                       conn: sqlite3.Connection = Depends(get_conn)) -> Auth:
    if auth is None:
        raise Unauthenticated()
    if auth.kind == "session" and auth.session is not None and auth.session.auth_method == "password" \
            and not request.url.path.startswith(SETUP_ALLOWED_PREFIXES):
        from .authn import needs_second_factor_setup
        if needs_second_factor_setup(conn, auth.user, auth.session):
            raise SecondFactorSetupRequired()
    return auth


async def require_session(auth: Auth = Depends(require_auth)) -> Auth:
    """API トークンでは行えない操作（トークン発行・ユーザー管理など）。"""
    if auth.kind != "session":
        raise Forbidden("この操作は API トークンでは行えません。ブラウザからログインして行ってください")
    return auth


async def require_admin_read(auth: Auth = Depends(require_auth)) -> Auth:
    """管理者の読み取りだけの API（状態・指標）。外部の監視から、管理者の API トークン（read）でも呼べる。"""
    if not auth.user.is_admin:
        raise Forbidden("管理者だけが行える操作です")
    if auth.kind == "token" and "read" not in auth.scopes:
        raise Forbidden("この API トークンには read の権限がありません")
    return auth


async def require_admin(auth: Auth = Depends(require_session)) -> Auth:
    if not auth.user.is_admin:
        raise Forbidden("管理者だけが行える操作です")
    return auth
