"""監視の詳細（/admin/system）と通知（docs/design/deployment.md 6 章・6.1）。"""

import dataclasses
import datetime as dt
import io
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from docseed import monitor, notify
from docseed.config import Settings
from docseed.db import connect
from docseed.inbox import InboxScanner
from docseed.storage import BlobStore


class Hook:
    """httpx のモックの送り先。送られた (URL, JSON) を記録する。fail に入れた URL は 500 を返す。"""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in self.fail:
            return httpx.Response(500)
        self.sent.append((url, json.loads(request.content)))
        return httpx.Response(200)


@pytest.fixture()
def hook():
    return Hook()


@pytest.fixture()
def nset(settings):
    return dataclasses.replace(settings, notify_urls=("https://hooks.example/a",), host="docs.example")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def test_settings_from_env():
    s = Settings.from_env({"DOCSEED_NOTIFY_URLS": "ntfy+https://ntfy.example/t, https://discord.com/api/webhooks/1/x",
                           "DOCSEED_NOTIFY_EVENTS": "alert", "DOCSEED_THERMAL_WARN_C": "72"})
    assert s.notify_urls == ("ntfy+https://ntfy.example/t", "https://discord.com/api/webhooks/1/x")
    assert s.notify_events == {"alert"} and s.thermal_warn_c == 72 and s.notify_enabled
    assert not Settings.from_env({}).notify_enabled
    assert Settings.from_env({}).notify_events == {"alert", "share_upload", "inbox"}
    # メールは SMTP サーバーと宛先の両方が要る
    assert not Settings.from_env({"DOCSEED_NOTIFY_EMAIL": "a@example.com"}).notify_enabled
    assert Settings.from_env({"DOCSEED_NOTIFY_EMAIL": "a@example.com", "DOCSEED_SMTP_HOST": "smtp"}).notify_enabled


def test_parse_destination():
    p = notify.parse_destination
    assert p("https://discord.com/api/webhooks/1/abc") == ("discord", "https://discord.com/api/webhooks/1/abc")
    assert p("https://hooks.slack.com/services/x")[0] == "slack"
    assert p("https://ntfy.sh/topic")[0] == "ntfy"
    assert p("ntfy+https://ntfy.home.example/docs") == ("ntfy", "https://ntfy.home.example/docs")
    assert p("slack+https://chat.example/hooks/1") == ("slack", "https://chat.example/hooks/1")
    assert p("https://example.com/hook")[0] == "json"
    # 秘密を含む URL の道は画面に出さない
    assert notify.describe_destination("https://discord.com/api/webhooks/1/secret") == "discord（discord.com）"


# ---------------------------------------------------------------------------
# 送る
# ---------------------------------------------------------------------------

def test_disabled_enqueues_nothing(settings, conn):
    assert notify.enqueue(conn, settings, "alert", "t", "b") is None
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_event_filter(nset, conn):
    s = dataclasses.replace(nset, notify_events=frozenset({"alert"}))
    assert notify.enqueue(conn, s, "share_upload", "t", "b") is None
    assert notify.enqueue(conn, s, "alert", "t", "b") is not None


def test_formats(settings, hook):
    s = dataclasses.replace(settings, notify_urls=(
        "ntfy+https://ntfy.example/docs", "https://discord.com/api/webhooks/1/x",
        "https://hooks.slack.com/services/x", "https://example.com/hook"))
    assert notify.deliver(s, "件名", "本文", "error", http=hook.client) == []
    by = dict(hook.sent)
    assert by["https://ntfy.example/"] == {"topic": "docs", "title": "件名", "message": "本文", "priority": 5,
                                           "tags": ["rotating_light"]}
    assert by["https://discord.com/api/webhooks/1/x"] == {"content": "**件名**\n本文"}
    assert by["https://hooks.slack.com/services/x"] == {"text": "*件名*\n本文"}
    assert by["https://example.com/hook"]["message"] == "本文" and by["https://example.com/hook"]["level"] == "error"


def test_send_pending_groups_and_marks_sent(nset, conn, hook):
    notify.enqueue(conn, nset, "share_upload", "受け取りました", "a.pdf\nhttps://x/f/1", group_key="share:1")
    notify.enqueue(conn, nset, "share_upload", "受け取りました", "b.pdf\nhttps://x/f/1", group_key="share:1")
    notify.enqueue(conn, nset, "alert", "警告", "disk", level="warning")
    assert notify.send_pending(conn, nset, http=hook.client) == 2
    msgs = [m["message"] for _, m in hook.sent]
    assert "a.pdf\nb.pdf\nhttps://x/f/1" in msgs and "disk" in msgs
    assert {r[0] for r in conn.execute("SELECT status FROM notifications")} == {"sent"}
    assert notify.send_pending(conn, nset, http=hook.client) == 0       # 送り直さない


def test_partial_failure_is_recorded_but_not_retried(settings, conn, hook):
    s = dataclasses.replace(settings, notify_urls=("https://ok.example/h", "https://ng.example/h"))
    hook.fail.add("https://ng.example/h")
    notify.enqueue(conn, s, "alert", "t", "b")
    assert notify.send_pending(conn, s, http=hook.client) == 1
    r = conn.execute("SELECT status, error FROM notifications").fetchone()
    assert r["status"] == "sent" and "ng.example" in r["error"]


def test_total_failure_retries_then_gives_up(nset, conn, hook):
    hook.fail.add("https://hooks.example/a")
    notify.enqueue(conn, nset, "alert", "t", "b")
    for i in range(notify.MAX_ATTEMPTS):
        notify.send_pending(conn, nset, http=hook.client)
        st = conn.execute("SELECT status, attempts FROM notifications").fetchone()
        assert st["attempts"] == i + 1
    assert st["status"] == "failed"
    hook.fail.clear()
    assert notify.send_pending(conn, nset, http=hook.client) == 0


def test_email(settings, conn):
    s = dataclasses.replace(settings, smtp_host="smtp.example", notify_email=("me@example.com",))
    got = []
    notify.enqueue(conn, s, "alert", "件名", "本文")
    notify.send_pending(conn, s, email=lambda st, t, b: got.append((t, b)))
    assert got == [("件名", "本文")]


def test_cleanup(nset, conn, hook):
    notify.enqueue(conn, nset, "alert", "t", "b")
    notify.send_pending(conn, nset, http=hook.client)
    assert notify.cleanup(conn) == 0
    assert notify.cleanup(conn, now_=dt.datetime.now(dt.UTC) + dt.timedelta(days=31)) == 1


# ---------------------------------------------------------------------------
# 監視の警告
# ---------------------------------------------------------------------------

def _alerts(monkeypatch, items):
    real = monitor.status

    def fake(conn, settings, **kw):
        st = real(conn, settings, **kw)
        st["alerts"] = [{"level": lv, "code": c, "message": m} for lv, c, m in items()]
        return st
    monkeypatch.setattr(monitor, "status", fake)


def _kinds(conn):
    return [(r["title"], r["body"].splitlines()[0]) for r in
            conn.execute("SELECT title, body FROM notifications ORDER BY id")]


def test_check_alerts_lifecycle(nset, conn, monkeypatch):
    current = [("warning", "disk_low", "SSD の空きが少ない"), ("info", "restic_none", "記録なし")]
    _alerts(monkeypatch, lambda: current)
    t0 = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    assert notify.check_alerts(conn, nset, now_=t0) == 0                       # 1 回目は様子を見る
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(minutes=5)) == 1
    assert _kinds(conn) == [("[注意] docseed の警告", "SSD の空きが少ない")]      # info は知らせない
    assert "https://docs.example/admin" in conn.execute("SELECT body FROM notifications").fetchone()[0]
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(minutes=10)) == 0   # 続いていても知らせ直さない
    # 重くなったら知らせ直す
    current[0] = ("error", "disk_low", "SSD がいっぱい")
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(minutes=15)) == 1
    # 1 日たっても続いていれば知らせ直す
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(hours=23)) == 0
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(hours=25)) == 1
    # 消えたら「解消」
    current.clear()
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(hours=26)) == 1
    assert _kinds(conn)[-1] == ("[解消] docseed の警告", "解消しました: SSD がいっぱい")
    assert notify.check_alerts(conn, nset, now_=t0 + dt.timedelta(hours=27)) == 0


def test_transient_alert_is_not_notified(nset, conn, monkeypatch):
    current = [("error", "worker_stale", "worker が止まった")]
    _alerts(monkeypatch, lambda: current)
    notify.check_alerts(conn, nset)
    current.clear()                                                            # 再起動で直った
    assert notify.check_alerts(conn, nset) == 0
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 受け取り
# ---------------------------------------------------------------------------

def test_share_upload_notifies(app, login, conn, nset):
    app.state.settings = nset
    alice = login("alice")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "受け取り"}).json()
    url = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": f["id"],
                                              "permission": "upload"}).json()["url"]
    anon = TestClient(app, base_url="http://testserver")
    anon.post(f"/s/{url.rsplit('/s/', 1)[1]}/upload", files=[("files", ("書類.pdf", io.BytesIO(b"%PDF-1.4 x"))),
                                                             ("files", ("写真.jpg", io.BytesIO(b"\xff\xd8\xff x")))])
    r = conn.execute("SELECT * FROM notifications").fetchone()
    assert r["kind"] == "share_upload" and r["group_key"].startswith("share:")
    assert "Alice さんの「受け取り」に 2 件" in r["body"] and "書類.pdf" in r["body"]
    assert f"https://docs.example/f/{f['id']}" in r["body"]


def test_inbox_notifies(settings, nset, app, login, users):
    login("alice")
    conn = connect(settings.db_path)
    scanner = InboxScanner(settings.inbox_dir, BlobStore(settings.files_dir, settings.tmp_dir), max_bytes=10_000,
                           settings=nset)
    scanner.ensure_user_dirs(conn)
    ok = settings.inbox_dir / "alice" / "スキャン.pdf"
    ok.write_bytes(b"%PDF-1.4 inbox")
    big = settings.inbox_dir / "alice" / "big.bin"
    big.write_bytes(b"x" * 20_000)
    for p in (ok, big):
        t = time.time() - 60
        os.utime(p, (t, t))
    scanner.scan(conn)
    scanner.scan(conn)
    rows = {r["level"]: r for r in conn.execute("SELECT * FROM notifications")}
    assert rows["info"]["body"] == "alice: スキャン.pdf → Inbox/スキャン.pdf"
    assert rows["warning"]["title"] == "受け取り箱のファイルを取り込めませんでした"
    assert "big.bin" in rows["warning"]["body"]


# ---------------------------------------------------------------------------
# 管理画面・API
# ---------------------------------------------------------------------------

def test_admin_system_api(login):
    admin = login("admin")
    alice = login("alice")
    alice.upload(alice.root, {"a.txt": b"one"})
    alice.upload(alice.root, {"a.txt": b"two"}, on_conflict="replace")
    s = admin.get("/api/v1/admin/system").json()
    assert s["schema_version"] >= 9 and s["counts"]["documents"] == 1 and s["counts"]["versions"] == 1
    assert s["storage"]["version_only_bytes"] == 3
    assert {"cpu", "memory", "disk", "db_bytes", "models", "version", "uptime_s"} <= s.keys()
    assert s["models"]["embedding"] and s["models"]["llm"] is None
    assert alice.get("/api/v1/admin/system").status_code == 403
    page = admin.get("/admin").text
    assert "検索の索引" in page and "過去の版 1" in page and "通知の宛先が設定されていません" in page


def test_memory_and_temperature_alerts(settings, conn, monkeypatch):
    monkeypatch.setattr(monitor, "memory", lambda: {"total": 8 << 30, "available": 400 << 20,
                                                    "swap_total": 0, "swap_free": 0})
    monkeypatch.setattr(monitor, "cpu_temp", lambda: 76.0)
    c = {a["code"]: a["level"] for a in monitor.status(conn, settings)["alerts"]}
    assert c["memory_low"] == "warning" and c["thermal_high"] == "warning" and "thermal" not in c
    monkeypatch.setattr(monitor, "memory", lambda: {"total": 8 << 30, "available": 4 << 30,
                                                    "swap_total": 0, "swap_free": 0})
    monkeypatch.setattr(monitor, "cpu_temp", lambda: 60.0)
    c = {a["code"] for a in monitor.status(conn, settings)["alerts"]}
    assert not {"memory_low", "thermal_high"} & c


def test_admin_notify_test(app, login, nset, monkeypatch, hook):
    admin = login("admin")
    r = admin.post("/api/v1/admin/notify/test")
    assert r.status_code == 422                                                # 宛先が無い
    app.state.settings = nset
    real = notify.deliver
    monkeypatch.setattr(notify, "deliver", lambda *a, **kw: real(*a, http=hook.client))
    r = admin.post("/api/v1/admin/notify/test").json()
    assert r["status"] == "sent" and r["kind"] == "test" and hook.sent[0][1]["title"] == "docseed のテスト通知"
    hook.fail.add("https://hooks.example/a")
    r = admin.post("/admin/notify/test", data={"csrf_token": admin.csrf}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin?done=notify_failed")
    j = admin.get("/api/v1/admin/notifications").json()
    assert j["enabled"] and j["destinations"] == ["json（hooks.example）"]
    assert [n["status"] for n in j["notifications"]] == ["failed", "sent"]
    page = admin.get("/admin").text
    assert "テスト通知を送る" in page and "json（hooks.example）" in page and "失敗" in page
    assert login("alice").post("/api/v1/admin/notify/test").status_code == 403
