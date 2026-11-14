"""管理画面のジョブの一覧（処理中・待ち・失敗）と、5 秒ごとに読み直す部分（admin/jobs/panel）。"""

from docseed.db import Tx, now


def test_admin_lists_waiting_file_then_running_with_progress(login, conn):
    admin, alice = login("admin"), login("alice")
    r = alice.upload(alice.root, {"議事録.txt": ("会議の内容です。" * 20).encode()})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "created", r.text

    page = admin.get("/admin").text
    assert "待っているジョブ" in page and "議事録.txt" in page and "文字の読み取り・検索の準備" in page
    api = admin.get("/api/v1/admin/jobs").json()
    assert [j["title"] for j in api["waiting"]] == ["議事録.txt"] and api["waiting"][0]["position"] == 1
    assert api["running"] == []

    # worker が 2 / 5 ページまで読んでいる途中の状態を作る
    with Tx(conn):
        conn.execute("UPDATE jobs SET status = 'running', locked_at = ?, attempts = 1 WHERE type = 'process_blob'",
                     (now(),))
        conn.execute("UPDATE blobs SET status = 'processing', progress_done = 2, progress_total = 5")

    panel = admin.get("/admin/jobs/panel").text
    assert 'id="admin-jobs"' in panel and 'hx-get="/admin/jobs/panel"' in panel
    assert "議事録.txt" in panel and "2/5 ページ" in panel and "処理中" in panel
    api = admin.get("/api/v1/admin/jobs").json()
    assert api["running"][0]["title"] == "議事録.txt"
    assert api["running"][0]["progress"] == {"done": 2, "total": 5} and api["running"][0]["attempts"] == 1
    assert api["waiting"] == []


def test_jobs_panel_is_admin_only(login):
    alice = login("alice")
    assert alice.get("/admin/jobs/panel").status_code == 403
    assert alice.get("/api/v1/admin/jobs").status_code == 403
