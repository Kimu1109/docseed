"""ログイン以外の流量制限とセキュリティログ（docs/design/security.md 9 章）。"""

from __future__ import annotations

import dataclasses
import re

from fastapi.testclient import TestClient

from docseed import ratelimit
from docseed.app import create_app
from tests.conftest import PASSWORD


def _log(settings) -> str:
    for h in ratelimit.sec_log.handlers:
        h.flush()
    p = settings.data_dir / "logs" / "security.log"
    return p.read_text() if p.exists() else ""


def test_invalid_share_tokens_are_limited(app, settings):
    c = TestClient(app)
    n, _ = ratelimit.SHARE_MISS_LIMIT
    for i in range(n):
        assert c.get(f"/s/not-a-token-{i}").status_code == 404
    r = c.get("/s/not-a-token-last")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    log = _log(settings)
    assert log.count("event=share_invalid ip=testclient") == 0   # testclient は IP の形でないので書かない
    assert "event=rate_limited" not in log


def test_security_log_format(settings, monkeypatch):
    ratelimit.setup_security_log(settings.data_dir / "logs")
    ratelimit.security_event("login_failed", "203.0.113.5")
    ratelimit.security_event("rate_limited", "2001:db8::1", rule="share upload;rm")
    ratelimit.security_event("login_failed", "$(reboot)")          # IP の形でないものは書かない
    lines = _log(settings).splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"\S+ docseed-security event=login_failed ip=203\.0\.113\.5", lines[0])
    assert lines[1].endswith("event=rate_limited ip=2001:db8::1 rule=share_upload_rm")


def test_login_failure_is_written_to_security_log(settings, users):
    app = create_app(settings)
    # 接続元を IP アドレスにする（TestClient の既定は "testclient"）
    c = TestClient(app, client=("198.51.100.7", 50000))
    c.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong-password-123"})
    assert "event=login_failed ip=198.51.100.7" in _log(settings)


def test_login_volume_is_limited(settings, users):
    app = create_app(settings)
    c = TestClient(app, client=("198.51.100.8", 50000))
    rule = next(r for r in ratelimit.RULES if r.name == "login")
    for _ in range(rule.limit):
        r = c.post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
        assert r.status_code in (401, 429)
    r = c.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert "event=rate_limited ip=198.51.100.8 rule=login" in _log(settings)
    # 別の IP は影響を受けない
    other = TestClient(app, client=("198.51.100.9", 50000))
    assert other.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


def test_search_is_limited(login, app):
    alice = login("alice")
    rule = next(r for r in ratelimit.RULES if r.name == "search")
    for _ in range(rule.limit):
        assert alice.get("/api/v1/search", params={"q": "x"}).status_code != 429
    assert alice.get("/api/v1/search", params={"q": "x"}).status_code == 429


def test_rate_limit_can_be_disabled(settings):
    app = create_app(dataclasses.replace(settings, rate_limit_enabled=False))
    c = TestClient(app)
    n, _ = ratelimit.SHARE_MISS_LIMIT
    for i in range(n + 5):
        assert c.get(f"/s/x{i}").status_code == 404


def test_sliding_window_memory_is_bounded():
    w = ratelimit.SlidingWindow()
    w.MAX_KEYS = 50
    for i in range(500):
        w.hit(f"k{i}", 10, 60)
    assert len(w._hits) <= 50


def test_missing_file_in_valid_share_is_not_counted(login, app):
    """リンクは有効で、中のファイルが無いだけの 404 は「無効なリンク」に数えない。"""
    alice = login("alice")
    tok = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": alice.root}).json()["url"]
    tok = tok.rsplit("/s/", 1)[1]
    c = TestClient(app)
    n, _ = ratelimit.SHARE_MISS_LIMIT
    for i in range(n + 5):
        assert c.get(f"/s/{tok}/thumb/{99999 + i}").status_code == 404
    assert c.get(f"/s/{tok}").status_code == 200
