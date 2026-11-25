"""Phase 7: パスキー・確認コード（TOTP）・復旧コード・再認証・OIDC・リセット。docs/design/extensions.md 10 章。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlparse

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from docseed import oidc, totp
from docseed.api import routes as api_routes
from docseed.config import OidcProvider, Settings
from tests.conftest import PASSWORD

HOST = "docseed.test"
ORIGIN = f"https://{HOST}"
IDP = "https://idp.example"


@pytest.fixture(autouse=True)
def _clear_limits():
    api_routes.limiter._fail.clear()
    oidc.clear_cache()
    yield
    api_routes.limiter._fail.clear()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", secret_key="x" * 40, cookie_secure=False, host=HOST,
                    oidc_providers=(OidcProvider("test", IDP, "client-1", "client-secret", "テスト IdP"),))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Browser:
    """https://docseed.test で開いたブラウザ。"""

    def __init__(self, app):
        self.http = TestClient(app, base_url=ORIGIN)
        self.csrf = ""
        self.totp_used = ""

    def login(self, username="alice", password=PASSWORD, **kw):
        r = self.http.post("/api/v1/auth/login", json={"username": username, "password": password, **kw})
        if r.status_code == 200 and "csrf_token" in r.json():
            self.csrf = r.json()["csrf_token"]
        return r

    def second_factor(self, code):
        r = self.http.post("/auth/second-factor", json={"code": code})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r

    def post(self, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return self.http.post(url, **kw)

    def patch(self, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return self.http.patch(url, **kw)

    def delete(self, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return self.http.delete(url, **kw)

    def get(self, url, **kw):
        return self.http.get(url, **kw)


def expire_reauth(conn, username="alice"):
    conn.execute("UPDATE sessions SET reauth_at = '2000-01-01T00:00:00Z' WHERE user_id ="
                 " (SELECT id FROM users WHERE username = ?)", (username,))


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

def test_totp_rfc6238_vectors():
    # RFC 6238 付録 B（SHA-1、8 桁、鍵 "12345678901234567890"）
    key = b"12345678901234567890"
    for t, want in [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")]:
        assert totp.hotp(key, t // 30, digits=8) == want
    secret = base64.b32encode(key).decode()
    assert totp.code_at(secret, 59) == "287082"


def test_totp_window_and_reuse():
    s = totp.new_secret()
    t = 1_700_000_000
    code = totp.code_at(s, t)
    step = totp.match(s, code, last_step=None, t=t + 30)        # 1 つ後の時間でも通る
    assert step == t // 30
    assert totp.match(s, code, last_step=None, t=t + 61) is None  # 2 つ後は通らない
    assert totp.match(s, code, last_step=step, t=t) is None      # 使用済み
    assert totp.match(s, "12345", last_step=None, t=t) is None


def test_recovery_code_format():
    codes = totp.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    for c in codes:
        assert len(c) == 14 and c[4] == "-" and c[9] == "-"
        assert totp.normalize_recovery(c.upper().replace("-", " ")) == c


def enable_totp(b: Browser) -> tuple[str, list[str]]:
    r = b.post("/api/v1/me/totp")
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert r.json()["qr_svg"].startswith("data:image/svg+xml")
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/docseed%3Aalice?")
    bad = b.post("/api/v1/me/totp/confirm", json={"enroll_id": r.json()["enroll_id"], "code": "000000"})
    assert bad.status_code == 422
    b.totp_used = totp.code_at(secret)       # 30 秒の境目をまたいでも、使ったコードそのものを後で試せるように
    r = b.post("/api/v1/me/totp/confirm", json={"enroll_id": r.json()["enroll_id"], "code": b.totp_used})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


def test_login_with_totp_and_recovery(app, users, conn):
    b = Browser(app)
    b.login()
    secret, codes = enable_totp(b)
    assert len(codes) == 10

    c = Browser(app)
    r = c.login()
    assert r.json() == {"second_factor_required": True, "methods": ["totp", "recovery"]}
    assert c.get("/api/v1/me").status_code == 401                  # まだセッションは無い
    assert c.second_factor("000000").status_code == 422
    # 有効化のときに使ったコードはもう使えない（同じ時間の番号）
    assert c.second_factor(b.totp_used).status_code == 422
    r = c.second_factor(codes[0])
    assert r.status_code == 200 and r.json()["user"]["username"] == "alice"
    assert c.get("/api/v1/me").status_code == 200
    sess = conn.execute("SELECT auth_method FROM sessions ORDER BY created_at DESC, rowid DESC").fetchone()
    assert sess["auth_method"] == "password+recovery"

    # 復旧コードは 1 回だけ
    d = Browser(app)
    d.login()
    assert d.second_factor(codes[0]).status_code == 422
    assert d.second_factor(codes[1].upper()).status_code == 200
    assert b.get("/api/v1/me/security").json()["recovery_codes_remaining"] == 8
    actions = {r[0] for r in conn.execute("SELECT action FROM audit_log")}
    assert {"totp_enable", "recovery_code_used", "second_factor_failed"} <= actions


def test_second_factor_counts_toward_rate_limit(app, users):
    b = Browser(app)
    b.login()
    enable_totp(b)
    c = Browser(app)
    c.login()
    for _ in range(5):
        c.second_factor("111111")
    assert c.second_factor("222222").status_code == 429
    assert Browser(app).login().status_code == 429                  # パスワードの試行も止まる


def test_second_factor_web_flow(app, users):
    b = Browser(app)
    b.login()
    secret, codes = enable_totp(b)
    w = TestClient(app, base_url=ORIGIN)
    r = w.post("/login", data={"username": "alice", "password": PASSWORD, "next": "/trash"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login/second-factor"
    assert "確認コード" in w.get("/login/second-factor").text
    r = w.post("/auth/second-factor", data={"code": "999999"}, follow_redirects=False)
    assert r.status_code == 422 and "確認コードが違います" in r.text
    r = w.post("/auth/second-factor", data={"code": codes[3]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/trash"
    assert w.get("/trash").status_code == 200


def test_second_factor_next_rejects_control_chars(app, users):
    """「/<タブ>/evil.example」はブラウザが「//evil.example」と読むので、戻り先にしない（security.md 2.2）。"""
    b = Browser(app)
    b.login()
    _, codes = enable_totp(b)
    for bad in ("/\t/evil.example", "/\n/evil.example", "/\x00/evil.example"):
        w = TestClient(app, base_url=ORIGIN)
        w.post("/login", data={"username": "alice", "password": PASSWORD, "next": bad}, follow_redirects=False)
        r = w.post("/auth/second-factor", data={"code": codes.pop()}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# 再認証・パスワードでのログインの切り替え・2 段階認証の必須
# ---------------------------------------------------------------------------

def test_reauth_required(app, users, conn):
    b = Browser(app)
    b.login()
    expire_reauth(conn)
    r = b.post("/api/v1/me/totp")
    assert r.status_code == 403 and r.json()["code"] == "reauth_required"
    assert b.post("/auth/reauth", json={"password": "wrong-password-x"}).status_code == 422
    assert b.post("/auth/reauth", json={"password": PASSWORD}).status_code == 204
    assert b.post("/api/v1/me/totp").status_code == 201
    # 画面: 本人確認が済んでいないとボタンを押せず、操作もエラーになる
    expire_reauth(conn)
    page = b.get("/settings/security").text
    assert "もう一度本人確認" in page
    r = b.post("/settings/security/totp", data={"csrf_token": b.csrf})
    assert r.status_code == 403
    r = b.post("/auth/reauth", data={"csrf_token": b.csrf, "password": "wrong-password-x"})
    assert r.status_code == 422 and "パスワードまたは確認コードが違います" in r.text
    r = b.post("/auth/reauth", data={"csrf_token": b.csrf, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/security?done=reauthed"
    assert 'name="enroll_id"' in b.post("/settings/security/totp", data={"csrf_token": b.csrf}).text


def test_reauth_with_totp_needs_code(app, users, conn):
    b = Browser(app)
    b.login()
    secret, codes = enable_totp(b)
    expire_reauth(conn)
    assert b.post("/auth/reauth", json={"password": PASSWORD}).status_code == 422
    assert b.post("/auth/reauth", json={"password": PASSWORD, "code": codes[0]}).status_code == 204


def test_password_login_toggle(app, users, conn):
    b = Browser(app)
    b.login()
    r = b.patch("/api/v1/me/security", json={"password_login": False})
    assert r.status_code == 422                                     # パスキーも連携も無い
    conn.execute("INSERT INTO user_identities(user_id, provider, subject, created_at) VALUES (?, 'test', 's1', 'x')",
                 (users["alice"].id,))
    assert b.patch("/api/v1/me/security", json={"password_login": False}).status_code == 204
    r = Browser(app).login()
    assert r.status_code == 401 and "パスワードでのログインを使わない" in r.json()["detail"]
    # パスワードが合ったときの案内も失敗として数える（数えないと、パスワードの正誤を何度でも試せる）
    for _ in range(4):
        Browser(app).login()
    assert Browser(app).login().status_code == 429
    api_routes.limiter._fail.clear()
    # 最後の 1 つは消せない
    iid = b.get("/api/v1/me/security").json()["identities"][0]["id"]
    assert b.delete(f"/api/v1/me/identities/{iid}").status_code == 422
    assert b.patch("/api/v1/me/security", json={"password_login": True}).status_code == 204
    assert b.delete(f"/api/v1/me/identities/{iid}").status_code == 204
    assert Browser(app).login().status_code == 200


def test_require_2fa(app, users, conn):
    admin = Browser(app)
    admin.login("admin")
    token = admin.post("/api/v1/me/tokens", json={"name": "cli"}).json()["token"]
    r = admin.patch("/api/v1/admin/settings", json={"require_2fa": True})
    assert r.status_code == 200 and r.json()["require_2fa"] is True
    assert Browser(app).login("bob").status_code == 200
    # 既存のセッションも含め、設定するまでほかの画面・API は使えない
    assert admin.get("/api/v1/folders/shared-with-me").json()["code"] == "second_factor_setup_required"
    b = Browser(app)
    b.login()
    r = b.get("/trash", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/security?setup=1"
    assert b.get("/settings/security").status_code == 200
    assert b.get("/api/v1/me/security").json()["second_factor_setup_required"] is True
    enable_totp(b)
    assert b.get("/trash").status_code == 200
    # API トークンは対象外
    assert admin.get("/api/v1/me").status_code == 403
    assert TestClient(app).get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_admin_reset_auth(app, users, conn):
    b = Browser(app)
    b.login()
    enable_totp(b)
    b.post("/api/v1/me/tokens", json={"name": "cli"})
    admin = Browser(app)
    admin.login("admin")
    assert Browser(app).login().json().get("second_factor_required")
    r = admin.post(f"/api/v1/admin/users/{users['alice'].id}/reset-auth")
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert b.get("/api/v1/me").status_code == 401                   # セッションは消える
    assert conn.execute("SELECT count(*) FROM api_tokens WHERE revoked_at IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM user_totp").fetchone()[0] == 0
    assert Browser(app).login(password=temp).status_code == 200    # 2 段階目なしでログインできる
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action = 'auth_reset'").fetchone()[0] == 1
    assert Browser(app).login("alice").status_code == 401           # 元のパスワードは使えない


def test_cli_reset_auth(app, users, settings, monkeypatch, capsys):
    from docseed import cli
    monkeypatch.setenv("DOCSEED_DATA_DIR", str(settings.data_dir))
    cli.main(["reset-auth", "--username", "admin"])
    out = capsys.readouterr().out
    temp = out.split("仮のパスワード: ")[1].split()[0]
    assert Browser(app).login("admin", temp).status_code == 200


def test_security_page_and_sessions(app, users):
    a = Browser(app)
    a.login()
    b = Browser(app)
    b.login()
    page = a.get("/settings/security").text
    assert "ログイン中の端末" in page and "テスト IdP と連携" in page and "パスキーを追加" in page
    r = a.post("/settings/security/sessions/revoke-others", data={"csrf_token": a.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert b.get("/api/v1/me").status_code == 401 and a.get("/api/v1/me").status_code == 200


def test_settings_web_totp(app, users):
    b = Browser(app)
    b.login()
    page = b.post("/settings/security/totp", data={"csrf_token": b.csrf}).text
    assert 'src="data:image/svg+xml' in page
    eid = page.split('name="enroll_id" value="')[1].split('"')[0]
    secret = page.split("<code data-select-all>")[1].split("</code>")[0].replace(" ", "")
    page = b.post("/settings/security/totp/confirm", data={"csrf_token": b.csrf, "enroll_id": eid,
                                                           "code": totp.code_at(secret)}).text
    assert "復旧コード" in page and page.count("<span>") >= 10
    assert b.get("/api/v1/me/security").json()["totp"]["enabled"] is True


# ---------------------------------------------------------------------------
# パスキー（ソフトウェアの認証器）
# ---------------------------------------------------------------------------

class SoftAuthenticator:
    def __init__(self, rp_id=HOST, origin=ORIGIN, sign_count=0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.rp_id, self.origin, self.count, self.user_handle = rp_id, origin, sign_count, None

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client_data(self, typ, challenge, origin=None) -> bytes:
        return json.dumps({"type": typ, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        pk = options["publicKey"]
        assert pk["authenticatorSelection"]["residentKey"] == "required"
        assert pk["authenticatorSelection"]["userVerification"] == "required"
        self.user_handle = pk["user"]["id"]
        rp_hash = hashlib.sha256(pk["rp"]["id"].encode()).digest()
        flags = 0x01 | 0x04 | 0x40        # UP, UV, AT
        auth_data = (rp_hash + bytes([flags]) + self.count.to_bytes(4, "big") + bytes(16)
                     + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(self._client_data("webauthn.create", pk["challenge"])),
                             "attestationObject": b64u(att), "transports": ["internal"]}}

    def get(self, options: dict, *, origin=None, count=None, user_handle=None) -> dict:
        pk = options["publicKey"]
        self.count = self.count + 1 if count is None else count
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = rp_hash + bytes([0x01 | 0x04]) + self.count.to_bytes(4, "big")
        cd = self._client_data("webauthn.get", pk["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": user_handle or self.user_handle}}


def register_passkey(b: Browser, auth: SoftAuthenticator, name="テスト端末"):
    opts = b.post("/api/v1/me/passkeys/register/options")
    assert opts.status_code == 200, opts.text
    r = b.post("/api/v1/me/passkeys/register/verify",
               json={"challenge_id": opts.json()["challenge_id"], "credential": auth.create(opts.json()), "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def passkey_login(c: Browser, auth: SoftAuthenticator, **kw):
    opts = c.http.post("/auth/passkeys/login/options").json()
    assert opts["publicKey"].get("allowCredentials", []) == []
    return c.http.post("/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"],
                                                            "credential": auth.get(opts, **kw), "next": "/trash"})


def test_passkey_register_and_login(app, users, conn):
    b = Browser(app)
    b.login()
    auth = SoftAuthenticator()
    register_passkey(b, auth)
    sec = b.get("/api/v1/me/security").json()
    assert sec["passkeys"][0]["name"] == "テスト端末"
    # 同じ認証器は 2 回登録できない（exclude_credentials に入る）
    opts = b.post("/api/v1/me/passkeys/register/options").json()
    assert opts["publicKey"]["excludeCredentials"][0]["id"] == b64u(auth.cred_id)

    c = Browser(app)
    r = passkey_login(c, auth)
    assert r.status_code == 200, r.text
    assert r.json()["redirect"] == "/trash" and r.json()["user"]["username"] == "alice"
    assert c.get("/api/v1/me").status_code == 200
    assert conn.execute("SELECT auth_method FROM sessions ORDER BY rowid DESC").fetchone()[0] == "passkey"
    assert conn.execute("SELECT sign_count FROM webauthn_credentials").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM settings WHERE key = 'auth.rp_id'").fetchone()[0] == HOST


def test_passkey_sign_count_regression(app, users, conn):
    b = Browser(app)
    b.login()
    auth = SoftAuthenticator(sign_count=5)
    register_passkey(b, auth)
    assert passkey_login(Browser(app), auth).status_code == 200      # 6
    r = passkey_login(Browser(app), auth, count=6)                   # 後退（同じ値）
    assert r.status_code == 401
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action = 'passkey_clone_suspected'").fetchone()[0] == 1


def test_synced_passkey_zero_count(app, users):
    b = Browser(app)
    b.login()
    auth = SoftAuthenticator()
    register_passkey(b, auth)
    for _ in range(2):
        assert passkey_login(Browser(app), auth, count=0).status_code == 200


def test_passkey_rejections(app, users, conn):
    b = Browser(app)
    b.login()
    auth = SoftAuthenticator()
    register_passkey(b, auth)
    c = Browser(app)
    # 別の origin
    assert passkey_login(c, auth, origin="https://evil.example").status_code == 401
    # 別のユーザーの handle
    assert passkey_login(c, auth, user_handle=b64u(os.urandom(32))).status_code == 401
    # 使い回しの challenge
    opts = c.http.post("/auth/passkeys/login/options").json()
    body = {"challenge_id": opts["challenge_id"], "credential": auth.get(opts)}
    assert c.http.post("/auth/passkeys/login/verify", json=body).status_code == 200
    body["credential"] = auth.get(opts)
    assert c.http.post("/auth/passkeys/login/verify", json=body).status_code == 401
    # 期限切れの challenge
    opts = c.http.post("/auth/passkeys/login/options").json()
    conn.execute("UPDATE auth_challenges SET expires_at = '2000-01-01T00:00:00Z'")
    r = c.http.post("/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"],
                                                         "credential": auth.get(opts)})
    assert r.status_code == 401
    # 登録されていないパスキー
    assert passkey_login(c, SoftAuthenticator()).status_code == 401
    # 無効にしたユーザー
    conn.execute("UPDATE users SET is_active = 0 WHERE username = 'alice'")
    assert passkey_login(c, auth).status_code == 401


def test_passkey_unavailable_by_ip(app, users):
    c = TestClient(app, base_url="https://192.168.1.20")
    page = c.get("/login").text
    assert "data-passkey-button" not in page and f"パスキーは {ORIGIN} で開いたときに使えます" in page
    assert c.post("/auth/passkeys/login/options").status_code == 400
    assert "data-passkey-button" in TestClient(app, base_url=ORIGIN).get("/login").text


def test_passkey_reauth_and_delete(app, users, conn):
    b = Browser(app)
    b.login()
    auth = SoftAuthenticator()
    pid = register_passkey(b, auth)
    expire_reauth(conn)
    assert b.delete(f"/api/v1/me/passkeys/{pid}").status_code == 403
    other = SoftAuthenticator()
    opts = b.http.post("/auth/passkeys/login/options").json()
    r = b.post("/auth/reauth", json={"challenge_id": opts["challenge_id"], "credential": auth.get(opts)})
    assert r.status_code == 204
    assert b.patch(f"/api/v1/me/passkeys/{pid}", json={"name": "新しい名前"}).status_code == 204
    assert b.delete(f"/api/v1/me/passkeys/{pid}").status_code == 204
    assert passkey_login(Browser(app), auth).status_code == 401
    del other


def test_passkey_login_page_has_conditional_ui(app, users):
    page = TestClient(app, base_url=ORIGIN).get("/login").text
    assert 'autocomplete="username webauthn"' in page and "/static/passkey.js" in page
    assert "テスト IdP でログイン" in page


# ---------------------------------------------------------------------------
# OIDC（偽の提供者）
# ---------------------------------------------------------------------------

class FakeIdP:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
        self.nonce = None
        self.claims: dict = {}
        self.token_requests: list[dict] = []

    def id_token(self, **over) -> str:
        now = int(time.time())
        claims = {"iss": IDP, "aud": "client-1", "sub": "google-sub-1", "email": "alice@example.com",
                  "email_verified": True, "iat": now, "exp": now + 300, "nonce": self.nonce}
        claims.update(self.claims)
        claims.update(over)
        return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == IDP + "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": IDP, "authorization_endpoint": IDP + "/authorize",
                                             "token_endpoint": IDP + "/token", "jwks_uri": IDP + "/jwks"})
        if url == IDP + "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url == IDP + "/token":
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "at"})
        return httpx.Response(404)


@pytest.fixture()
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(oidc, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake


def oidc_round(c: TestClient, idp: FakeIdP, purpose="login", *, state=None, code="code-1"):
    r = c.get(f"/auth/oidc/test/start?purpose={purpose}&next=/trash", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == IDP + "/authorize"
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == ORIGIN + "/auth/oidc/test/callback"
    assert q["scope"] == "openid email profile"
    idp.nonce = q["nonce"]
    return c.get(f"/auth/oidc/test/callback?state={state or q['state']}&code={code}", follow_redirects=False)


def test_oidc_unlinked_rejected(app, users, idp):
    r = oidc_round(TestClient(app, base_url=ORIGIN), idp)
    assert r.status_code == 400 and "連携されていません" in r.text


def test_oidc_link_and_login(app, users, idp, conn):
    b = Browser(app)
    b.login()
    r = oidc_round(b.http, idp, "link")
    assert r.status_code == 303 and r.headers["location"] == "/settings/security?done=identity_linked"
    # PKCE の code_verifier と client_secret を送っている
    req = idp.token_requests[-1]
    assert req["code_verifier"][0] and req["client_secret"] == ["client-secret"]
    ident = b.get("/api/v1/me/security").json()["identities"][0]
    assert ident["provider"] == "test" and ident["email"] == "alice@example.com" and ident["label"] == "テスト IdP"

    c = TestClient(app, base_url=ORIGIN)
    r = oidc_round(c, idp)
    assert r.status_code == 303 and r.headers["location"] == "/trash"
    assert c.get("/api/v1/me").json()["username"] == "alice"
    assert conn.execute("SELECT auth_method FROM sessions ORDER BY rowid DESC").fetchone()[0] == "oidc:test"

    # 同じアカウントを別のユーザーには連携できない
    bob = Browser(app)
    bob.login("bob")
    r = oidc_round(bob.http, idp, "link")
    assert r.status_code == 422 and "別のユーザーに連携" in r.text


def test_oidc_link_needs_recent_auth(app, users, idp, conn):
    b = Browser(app)
    b.login()
    expire_reauth(conn)
    r = b.get("/auth/oidc/test/start?purpose=link", follow_redirects=False)
    assert r.headers["location"] == "/settings/security?reauth=1"


@pytest.mark.parametrize("claims,msg", [
    ({"nonce": "other"}, "nonce"),
    ({"aud": "someone-else"}, "aud"),
    ({"iss": "https://evil.example"}, "iss"),
    ({"exp": 1000}, "期限"),
])
def test_oidc_bad_tokens(app, users, idp, conn, claims, msg):
    conn.execute("INSERT INTO user_identities(user_id, provider, subject, created_at) VALUES (?, 'test', ?, 'x')",
                 (users["alice"].id, "google-sub-1"))
    idp.claims = claims
    r = oidc_round(TestClient(app, base_url=ORIGIN), idp)
    assert r.status_code == 400 and msg in r.text


def test_oidc_state_mismatch_and_bad_signature(app, users, idp, conn):
    conn.execute("INSERT INTO user_identities(user_id, provider, subject, created_at) VALUES (?, 'test', ?, 'x')",
                 (users["alice"].id, "google-sub-1"))
    c = TestClient(app, base_url=ORIGIN)
    assert "state" in oidc_round(c, idp, state="forged").text
    # 別の鍵で署名した ID トークン
    real = idp.key
    idp.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    jwks_key = real

    def handler(request):
        if str(request.url) == IDP + "/jwks":
            return httpx.Response(200, json={"keys": [jwks_key.as_dict(private=False)]})
        return FakeIdP.handler(idp, request)
    import docseed.oidc as mod
    mod.http_client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    r = oidc_round(TestClient(app, base_url=ORIGIN), idp)
    assert r.status_code == 400 and "署名" in r.text
    # Cookie が無い（別のブラウザで戻ってきた）
    r = TestClient(app, base_url=ORIGIN).get("/auth/oidc/test/callback?state=x&code=y")
    assert r.status_code == 400


def test_oidc_allowed_domains(app, users, idp, conn):
    admin = Browser(app)
    admin.login("admin")
    r = admin.patch("/api/v1/admin/settings", json={"oidc_allowed_domains": ["corp.example"]})
    assert r.json()["oidc_allowed_domains"] == ["corp.example"]
    b = Browser(app)
    b.login()
    r = oidc_round(b.http, idp, "link")
    assert r.status_code == 400 and "ドメイン" in r.text
    idp.claims = {"email": "alice@corp.example"}
    assert oidc_round(b.http, idp, "link").status_code == 303
    assert admin.patch("/api/v1/admin/settings", json={"oidc_allowed_domains": ["bad domain!"]}).status_code == 422


def test_oidc_unknown_provider(app, users):
    assert TestClient(app, base_url=ORIGIN).get("/auth/oidc/nope/start").status_code == 404


# ---------------------------------------------------------------------------
# 設定・監視
# ---------------------------------------------------------------------------

def test_settings_rp_id_and_oidc_env():
    s = Settings.from_env({"DOCSEED_HOST": "Docs.Example.com, docseed.local", "DOCSEED_HTTPS_PORT": "8443",
                           "DOCSEED_OIDC_PROVIDERS": "google",
                           "DOCSEED_OIDC_GOOGLE_ISSUER": "https://accounts.google.com/",
                           "DOCSEED_OIDC_GOOGLE_CLIENT_ID": "id", "DOCSEED_OIDC_GOOGLE_CLIENT_SECRET": "sec"})
    assert s.rp_id == "docs.example.com" and s.public_url == "https://docs.example.com:8443"
    p = s.oidc_provider("google")
    assert p.issuer == "https://accounts.google.com" and p.label == "google"
    assert Settings.from_env({"DOCSEED_HOST": "192.168.1.20"}).rp_id == ""
    with pytest.raises(SystemExit):
        Settings.from_env({"DOCSEED_OIDC_PROVIDERS": "google"})


def test_rp_id_change_alert(app, users, conn, settings):
    from dataclasses import replace

    from docseed.monitor import status
    b = Browser(app)
    b.login()
    register_passkey(b, SoftAuthenticator())
    assert not any(a["code"] == "rp_id_changed" for a in status(conn, settings)["alerts"])
    other = replace(settings, host="new.example")
    assert any(a["code"] == "rp_id_changed" for a in status(conn, other)["alerts"])
