"""Prometheus 形式の指標（14）と、画面のルートの分け方（13）・.gitignore（15）。"""

import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _token(client, scopes):
    return client.post("/api/v1/me/tokens", json={"name": "監視", "scopes": scopes}).json()["token"]


def _parse(text: str) -> dict[str, list[tuple[str, float]]]:
    """テキスト形式を (名前 → [(ラベル, 値)]) にする。形式の崩れは assert で落とす。"""
    out: dict[str, list[tuple[str, float]]] = {}
    for ln in text.splitlines():
        if ln.startswith("# HELP ") or ln.startswith("# TYPE "):
            assert len(ln.split(" ", 3)) == 4, ln
            continue
        m = re.fullmatch(r'([a-z_:][a-z0-9_:]*)(\{.*\})? (-?[0-9.e+-]+|\+Inf)', ln)
        assert m, f"形式が崩れている: {ln!r}"
        out.setdefault(m.group(1), []).append((m.group(2) or "", float(m.group(3))))
    return out


def test_metrics_requires_admin_and_accepts_admin_token(app, login):
    admin, alice = login("admin"), login("alice")
    anon = TestClient(app)
    assert anon.get("/api/v1/admin/metrics").status_code == 401
    assert alice.get("/api/v1/admin/metrics").status_code == 403
    r = admin.get("/api/v1/admin/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    # Prometheus は API トークンを bearer で送る
    tok = _token(admin, ["read"])
    assert anon.get("/api/v1/admin/metrics", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    # 状態・システムも同じ（外部の監視用）。書き込みだけのトークン・一般の人のトークンは断る
    for path in ("/api/v1/admin/status", "/api/v1/admin/system"):
        assert anon.get(path, headers={"Authorization": f"Bearer {tok}"}).status_code == 200, path
    write_only = _token(admin, ["write"])
    assert anon.get("/api/v1/admin/metrics", headers={"Authorization": f"Bearer {write_only}"}).status_code == 403
    user_tok = _token(alice, ["read"])
    assert anon.get("/api/v1/admin/metrics", headers={"Authorization": f"Bearer {user_tok}"}).status_code == 403
    # ほかの管理の操作は、これまでどおりトークンでは行えない
    assert anon.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_metrics_content(login, conn):
    admin, alice = login("admin"), login("alice")
    alice.upload(alice.root, {"a.txt": b"hello world", "b.txt": b"x"})
    did = alice.get(f"/api/v1/folders/{alice.root}/children").json()["documents"][1]["id"]
    alice.delete(f"/api/v1/documents/{did}")
    alice.get("/api/v1/search", params={"q": "hello", "mode": "keyword"})
    alice.get("/search", params={"type": "text"})
    m = _parse(admin.get("/api/v1/admin/metrics").text)
    assert m["docseed_info"][0][1] == 1 and 'schema="' in m["docseed_info"][0][0]
    assert dict(m["docseed_documents"]) == {'{state="active"}': 1, '{state="trash"}': 1}
    assert ('{type="process_blob",status="pending"}', 2) in m["docseed_jobs"]
    assert m["docseed_jobs_oldest_pending_seconds"][0][1] >= 0
    assert m["docseed_worker_up"] == [("", 0)]
    assert ('{code="worker_never",level="warning"}', 1) in m["docseed_alert"]
    usage = dict(m["docseed_user_usage_bytes"])
    assert usage['{username="alice"}'] == 12 and usage['{username="admin"}'] == 0
    assert "docseed_user_quota_bytes" not in m                       # 上限なし
    assert {"docseed_disk_free_bytes", "docseed_db_bytes", "docseed_blob_bytes"} <= m.keys()
    # 検索の速さ（API と画面の検索の両方を数える。絞り込みだけは mode="filter"）
    counts = dict(m["docseed_search_duration_seconds_count"])
    assert counts.get('{mode="keyword"}', 0) >= 1 and counts.get('{mode="filter"}', 0) >= 1
    inf = [v for lab, v in m["docseed_search_duration_seconds_bucket"] if 'le="+Inf"' in lab]
    assert inf and all(v >= 1 for v in inf)


def test_metrics_job_durations_and_quota(login, conn, users):
    from docseed.limits import set_user_quota
    admin, alice = login("admin"), login("alice")
    alice.upload(alice.root, {"a.txt": b"x"})
    conn.execute("UPDATE jobs SET status = 'done', locked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 seconds'),"
                 " finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    set_user_quota(conn, users["alice"].id, 1000)
    m = _parse(admin.get("/api/v1/admin/metrics").text)
    avg = dict(m["docseed_job_duration_seconds_avg"])['{type="process_blob"}']
    assert 29 <= avg <= 31
    assert dict(m["docseed_jobs_done_recent"])['{type="process_blob"}'] == 1
    assert dict(m["docseed_user_quota_bytes"]) == {'{username="alice"}': 1000}


def test_label_values_are_escaped():
    from docseed.metrics import _Out
    o = _Out()
    o.metric("x", "gauge", "help", 1, {"a": 'q"b\\c\nd'})
    assert o.lines[-1] == 'x{a="q\\"b\\\\c\\nd"} 1'


# ---------------------------------------------------------------------------
# 13: 画面のルートを views/ に分けた
# ---------------------------------------------------------------------------

def test_web_routes_are_split_and_reexported():
    from docseed.web import common, routes
    from docseed.web.views import admin
    assert routes.router is common.router
    assert routes.admin_context is admin.admin_context
    for name in ("templates", "base_context", "render_error", "_check_origin", "_safe_next"):
        assert getattr(routes, name) is getattr(common, name), name
    assert len((ROOT / "src/docseed/web/routes.py").read_text().splitlines()) < 60
    for p in (ROOT / "src/docseed/web/views").glob("*.py"):
        assert len(p.read_text().splitlines()) < 600, p.name


# ---------------------------------------------------------------------------
# 15: 手元のログはリポジトリに入れない
# ---------------------------------------------------------------------------

def test_logs_are_ignored():
    def ignored(path: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
    assert ignored("server6.log") and ignored("data/serve.log")
    assert not ignored("scripts/poc/results/run_all-20261107-195929.log")     # 計測の結果は残す
    tracked = subprocess.run(["git", "ls-files", "*.log"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert all(t.startswith("scripts/poc/results/") for t in tracked), tracked
