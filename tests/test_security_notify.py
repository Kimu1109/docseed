"""監視と通知（docs/design/security.md 11 章）。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from docseed import security_notify as notify
from docseed.audit import audit
from docseed.db import Tx


@pytest.fixture()
def nsettings(settings):
    return dataclasses.replace(settings, notify_url="https://ntfy.example/docseed-alerts", host="docs.example.com")


def _audit(conn, action, **kw):
    with Tx(conn):
        audit(conn, action, **kw)


def _check(conn, s):
    return notify.check(conn, s)


def _titles(msgs):
    return [m.title for m in msgs]


def test_first_run_does_not_replay_history(conn, users, nsettings):
    _audit(conn, "user_create", user_id=users["admin"].id, target_type="user", target_id=users["bob"].id, ip="1.2.3.4")
    assert [m for m in _check(conn, nsettings) if m.event == "user_create"] == []
    _audit(conn, "user_update", user_id=users["admin"].id, target_type="user", target_id=users["bob"].id, ip="1.2.3.4")
    msgs = [m for m in _check(conn, nsettings) if m.event == "user_update"]
    assert len(msgs) == 1 and "admin" in msgs[0].body and "bob" in msgs[0].body
    assert [m for m in _check(conn, nsettings) if m.event == "user_update"] == []   # 同じ記録は 1 回だけ


def test_new_ip_login(conn, users, nsettings):
    _check(conn, nsettings)
    alice = users["alice"].id
    _audit(conn, "login", user_id=alice, ip="203.0.113.10", method="password")
    m = [m for m in _check(conn, nsettings) if m.event == "new_ip_login"]
    assert len(m) == 1 and "alice" in m[0].body and "203.0.113.10" in m[0].body
    # 同じ /24 からは知らせない
    _audit(conn, "passkey_login", user_id=alice, ip="203.0.113.99")
    assert not [m for m in _check(conn, nsettings) if m.event == "new_ip_login"]
    # 別の接続元
    _audit(conn, "oidc_login", user_id=alice, ip="2001:db8:1:2::5")
    m = [m for m in _check(conn, nsettings) if m.event == "new_ip_login"]
    assert len(m) == 1 and "外部アカウント" in m[0].body
    # 同じ /64 の IPv6 は知らせない
    _audit(conn, "login", user_id=alice, ip="2001:db8:1:2::77")
    assert not [m for m in _check(conn, nsettings) if m.event == "new_ip_login"]
    # LAN・Tailscale・IP の形でないものは知らせない
    for ip in ("192.168.1.5", "100.101.102.103", "fd7a:115c:a1e0::9", "testclient"):
        _audit(conn, "login", user_id=alice, ip=ip)
    assert not [m for m in _check(conn, nsettings) if m.event == "new_ip_login"]


def test_trusted_nets_follow_caddy_setting(conn, users, nsettings):
    s = dataclasses.replace(nsettings, trusted_nets="100.64.0.0/10")   # LAN も外側扱い
    _check(conn, s)
    _audit(conn, "login", user_id=users["alice"].id, ip="192.168.1.5")
    assert [m for m in _check(conn, s) if m.event == "new_ip_login"]


def test_login_failure_surge(conn, users, nsettings):
    s = dataclasses.replace(nsettings, notify_login_failures=5)
    _check(conn, s)
    for i in range(4):
        _audit(conn, "login_failed", ip=f"198.51.100.{i}")
    assert not [m for m in _check(conn, s) if m.event == "login_failures"]
    _audit(conn, "second_factor_failed", user_id=users["alice"].id, ip="198.51.100.0")
    m = [m for m in _check(conn, s) if m.event == "login_failures"]
    assert len(m) == 1 and "5 回" in m[0].body and "198.51.100.0（2 回）" in m[0].body
    _audit(conn, "login_failed", ip="198.51.100.9")
    assert not [m for m in _check(conn, s) if m.event == "login_failures"]   # 1 時間に 1 回まで


def test_share_password_failures(conn, users, nsettings):
    _check(conn, nsettings)
    for _ in range(notify.SHARE_PASSWORD_FAILURES):
        _audit(conn, "share_access", ip="198.51.100.1", share_id=7, what="password_failed")
    m = [m for m in _check(conn, nsettings) if m.event == "share_password"]
    assert len(m) == 1 and "#7" in m[0].body
    _audit(conn, "share_access", ip="198.51.100.1", share_id=7, what="password_failed")
    assert not [m for m in _check(conn, nsettings) if m.event == "share_password"]


def test_new_system_alert(conn, users, nsettings, monkeypatch):
    from docseed import monitor
    alerts = []
    real = monitor.status

    def fake(conn, settings, now=None):
        st = real(conn, settings, now=now)
        st["alerts"] = list(alerts)
        return st
    monkeypatch.setattr(monitor, "status", fake)
    _check(conn, nsettings)
    alerts.append({"level": "error", "code": "disk_full", "message": "SSD の空きが 5% を下回っています"})
    alerts.append({"level": "info", "code": "restic_none", "message": "info は送らない"})
    m = [m for m in _check(conn, nsettings) if m.event.startswith("alert:")]
    assert [x.event for x in m] == ["alert:disk_full"] and m[0].level == "error"
    assert not [m for m in _check(conn, nsettings) if m.event.startswith("alert:")]   # 続いている間は 1 回
    alerts.clear()
    _check(conn, nsettings)
    alerts.append({"level": "error", "code": "disk_full", "message": "再び"})
    assert [m.event for m in _check(conn, nsettings) if m.event.startswith("alert:")] == ["alert:disk_full"]


@pytest.mark.parametrize("url,fmt,want_url,key", [
    ("https://ntfy.sh/my-secret-topic", "auto", "https://ntfy.sh/", "topic"),
    ("https://discord.com/api/webhooks/1/abc", "auto", "https://discord.com/api/webhooks/1/abc", "content"),
    ("https://hooks.slack.com/services/T/B/x", "auto", "https://hooks.slack.com/services/T/B/x", "text"),
    ("https://example.com/hook", "auto", "https://example.com/hook", "event"),
    ("https://push.example.com/alerts", "ntfy", "https://push.example.com/", "topic"),
])
def test_payload_formats(settings, url, fmt, want_url, key):
    s = dataclasses.replace(settings, notify_url=url, notify_format=fmt, host="docs.example.com")
    got_url, body = notify._payload(s, notify.Message("題名", "本文", "error", "x"))
    assert got_url == want_url and key in body
    if key == "topic":
        assert body["topic"] == url.rsplit("/", 1)[1] and body["priority"] == 5
        assert body["title"] == "[docs.example.com] 題名"


def test_deliver_summarizes_many(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, m: sent.append(m) or True)
    msgs = [notify.Message(f"t{i}", "b", "warning", "e") for i in range(25)]
    assert notify.deliver(settings, msgs) == notify.MAX_PER_ROUND
    assert sent[-1].title == f"ほか {25 - notify.MAX_PER_ROUND + 1} 件の通知"


def test_send_posts_json(settings, monkeypatch):
    import httpx
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return httpx.Response(200)
    monkeypatch.setattr(httpx, "post", fake_post)
    s = dataclasses.replace(settings, notify_url="https://ntfy.sh/t0pic")
    assert notify.send(s, notify.Message("a", "b"))
    assert calls == [("https://ntfy.sh/", {"topic": "t0pic", "title": "[docseed] a", "message": "b", "priority": 3,
                                           "tags": ["docseed", "info"]})]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(500, text="no"))
    assert not notify.send(s, notify.Message("a", "b"))


def test_cooldown_expires(conn, users, nsettings):
    s = dataclasses.replace(nsettings, notify_login_failures=1)
    t0 = dt.datetime.now(dt.UTC)
    notify.check(conn, s, now=t0)
    _audit(conn, "login_failed", ip="198.51.100.1")
    assert [m for m in notify.check(conn, s, now=t0) if m.event == "login_failures"]
    assert not [m for m in notify.check(conn, s, now=t0 + dt.timedelta(minutes=30)) if m.event == "login_failures"]


def test_run_once_sends_over_http(conn, users, settings, monkeypatch):
    """本物の HTTP で、ローカルの受け口に届くこと。"""
    import http.server
    import json
    import threading

    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    try:
        s = dataclasses.replace(settings, notify_url=f"http://127.0.0.1:{srv.server_port}/hook")
        notify.run_once(s)                       # 初回は過去を送らない
        _audit(conn, "auth_settings_change", user_id=users["admin"].id, ip="203.0.113.1")
        assert notify.run_once(s) == 1
    finally:
        srv.shutdown()
    path, body = got[-1]
    assert path == "/hook" and body["event"] == "auth_settings_change" and "admin" in body["message"]
