"""OIDC（Google など）でのログインと連携。docs/design/extensions.md 5.4。

認可コードフロー + PKCE（S256）。ID トークンの署名は提供者の JWKS で確かめる（joserfc）。
state・nonce・PKCE の値は、署名付きの Cookie（docseed_oidc、10 分）に入れる。
提供者のアクセストークン・リフレッシュトークンは保存しない。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from . import security
from .audit import audit
from .authn import get_auth_settings
from .config import OidcProvider, Settings
from .db import Tx, now
from .errors import AppError, NotFound, ValidationFailed

COOKIE = "docseed_oidc"
COOKIE_PATH = "/auth/oidc"
COOKIE_MAX_AGE = 600
DISCOVERY_TTL = 24 * 3600
CLOCK_SKEW = 120
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "EdDSA"]


class OidcError(AppError):
    status, code, title = 400, "oidc_failed", "外部アカウントでのログインに失敗しました"


def http_client() -> httpx.Client:
    """提供者と通信するクライアント。テストでは差し替える。"""
    return httpx.Client(timeout=10.0, follow_redirects=False)


_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _get_json(url: str) -> dict:
    with http_client() as c:
        r = c.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OidcError(f"提供者から設定を読めませんでした（{url} が HTTP {r.status_code}）")
    return r.json()


def discovery(p: OidcProvider) -> dict:
    """/.well-known/openid-configuration（24 時間キャッシュ）。"""
    key = "disc:" + p.issuer
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        doc = _get_json(p.issuer + "/.well-known/openid-configuration")
    except httpx.HTTPError as e:
        raise OidcError(f"提供者（{p.issuer}）に接続できませんでした: {e}") from None
    if doc.get("issuer", "").rstrip("/") != p.issuer:
        raise OidcError("提供者の設定の issuer が DOCSEED_OIDC_*_ISSUER と一致しません")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(k):
            raise OidcError(f"提供者の設定に {k} がありません")
    with _lock:
        _cache[key] = (time.time() + DISCOVERY_TTL, doc)
    return doc


def _jwks(p: OidcProvider, uri: str, refresh: bool = False) -> KeySet:
    key = "jwks:" + uri
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time() and not refresh:
        return hit[1]
    try:
        ks = KeySet.import_key_set(_get_json(uri))
    except (httpx.HTTPError, JoseError, ValueError) as e:
        raise OidcError(f"提供者の公開鍵を読めませんでした: {e}") from None
    with _lock:
        _cache[key] = (time.time() + DISCOVERY_TTL, ks)
    return ks


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def redirect_uri(settings: Settings, p: OidcProvider) -> str:
    return f"{settings.public_url}/auth/oidc/{p.name}/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def start(settings: Settings, p: OidcProvider, purpose: str, next_url: str | None,
          user_id: int | None) -> tuple[str, str]:
    """(提供者の認可画面の URL, Cookie の値) を返す。"""
    if not settings.public_url:
        raise OidcError("DOCSEED_HOST が設定されていないため、外部アカウントでのログインを使えません")
    doc = discovery(p)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri(settings, p),
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if purpose == "link":
        params["prompt"] = "select_account"
    cookie = security.sign_value(settings.secret_key, "oidc", {
        "p": p.name, "s": state, "n": nonce, "v": verifier, "purpose": purpose, "next": next_url, "uid": user_id,
    }, COOKIE_MAX_AGE)
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return doc["authorization_endpoint"] + sep + urlencode(params), cookie


def callback(settings: Settings, p: OidcProvider, cookie: str | None, state: str | None, code: str | None,
             error: str | None) -> tuple[dict, dict]:
    """戻り先の処理。(Cookie の中身, ID トークンのクレーム) を返す。"""
    data = security.unsign_value(settings.secret_key, "oidc", cookie)
    if data is None or data.get("p") != p.name:
        raise OidcError("時間が経ちすぎたか、別のタブで開始されました。最初からやり直してください")
    if error:
        raise OidcError("提供者の画面で取り消されたか、エラーになりました" + (f"（{error[:100]}）" if error else ""))
    if not state or not secrets.compare_digest(state, data["s"]) or not code:
        raise OidcError("state が一致しません。最初からやり直してください")
    doc = discovery(p)
    try:
        with http_client() as c:
            r = c.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(settings, p),
                "client_id": p.client_id, "client_secret": p.client_secret, "code_verifier": data["v"],
            }, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OidcError(f"提供者に接続できませんでした: {e}") from None
    if r.status_code != 200:
        raise OidcError(f"認可コードを ID トークンに換えられませんでした（HTTP {r.status_code}）")
    id_token = r.json().get("id_token")
    if not id_token:
        raise OidcError("提供者の応答に ID トークンがありません")
    claims = verify_id_token(p, doc, id_token, data["n"])
    return data, claims


def verify_id_token(p: OidcProvider, doc: dict, id_token: str, nonce: str) -> dict:
    try:
        tok = jwt.decode(id_token, _jwks(p, doc["jwks_uri"]), algorithms=ALGORITHMS)
    except JoseError:
        # 鍵の入れ替えに備えて、1 回だけ JWKS を読み直す
        try:
            tok = jwt.decode(id_token, _jwks(p, doc["jwks_uri"], refresh=True), algorithms=ALGORITHMS)
        except JoseError as e:
            raise OidcError(f"ID トークンの署名を確かめられませんでした（{e.__class__.__name__}）") from None
    c = tok.claims
    t = time.time()
    if str(c.get("iss", "")).rstrip("/") != p.issuer:
        raise OidcError("ID トークンの発行者（iss）が違います")
    aud = c.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if p.client_id not in auds or (len(auds) > 1 and c.get("azp") != p.client_id):
        raise OidcError("ID トークンの宛先（aud）が違います")
    if not isinstance(c.get("exp"), int | float) or c["exp"] + CLOCK_SKEW < t:
        raise OidcError("ID トークンの期限が切れています")
    if isinstance(c.get("iat"), int | float) and c["iat"] - CLOCK_SKEW > t:
        raise OidcError("ID トークンの発行時刻が未来です。Pi の時刻を確かめてください")
    if not c.get("nonce") or not secrets.compare_digest(str(c["nonce"]), nonce):
        raise OidcError("ID トークンの nonce が一致しません")
    if not c.get("sub"):
        raise OidcError("ID トークンに sub がありません")
    return c


def _check_domain(conn: sqlite3.Connection, claims: dict) -> None:
    allowed = get_auth_settings(conn).oidc_allowed_domains
    if not allowed:
        return
    email = str(claims.get("email") or "").lower()
    verified = claims.get("email_verified") in (True, "true")
    if not email or not verified or email.rsplit("@", 1)[-1] not in allowed:
        raise OidcError("このメールアドレスのアカウントは連携できません（管理者がドメインを制限しています）")


def login_user(conn: sqlite3.Connection, p: OidcProvider, claims: dict, ip: str | None) -> int:
    """連携済みのユーザーの ID を返す。メールアドレスでアカウントを作ったり結び付けたりはしない。"""
    _check_domain(conn, claims)
    row = conn.execute(
        "SELECT i.id, i.user_id, u.is_active FROM user_identities i JOIN users u ON u.id = i.user_id"
        " WHERE i.provider = ? AND i.subject = ?", (p.name, str(claims["sub"]))).fetchone()
    if row is None:
        with Tx(conn):
            audit(conn, "login_failed", ip=ip, provider=p.name, reason="oidc_not_linked")
        raise OidcError(f"この {p.label} のアカウントは、どのユーザーにも連携されていません。"
                        "パスワードなどでログインしてから、設定の「セキュリティ」で連携してください")
    if not row["is_active"]:
        raise OidcError("このアカウントは無効になっています")
    with Tx(conn):
        conn.execute("UPDATE user_identities SET last_login_at = ?, email = coalesce(?, email) WHERE id = ?",
                     (now(), claims.get("email"), row["id"]))
    return int(row["user_id"])


def link_user(conn: sqlite3.Connection, p: OidcProvider, claims: dict, user_id: int, ip: str | None) -> int:
    _check_domain(conn, claims)
    sub = str(claims["sub"])
    with Tx(conn):
        other = conn.execute("SELECT user_id FROM user_identities WHERE provider = ? AND subject = ?",
                             (p.name, sub)).fetchone()
        if other is not None:
            if other["user_id"] == user_id:
                raise ValidationFailed(f"この {p.label} のアカウントは既に連携しています")
            raise ValidationFailed(f"この {p.label} のアカウントは別のユーザーに連携されています")
        cur = conn.execute("INSERT INTO user_identities(user_id, provider, subject, email, created_at)"
                           " VALUES (?,?,?,?,?)", (user_id, p.name, sub, claims.get("email"), now()))
        iid = int(cur.lastrowid)
        audit(conn, "oidc_link", user_id=user_id, target_type="identity", target_id=iid, ip=ip, provider=p.name)
    return iid


def provider_or_404(settings: Settings, name: str) -> OidcProvider:
    p = settings.oidc_provider(name)
    if p is None:
        raise NotFound("この提供者は設定されていません")
    return p
