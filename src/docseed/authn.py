"""ログイン方法（2 段階認証・復旧コード・再認証・リセット）とセッションの開始。

docs/design/extensions.md 5 章。パスキーは passkeys.py、OIDC は oidc.py。
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
from dataclasses import dataclass

from fastapi import Request, Response

from . import security, totp
from .audit import audit
from .config import Settings
from .db import Tx, now
from .errors import NotFound, ReauthRequired, Unauthenticated, ValidationFailed
from .users import User, get_user

CHALLENGE_TTL = 300                 # 5 分
SECOND_FACTOR_COOKIE = "docseed_2fa"
# ログインに成功した端末の印（docs/design/security.md 2.4）。この Cookie があるユーザー名の試行は、
# ユーザー名単位のロックではなく端末単位で数える。署名付き・ユーザー名ごと・1 年
DEVICE_COOKIE = "docseed_device"
DEVICE_DAYS = 365
LOCK_HINTS = {"u:": "以前このアカウントでログインした端末か、パスキーでならログインできます"}

# ログイン試行の制限。パスワード・確認コード・復旧コード・パスキーの失敗をまとめて数える（5.1 節）
limiter = security.LoginLimiter()

METHOD_LABEL = {
    "password": "パスワード",
    "password+totp": "パスワード＋確認コード",
    "password+recovery": "パスワード＋復旧コード",
    "passkey": "パスキー",
}


def method_label(method: str, settings: Settings | None = None) -> str:
    if method.startswith("oidc:"):
        name = method[5:]
        p = settings.oidc_provider(name) if settings else None
        return p.label if p else name
    return METHOD_LABEL.get(method, method)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 短い間だけ使う値（auth_challenges）
# ---------------------------------------------------------------------------

def challenge_create(conn: sqlite3.Connection, purpose: str, user_id: int | None, data: bytes | None,
                     ttl: int = CHALLENGE_TTL) -> str:
    cid = secrets.token_urlsafe(32)
    expires = _iso(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl))
    with Tx(conn):
        # 期限の切れたものは、新しく作るときに消す（作られなければ溜まらない）
        conn.execute("DELETE FROM auth_challenges WHERE expires_at <= ?", (now(),))
        conn.execute("INSERT INTO auth_challenges(id, purpose, user_id, challenge, expires_at) VALUES (?,?,?,?,?)",
                     (cid, purpose, user_id, data, expires))
    return cid


def challenge_get(conn: sqlite3.Connection, cid: str | None, purpose: str) -> sqlite3.Row | None:
    if not cid:
        return None
    row = conn.execute("SELECT * FROM auth_challenges WHERE id = ? AND purpose = ?", (cid, purpose)).fetchone()
    if row is None or row["expires_at"] <= now():
        return None
    return row


def challenge_take(conn: sqlite3.Connection, cid: str | None, purpose: str) -> sqlite3.Row | None:
    """取り出して消す（同じ challenge を 2 回使わせない）。期限切れなら None。"""
    if not cid:
        return None
    with Tx(conn):
        row = conn.execute("SELECT * FROM auth_challenges WHERE id = ? AND purpose = ?", (cid, purpose)).fetchone()
        if row is not None:
            conn.execute("DELETE FROM auth_challenges WHERE id = ?", (cid,))
    if row is None or row["expires_at"] <= now():
        return None
    return row


def challenge_delete(conn: sqlite3.Connection, cid: str) -> None:
    with Tx(conn):
        conn.execute("DELETE FROM auth_challenges WHERE id = ?", (cid,))


# ---------------------------------------------------------------------------
# 管理者の設定（settings 表の auth.*）
# ---------------------------------------------------------------------------

@dataclass
class AuthSettings:
    require_2fa: bool = False
    oidc_allowed_domains: tuple[str, ...] = ()


def get_auth_settings(conn: sqlite3.Connection) -> AuthSettings:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'auth.%'").fetchall())
    domains = tuple(d for d in (rows.get("auth.oidc_allowed_domains") or "").split(",") if d)
    return AuthSettings(require_2fa=rows.get("auth.require_2fa") == "1", oidc_allowed_domains=domains)


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def set_auth_settings(conn: sqlite3.Connection, *, require_2fa: bool | None = None,
                      oidc_allowed_domains: str | list[str] | None = None) -> AuthSettings:
    with Tx(conn):
        if require_2fa is not None:
            _set_setting(conn, "auth.require_2fa", "1" if require_2fa else "0")
        if oidc_allowed_domains is not None:
            items = oidc_allowed_domains.replace(",", " ").split() if isinstance(oidc_allowed_domains, str) \
                else oidc_allowed_domains
            domains = []
            for d in items:
                d = d.strip().lower().lstrip("@")
                if not d:
                    continue
                if "." not in d or not all(p and (p.replace("-", "").isalnum()) for p in d.split(".")):
                    raise ValidationFailed(f"ドメインの形式が正しくありません: {d}")
                domains.append(d)
            _set_setting(conn, "auth.oidc_allowed_domains", ",".join(dict.fromkeys(domains)))
    return get_auth_settings(conn)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with Tx(conn):
        _set_setting(conn, key, value)


# ---------------------------------------------------------------------------
# ユーザーのログイン方法
# ---------------------------------------------------------------------------

def has_totp(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM user_totp WHERE user_id = ?", (user_id,)).fetchone() is not None


def passwordless_methods(conn: sqlite3.Connection, user_id: int) -> int:
    """パスワードを使わずにログインできる方法（パスキー・OIDC の連携）の数。"""
    r = conn.execute("SELECT (SELECT count(*) FROM webauthn_credentials WHERE user_id = ?)"
                     " + (SELECT count(*) FROM user_identities WHERE user_id = ?)", (user_id, user_id)).fetchone()
    return int(r[0])


def needs_second_factor_setup(conn: sqlite3.Connection, user: User, session: security.Session) -> bool:
    """管理者が 2 段階認証を必須にしていて、パスワードだけでログインしたユーザーが未設定か。"""
    if session.auth_method != "password" or not user.password_login:
        return False
    return get_auth_settings(conn).require_2fa and not has_totp(conn, user.id)


def security_summary(conn: sqlite3.Connection, settings: Settings, user_id: int,
                     current_session: str | None) -> dict:
    u = conn.execute("SELECT password_login FROM users WHERE id = ?", (user_id,)).fetchone()
    t = conn.execute("SELECT enabled_at FROM user_totp WHERE user_id = ?", (user_id,)).fetchone()
    rc = conn.execute("SELECT count(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL",
                      (user_id,)).fetchone()[0]
    passkeys = conn.execute(
        "SELECT id, name, created_at, last_used_at, backed_up FROM webauthn_credentials WHERE user_id = ?"
        " ORDER BY id", (user_id,)).fetchall()
    identities = conn.execute(
        "SELECT id, provider, email, created_at, last_login_at FROM user_identities WHERE user_id = ? ORDER BY id",
        (user_id,)).fetchall()
    sessions = conn.execute(
        "SELECT id, created_at, last_seen_at, user_agent, ip, auth_method FROM sessions"
        " WHERE user_id = ? AND expires_at > ? ORDER BY last_seen_at DESC", (user_id, now())).fetchall()
    return {
        "password_login": bool(u["password_login"]),
        "totp": {"enabled": t is not None, "enabled_at": t["enabled_at"] if t else None},
        "recovery_codes_remaining": rc,
        "passkeys": [{"id": p["id"], "name": p["name"], "created_at": p["created_at"],
                      "last_used_at": p["last_used_at"], "synced": bool(p["backed_up"])} for p in passkeys],
        "identities": [{"id": i["id"], "provider": i["provider"],
                        "label": method_label("oidc:" + i["provider"], settings), "email": i["email"],
                        "created_at": i["created_at"], "last_login_at": i["last_login_at"]} for i in identities],
        "sessions": [{"current": s["id"] == current_session, "created_at": s["created_at"],
                      "last_seen_at": s["last_seen_at"], "user_agent": s["user_agent"], "ip": s["ip"],
                      "auth_method": s["auth_method"], "auth_label": method_label(s["auth_method"], settings)}
                     for s in sessions],
    }


# ---------------------------------------------------------------------------
# 再認証（5.6 節）
# ---------------------------------------------------------------------------

def require_recent_auth(session: security.Session | None) -> None:
    if session is None or not session.recently_authenticated():
        raise ReauthRequired()


def check_second_factor(conn: sqlite3.Connection, user_id: int, code: str) -> str | None:
    """確認コードか復旧コードを確かめ、合えば 'totp' / 'recovery' を返す。コードは 1 回しか使えない。"""
    code = (code or "").strip()
    if not code:
        return None
    if any(ch.isalpha() for ch in code) or len(totp.normalize_code(code)) > totp.DIGITS:
        h = totp.recovery_hash(code)
        if not totp.normalize_recovery(code):
            return None
        with Tx(conn):
            cur = conn.execute("UPDATE user_recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ?"
                               " AND used_at IS NULL", (now(), user_id, h))
            if cur.rowcount:
                audit(conn, "recovery_code_used", user_id=user_id)
                return "recovery"
        return None
    row = conn.execute("SELECT secret, last_step FROM user_totp WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    step = totp.match(row["secret"], code, last_step=row["last_step"])
    if step is None:
        return None
    with Tx(conn):
        # 同時に 2 つのリクエストが同じコードを使っても、片方だけが成功するようにする
        cur = conn.execute("UPDATE user_totp SET last_step = ? WHERE user_id = ?"
                           " AND (last_step IS NULL OR last_step < ?)", (step, user_id, step))
    return "totp" if cur.rowcount else None


# ---------------------------------------------------------------------------
# ログインに成功した端末（デバイス Cookie）
# ---------------------------------------------------------------------------

def _norm(username: str) -> str:
    return username.strip().lower()


def trusted_device(settings: Settings, request: Request, username: str) -> str | None:
    """この端末がそのユーザー名でログインに成功したことがあれば、端末の ID を返す。"""
    for value in security.cookie_values(request, DEVICE_COOKIE):   # 同じ名前が複数あれば、合うものを先から
        d = security.unsign_value(settings.secret_key, "device", value)
        if isinstance(d, dict) and d.get("u") == _norm(username) and isinstance(d.get("n"), str):
            return d["n"]
    return None


def set_device_cookie(settings: Settings, request: Request, response: Response, username: str) -> None:
    nonce = trusted_device(settings, request, username) or secrets.token_urlsafe(12)
    value = security.sign_value(settings.secret_key, "device", {"u": _norm(username), "n": nonce},
                                DEVICE_DAYS * 86400)
    response.set_cookie(DEVICE_COOKIE, value, httponly=True, secure=settings.cookie_secure, samesite="lax",
                        path="/", max_age=DEVICE_DAYS * 86400)


def login_keys(settings: Settings, request: Request, username: str, ip: str) -> tuple[str, ...]:
    """パスワードでのログインの試行を数えるキー。

    - ログインに成功したことのある端末: その端末だけで数える（ユーザー名や IP のロックに巻き込まれない）
    - それ以外: ユーザー名と IP で数える。攻撃者がユーザー名をロックしても、本人は使い慣れた端末や
      パスキー（IP だけで数える）でログインできる
    """
    dev = trusted_device(settings, request, username)
    return (f"dev:{dev}",) if dev else (f"u:{_norm(username)}", f"ip:{ip}")


def reauth_with_password(conn: sqlite3.Connection, user: User, session: security.Session, password: str,
                         code: str, ip: str | None) -> None:
    # ログイン済みのセッションからだけ呼ばれるので、ユーザー名のロックとは別に数える
    keys = (f"reauth:{user.id}",)
    limiter.check(*keys)
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    ok = security.verify_password(row["password_hash"], password)
    if ok and has_totp(conn, user.id):
        ok = check_second_factor(conn, user.id, code) is not None
    if not ok:
        limiter.failed(*keys)
        with Tx(conn):
            audit(conn, "reauth_failed", user_id=user.id, ip=ip)
        raise ValidationFailed("パスワードまたは確認コードが違います")
    limiter.succeeded(*keys)
    security.mark_reauthenticated(conn, session)


# ---------------------------------------------------------------------------
# ログインの完了（セッションを作って Cookie を設定する）
# ---------------------------------------------------------------------------

def start_session(conn: sqlite3.Connection, settings: Settings, request: Request, response: Response,
                  user_id: int, method: str, remember: bool) -> str:
    """セッションを作り、Cookie を設定して CSRF トークンを返す。"""
    from .deps import client_ip
    ip = client_ip(request) or "-"
    raw, sess = security.create_session(
        conn, user_id, remember=remember, days=settings.session_days, short_hours=settings.session_hours_short,
        user_agent=request.headers.get("user-agent"), ip=ip, auth_method=method)
    action = "passkey_login" if method == "passkey" else "oidc_login" if method.startswith("oidc:") else "login"
    with Tx(conn):
        if method.startswith("password"):
            audit(conn, action, user_id=user_id, ip=ip, method=method)
        else:
            audit(conn, action, user_id=user_id, ip=ip)
    response.set_cookie(
        security.SESSION_COOKIE, raw, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
        max_age=settings.session_days * 86400 if remember else None,
    )
    response.delete_cookie(SECOND_FACTOR_COOKIE, path="/")
    user = get_user(conn, user_id)
    set_device_cookie(settings, request, response, user.username)
    return sess.csrf_token


@dataclass
class LoginResult:
    user: User
    csrf_token: str | None          # 2 段階目が要るときは None
    second_factor: bool = False


def password_login(conn: sqlite3.Connection, settings: Settings, request: Request, response: Response,
                   username: str, password: str, remember: bool, next_url: str | None = None) -> LoginResult:
    """パスワードでのログイン。確認コードを設定したユーザーは、2 段階目の待ちの状態を作って返す。"""
    from .deps import client_ip
    from .users import find_by_username
    ip = client_ip(request) or "-"
    keys = login_keys(settings, request, username, ip)
    limiter.check(*keys, hints=LOCK_HINTS)
    row = find_by_username(conn, username)
    ok = security.verify_password(row["password_hash"] if row else None, password)
    if not ok or not row["is_active"]:
        limiter.failed(*keys)
        with Tx(conn):
            audit(conn, "login_failed", user_id=row["id"] if row else None, ip=ip, username=username[:50])
        raise Unauthenticated("ユーザー名またはパスワードが違います")
    user = User.from_row(row)
    if not user.password_login:
        with Tx(conn):
            audit(conn, "login_failed", user_id=user.id, ip=ip, reason="password_login_disabled")
        raise Unauthenticated("このアカウントはパスワードでのログインを使わない設定です。パスキーなど、"
                              "設定したほかの方法でログインしてください")
    if has_totp(conn, user.id):
        data = json.dumps({"remember": remember, "next": next_url}).encode()
        cid = challenge_create(conn, "second_factor", user.id, data)
        response.set_cookie(SECOND_FACTOR_COOKIE, cid, httponly=True, secure=settings.cookie_secure,
                            samesite="lax", path="/", max_age=CHALLENGE_TTL)
        return LoginResult(user, None, second_factor=True)
    limiter.succeeded(*keys)
    return LoginResult(user, start_session(conn, settings, request, response, user.id, "password", remember))


def pending_second_factor(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    return next((row for cid in security.cookie_values(request, SECOND_FACTOR_COOKIE)
                 if (row := challenge_get(conn, cid, "second_factor")) is not None), None)


def finish_second_factor(conn: sqlite3.Connection, settings: Settings, request: Request, response: Response,
                         code: str) -> tuple[LoginResult, str | None]:
    """2 段階目。合えばセッションを作り、(結果, ログイン後に開く URL) を返す。"""
    from .deps import client_ip
    ip = client_ip(request) or "-"
    row = pending_second_factor(conn, request)
    if row is None:
        raise Unauthenticated("時間が経ちすぎました。もう一度ユーザー名とパスワードを入力してください")
    user = get_user(conn, row["user_id"])
    # 2 段階目に来られるのはパスワードを知っている人だけなので、ユーザー名のロックとは別に数える
    first = login_keys(settings, request, user.username, ip)
    keys = (f"2fa:{user.id}", *(k for k in first if k.startswith("ip:")))
    limiter.check(*keys)
    kind = check_second_factor(conn, user.id, code)
    if kind is None or not user.is_active:
        limiter.failed(*keys)
        with Tx(conn):
            audit(conn, "second_factor_failed", user_id=user.id, ip=ip)
        raise ValidationFailed("確認コードが違います")
    limiter.succeeded(*keys, *first)
    challenge_delete(conn, row["id"])
    data = json.loads(row["challenge"] or b"{}")
    csrf = start_session(conn, settings, request, response, user.id, f"password+{kind}", bool(data.get("remember")))
    return LoginResult(user, csrf), data.get("next")


# ---------------------------------------------------------------------------
# 確認コード（TOTP）と復旧コードの設定
# ---------------------------------------------------------------------------

def totp_begin(conn: sqlite3.Connection, user: User) -> tuple[str, str]:
    """登録を始める。(登録の ID, 秘密の値) を返す。コードの確認が済むまで有効にしない。"""
    if has_totp(conn, user.id):
        raise ValidationFailed("確認コードは既に有効です")
    secret = totp.new_secret()
    return challenge_create(conn, "totp_enroll", user.id, secret.encode(), ttl=900), secret


def totp_pending_secret(conn: sqlite3.Connection, user: User, enroll_id: str) -> str | None:
    row = challenge_get(conn, enroll_id, "totp_enroll")
    if row is None or row["user_id"] != user.id:
        return None
    return bytes(row["challenge"]).decode()


def _store_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    codes = totp.new_recovery_codes()
    conn.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO user_recovery_codes(user_id, code_hash) VALUES (?, ?)",
                     [(user_id, totp.recovery_hash(c)) for c in codes])
    return codes


def totp_confirm(conn: sqlite3.Connection, user: User, enroll_id: str, code: str, ip: str | None) -> list[str]:
    """コードが合えば有効にし、復旧コードを返す（表示は 1 回だけ）。"""
    secret = totp_pending_secret(conn, user, enroll_id)
    if secret is None:
        raise ValidationFailed("登録の有効期限が切れました。最初からやり直してください")
    step = totp.match(secret, code, last_step=None)
    if step is None:
        raise ValidationFailed("確認コードが違います。端末の時刻が合っているかも確かめてください")
    with Tx(conn):
        if has_totp(conn, user.id):
            raise ValidationFailed("確認コードは既に有効です")
        conn.execute("INSERT INTO user_totp(user_id, secret, last_step, enabled_at) VALUES (?,?,?,?)",
                     (user.id, secret, step, now()))
        conn.execute("DELETE FROM auth_challenges WHERE id = ?", (enroll_id,))
        codes = _store_recovery_codes(conn, user.id)
        audit(conn, "totp_enable", user_id=user.id, ip=ip)
    return codes


def totp_disable(conn: sqlite3.Connection, user: User, ip: str | None) -> None:
    with Tx(conn):
        cur = conn.execute("DELETE FROM user_totp WHERE user_id = ?", (user.id,))
        if not cur.rowcount:
            raise NotFound("確認コードは設定されていません")
        conn.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (user.id,))
        audit(conn, "totp_disable", user_id=user.id, ip=ip)


def regenerate_recovery_codes(conn: sqlite3.Connection, user: User, ip: str | None) -> list[str]:
    with Tx(conn):
        if not has_totp(conn, user.id):
            raise ValidationFailed("復旧コードは、確認コードを有効にしてから使えます")
        codes = _store_recovery_codes(conn, user.id)
        audit(conn, "recovery_codes_regenerate", user_id=user.id, ip=ip)
    return codes


# ---------------------------------------------------------------------------
# パスワードでのログインの切り替え・連携の解除
# ---------------------------------------------------------------------------

def set_password_login(conn: sqlite3.Connection, user: User, enabled: bool, ip: str | None) -> None:
    with Tx(conn):
        if not enabled and passwordless_methods(conn, user.id) == 0:
            raise ValidationFailed("パスワードでのログインを止めるには、先にパスキーか外部アカウントの連携を"
                                   "1 つ以上設定してください")
        conn.execute("UPDATE users SET password_login = ?, updated_at = ? WHERE id = ?",
                     (int(enabled), now(), user.id))
        audit(conn, "password_login_change", user_id=user.id, ip=ip, enabled=enabled)


def _check_not_last(conn: sqlite3.Connection, user_id: int) -> None:
    pw = conn.execute("SELECT password_login FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    if not pw and passwordless_methods(conn, user_id) <= 1:
        raise ValidationFailed("ログインする方法が無くなるため削除できません。先にパスワードでのログインを"
                               "有効にするか、ほかのパスキー・連携を追加してください")


def remove_passkey(conn: sqlite3.Connection, user: User, passkey_id: int, ip: str | None) -> None:
    with Tx(conn):
        row = conn.execute("SELECT name FROM webauthn_credentials WHERE id = ? AND user_id = ?",
                           (passkey_id, user.id)).fetchone()
        if row is None:
            raise NotFound("パスキーが見つかりません")
        _check_not_last(conn, user.id)
        conn.execute("DELETE FROM webauthn_credentials WHERE id = ?", (passkey_id,))
        audit(conn, "passkey_remove", user_id=user.id, target_type="passkey", target_id=passkey_id, ip=ip,
              name=row["name"])


def rename_passkey(conn: sqlite3.Connection, user: User, passkey_id: int, name: str) -> None:
    name = name.strip()
    if not name or len(name) > 50:
        raise ValidationFailed("パスキーの名前は 1〜50 文字にしてください")
    with Tx(conn):
        cur = conn.execute("UPDATE webauthn_credentials SET name = ? WHERE id = ? AND user_id = ?",
                           (name, passkey_id, user.id))
        if not cur.rowcount:
            raise NotFound("パスキーが見つかりません")


def remove_identity(conn: sqlite3.Connection, user: User, identity_id: int, ip: str | None) -> None:
    with Tx(conn):
        row = conn.execute("SELECT provider FROM user_identities WHERE id = ? AND user_id = ?",
                           (identity_id, user.id)).fetchone()
        if row is None:
            raise NotFound("連携が見つかりません")
        _check_not_last(conn, user.id)
        conn.execute("DELETE FROM user_identities WHERE id = ?", (identity_id,))
        audit(conn, "oidc_unlink", user_id=user.id, target_type="identity", target_id=identity_id, ip=ip,
              provider=row["provider"])


# ---------------------------------------------------------------------------
# リセット（5.7 節）
# ---------------------------------------------------------------------------

def reset_auth(conn: sqlite3.Connection, user_id: int, *, actor_id: int | None, ip: str | None) -> str:
    """パスキー・確認コード・OIDC の連携を消し、仮のパスワードを発行して返す。

    そのユーザーのすべてのセッションと API トークンを無効にする。
    """
    get_user(conn, user_id)
    temp = secrets.token_urlsafe(12)
    pw_hash = security.hash_password(temp)
    with Tx(conn):
        for table in ("webauthn_credentials", "user_totp", "user_recovery_codes", "user_identities",
                      "auth_challenges", "sessions"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET password_hash = ?, password_login = 1, updated_at = ? WHERE id = ?",
                     (pw_hash, now(), user_id))
        conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now(), user_id))
        audit(conn, "auth_reset", user_id=actor_id, target_type="user", target_id=user_id, ip=ip)
    return temp
