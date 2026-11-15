"""監視と通知。docs/design/security.md 11 章。

api の中で 1 分ごとに監査ログ（audit_log）と状態（monitor.status）を確かめ、次のことが起きたら
DOCSEED_NOTIFY_URL（ntfy・Discord・Slack・汎用の Webhook）に知らせる。

- ログインの失敗の急増（15 分に DOCSEED_NOTIFY_LOGIN_FAILURES 回以上。1 時間に 1 回まで）
- 外側（LAN・Tailscale 以外）の新しい接続元からのログインの成功（IPv4 は /24、IPv6 は /64 で比べる）
- 管理者の操作（ユーザーの作成・変更、ログインの設定の変更、ログイン方法のリセット）と、
  確認コードの無効化・パスキーの複製の疑い
- 共有リンクのパスワードの失敗の急増（同じリンクで 15 分に 5 回以上。リンクごとに 1 時間に 1 回まで）
- 管理画面の「確認が必要な項目」（error・warning）が新しく出たとき

worker は外部と通信しない設定なので、通知は外部へ出られる api が送る。
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Settings
from .db import Tx, connect

log = logging.getLogger("docseed.notify")

INTERVAL_S = 60
FIRST_DELAY_S = 30
WINDOW_S = 15 * 60
COOLDOWN_S = 3600
SHARE_PASSWORD_FAILURES = 5
NEW_IP_LOOKBACK_DAYS = 180
MAX_PER_ROUND = 10       # 1 回に送る件数の上限（超えた分はまとめて 1 件にする）
TIMEOUT_S = 10

LOGIN_ACTIONS = ("login", "passkey_login", "oidc_login")
FAILURE_ACTIONS = ("login_failed", "second_factor_failed")
WATCHED_ACTIONS = {
    "user_create": "ユーザーを作成しました",
    "user_update": "ユーザーを変更しました",
    "auth_settings_change": "ログインの設定を変更しました",
    "auth_reset": "ユーザーのログイン方法をリセットしました",
    "totp_disable": "確認コード（2 段階認証）を無効にしました",
    "passkey_clone_suspected": "パスキーの複製が疑われる使用がありました",
}


@dataclass
class Message:
    title: str
    body: str
    level: str = "info"      # info / warning / error
    event: str = ""


# ---------------------------------------------------------------------------
# 状態（settings 表の notify.*）
# ---------------------------------------------------------------------------

def _get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 内側の接続元（Caddy の DOCSEED_TRUSTED_NETS と同じ書き方）
# ---------------------------------------------------------------------------

# Caddy の private_ranges と同じ範囲（Python の is_private は説明用のアドレスなども含むので使わない）
CADDY_PRIVATE_RANGES = ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "127.0.0.1/8", "fd00::/8", "::1")


def trusted_networks(spec: str) -> list[ipaddress._BaseNetwork]:
    nets = []
    for tok in spec.split():
        if tok == "private_ranges":
            nets.extend(ipaddress.ip_network(n, strict=False) for n in CADDY_PRIVATE_RANGES)
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            log.warning("DOCSEED_TRUSTED_NETS の %r は CIDR ではないので無視します", tok)
    return nets


def is_inside(ip: str | None, spec: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return True          # IP の形でないもの（テストの testclient など）は数えない
    nets = trusted_networks(spec)
    return any(addr in n for n in nets if n.version == addr.version)


def _prefix(ip: str) -> ipaddress._BaseNetwork | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 64}", strict=False)


# ---------------------------------------------------------------------------
# 確かめる
# ---------------------------------------------------------------------------

def _username(conn: sqlite3.Connection, user_id: int | None) -> str:
    if user_id is None:
        return "（不明）"
    r = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return r[0] if r else f"#{user_id}"


def _cooled(conn: sqlite3.Connection, key: str, now: dt.datetime) -> bool:
    """key の通知を前回から COOLDOWN_S 以上たっていれば記録して True。"""
    raw = _get(conn, "notify.cooldown")
    cd = json.loads(raw) if raw else {}
    last = cd.get(key)
    if last and (now - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < COOLDOWN_S:
        return False
    cd[key] = _iso(now)
    # 古いものは捨てる
    cd = {k: v for k, v in cd.items()
          if (now - dt.datetime.fromisoformat(v.replace("Z", "+00:00"))).total_seconds() < 2 * COOLDOWN_S}
    _set(conn, "notify.cooldown", json.dumps(cd))
    return True


def _new_ip_login(conn: sqlite3.Connection, row: sqlite3.Row, settings: Settings) -> Message | None:
    ip = row["ip"]
    if is_inside(ip, settings.trusted_nets):
        return None
    pfx = _prefix(ip)
    if pfx is None:
        return None
    since = _iso(dt.datetime.now(dt.UTC) - dt.timedelta(days=NEW_IP_LOOKBACK_DAYS))
    marks = ",".join("?" * len(LOGIN_ACTIONS))
    seen = conn.execute(
        f"SELECT DISTINCT ip FROM audit_log WHERE user_id = ? AND action IN ({marks}) AND id < ? AND at >= ?",
        (row["user_id"], *LOGIN_ACTIONS, row["id"], since)).fetchall()
    for (old,) in seen:
        try:
            if old and ipaddress.ip_address(old) in pfx:
                return None
        except ValueError:
            continue
    how = {"passkey_login": "パスキー", "oidc_login": "外部アカウント"}.get(row["action"], "パスワード")
    return Message("新しい接続元からのログイン",
                   f"{_username(conn, row['user_id'])} が {ip} から{how}でログインしました（{row['at']}）。"
                   "心当たりが無ければ、設定画面の「ログイン中の端末」から終了させ、パスワードを変えてください",
                   "warning", "new_ip_login")


def check(conn: sqlite3.Connection, settings: Settings, *, now: dt.datetime | None = None) -> list[Message]:
    """前回からの監査ログと状態を確かめ、送る通知を返す（状態は settings 表に記録する）。"""
    now = now or dt.datetime.now(dt.UTC)
    out: list[Message] = []
    with Tx(conn):
        last_raw = _get(conn, "notify.last_audit_id")
        max_id = conn.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()[0]
        if last_raw is None:
            # 初回は過去の記録を通知しない
            _set(conn, "notify.last_audit_id", str(max_id))
            last_id = max_id
        else:
            last_id = int(last_raw)
        rows = conn.execute("SELECT * FROM audit_log WHERE id > ? AND id <= ? ORDER BY id",
                            (last_id, max_id)).fetchall()

        for r in rows:
            if r["action"] in LOGIN_ACTIONS:
                m = _new_ip_login(conn, r, settings)
                if m:
                    out.append(m)
            elif r["action"] in WATCHED_ACTIONS:
                target = f"（対象: {_username(conn, r['target_id'])}）" if r["target_type"] == "user" else ""
                level = "error" if r["action"] == "passkey_clone_suspected" else "warning"
                out.append(Message(WATCHED_ACTIONS[r["action"]],
                                   f"{_username(conn, r['user_id'])} が{WATCHED_ACTIONS[r['action']]}{target}"
                                   f"（{r['ip'] or '-'}、{r['at']}）", level, r["action"]))

        since = _iso(now - dt.timedelta(seconds=WINDOW_S))
        marks = ",".join("?" * len(FAILURE_ACTIONS))
        fails = conn.execute(f"SELECT count(*), count(DISTINCT ip) FROM audit_log WHERE action IN ({marks})"
                             " AND at >= ?", (*FAILURE_ACTIONS, since)).fetchone()
        if fails[0] >= settings.notify_login_failures and _cooled(conn, "login_failures", now):
            top = conn.execute(f"SELECT ip, count(*) n FROM audit_log WHERE action IN ({marks}) AND at >= ?"
                               " GROUP BY ip ORDER BY n DESC LIMIT 3", (*FAILURE_ACTIONS, since)).fetchall()
            ips = "、".join(f"{t['ip'] or '-'}（{t['n']} 回）" for t in top)
            out.append(Message("ログインの失敗が増えています",
                               f"直近 15 分で {fails[0]} 回（接続元 {fails[1]} 件）。多い順: {ips}",
                               "warning", "login_failures"))

        for s in conn.execute(
                "SELECT json_extract(detail, '$.share_id') sid, count(*) n FROM audit_log"
                " WHERE action = 'share_access' AND json_extract(detail, '$.what') = 'password_failed'"
                " AND at >= ? GROUP BY sid HAVING n >= ?", (since, SHARE_PASSWORD_FAILURES)).fetchall():
            if _cooled(conn, f"share_password:{s['sid']}", now):
                out.append(Message("共有リンクのパスワードの失敗が続いています",
                                   f"共有リンク #{s['sid']} で、直近 15 分に {s['n']} 回パスワードを間違えています。"
                                   "心当たりが無ければ、リンクを無効にしてください", "warning", "share_password"))

        _set(conn, "notify.last_audit_id", str(max_id))

    out.extend(_new_alerts(conn, settings, now))
    return out


def _new_alerts(conn: sqlite3.Connection, settings: Settings, now: dt.datetime) -> list[Message]:
    """管理画面の「確認が必要な項目」のうち、前回は無かった error・warning。"""
    from .monitor import status
    try:
        alerts = [a for a in status(conn, settings, now=now)["alerts"] if a["level"] in ("error", "warning")]
    except Exception:  # noqa: BLE001 - 状態の取得に失敗しても、ほかの通知は送る
        log.exception("status for notify failed")
        return []
    codes = sorted({a["code"] for a in alerts})
    with Tx(conn):
        prev_raw = _get(conn, "notify.alerts")
        _set(conn, "notify.alerts", json.dumps(codes))
    if prev_raw is None:
        return []            # 初回は今ある項目を通知しない（再起動のたびに届かないように）
    prev = set(json.loads(prev_raw))
    return [Message("確認が必要な項目", a["message"], a["level"], f"alert:{a['code']}")
            for a in alerts if a["code"] not in prev]


# ---------------------------------------------------------------------------
# 送る
# ---------------------------------------------------------------------------

def _payload(settings: Settings, msg: Message) -> tuple[str, dict]:
    """(送り先の URL, JSON)。DOCSEED_NOTIFY_FORMAT が auto なら URL から決める。"""
    url = settings.notify_url
    u = urlsplit(url)
    fmt = settings.notify_format
    if fmt == "auto":
        host = (u.hostname or "").lower()
        if host in ("discord.com", "discordapp.com") and u.path.startswith("/api/webhooks/"):
            fmt = "discord"
        elif host == "hooks.slack.com":
            fmt = "slack"
        elif "ntfy" in host:
            fmt = "ntfy"
        else:
            fmt = "json"
    name = settings.host.split(",")[0].strip() or "docseed"
    if fmt == "discord":
        return url, {"content": f"**[{name}] {msg.title}**\n{msg.body}"[:1900]}
    if fmt == "slack":
        return url, {"text": f"*[{name}] {msg.title}*\n{msg.body}"}
    if fmt == "ntfy":
        # 日本語の題名を送れるよう、ヘッダではなく JSON で送る（https://<server>/ に topic を付けて POST）
        topic = u.path.strip("/")
        prio = {"error": 5, "warning": 4}.get(msg.level, 3)
        return f"{u.scheme}://{u.netloc}/", {"topic": topic, "title": f"[{name}] {msg.title}", "message": msg.body,
                                             "priority": prio, "tags": ["docseed", msg.level]}
    return url, {"title": msg.title, "message": msg.body, "level": msg.level, "event": msg.event, "host": name}


def send(settings: Settings, msg: Message) -> bool:
    import httpx
    url, body = _payload(settings, msg)
    try:
        r = httpx.post(url, json=body, timeout=TIMEOUT_S)
        if r.status_code >= 300:
            log.warning("notify failed: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("notify failed: %s", e)
        return False


def deliver(settings: Settings, messages: list[Message]) -> int:
    if len(messages) > MAX_PER_ROUND:
        rest = messages[MAX_PER_ROUND - 1:]
        messages = messages[:MAX_PER_ROUND - 1] + [Message(
            f"ほか {len(rest)} 件の通知", "\n".join(f"・{m.title}: {m.body[:80]}" for m in rest)[:1500],
            max((m.level for m in rest), key=["info", "warning", "error"].index), "summary")]
    return sum(send(settings, m) for m in messages)


def run_once(settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        return deliver(settings, check(conn, settings))
    finally:
        conn.close()


def start_background(settings: Settings) -> threading.Thread | None:
    """api の中で 1 分ごとに確かめるスレッドを起動する（DOCSEED_NOTIFY_URL が無ければ何もしない）。"""
    if not settings.notify_url:
        return None

    def loop():
        time.sleep(FIRST_DELAY_S)
        while True:
            try:
                run_once(settings)
            except Exception:  # noqa: BLE001 - 通知の失敗で api を止めない
                log.exception("notify check failed")
            time.sleep(INTERVAL_S)

    th = threading.Thread(target=loop, name="notify", daemon=True)
    th.start()
    return th
