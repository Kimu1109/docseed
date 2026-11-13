"""容量の上限（11）とゴミ箱の保持日数（12）。"""

import datetime as dt
import io

from docseed import documents as dm
from docseed import limits
from docseed.storage import BlobStore


def _up(client, name: str, body: bytes, folder: int | None = None, **form) -> dict:
    return client.upload(folder or client.root, {name: body}, **form).json()["results"][0]


# ---------------------------------------------------------------------------
# 容量の上限
# ---------------------------------------------------------------------------

def test_usage_counts_current_trash_and_versions(login, conn, users):
    alice = login("alice")
    a = _up(alice, "a.txt", b"x" * 100)["document"]["id"]
    _up(alice, "a.txt", b"y" * 50, on_conflict="replace")          # 100 は過去の版になる
    b = _up(alice, "b.txt", b"z" * 30)["document"]["id"]
    alice.delete(f"/api/v1/documents/{b}")
    u = limits.usage(conn, users["alice"].id)
    assert u == {"current": 50, "trash": 30, "versions": 100, "total": 180}
    assert a
    me = alice.get("/api/v1/me/usage").json()
    assert me["total"] == 180 and me["quota"] == 0 and me["ratio"] is None


def test_upload_rejected_over_quota(login, conn, users):
    alice = login("alice")
    limits.set_user_quota(conn, users["alice"].id, 100)
    assert _up(alice, "a.txt", b"x" * 60)["status"] == "created"
    r = _up(alice, "b.txt", b"y" * 60)
    assert r["status"] == "error" and r["error"]["code"] == "quota_exceeded"
    assert "容量の上限" in r["error"]["detail"]
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    # 上限ちょうどまでは入る
    assert _up(alice, "c.txt", b"z" * 40)["status"] == "created"
    # 上限に達したら、小さくても受信の前に断る
    assert _up(alice, "d.txt", b"1")["error"]["code"] == "quota_exceeded"
    # ゴミ箱を空にすると空く
    did = alice.get(f"/api/v1/folders/{alice.root}/children").json()["documents"][0]["id"]
    alice.delete(f"/api/v1/documents/{did}")
    assert _up(alice, "e.txt", b"1")["error"]["code"] == "quota_exceeded"   # ゴミ箱の中も数える
    alice.delete("/api/v1/trash")
    assert _up(alice, "e.txt", b"1")["status"] == "created"


def test_quota_new_version_and_copy(login, conn, users):
    alice = login("alice")
    limits.set_user_quota(conn, users["alice"].id, 100)
    did = _up(alice, "a.txt", b"x" * 60)["document"]["id"]
    r = alice.post(f"/api/v1/documents/{did}/versions", files={"file": ("a.txt", io.BytesIO(b"y" * 50), "text/plain")})
    assert r.status_code == 507 and r.json()["code"] == "quota_exceeded"
    r = alice.post(f"/api/v1/documents/{did}/copy", json={"folder_id": alice.root})
    assert r.status_code == 507


def test_quota_counts_against_folder_owner(app, login, conn, users):
    """共有されたフォルダや受け取り用のリンクに置いたものは、フォルダの持ち主の分。"""
    from fastapi.testclient import TestClient
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "共有"}).json()
    alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "editor"})
    limits.set_user_quota(conn, users["alice"].id, 50)
    r = bob.upload(f["id"], {"b.txt": b"x" * 60}).json()["results"][0]
    assert r["error"]["code"] == "quota_exceeded" and "フォルダの持ち主" in r["error"]["detail"]
    assert _up(bob, "own.txt", b"x" * 60)["status"] == "created"           # bob 自身には上限なし
    url = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": f["id"],
                                              "permission": "upload"}).json()["url"]
    page = TestClient(app).post(f"/s/{url.rsplit('/s/', 1)[1]}/upload",
                                files=[("files", ("c.txt", io.BytesIO(b"y" * 60)))]).text
    assert "容量の上限" in page
    assert limits.usage(conn, users["alice"].id)["total"] == 0


def test_default_quota_and_override(login, conn, users):
    admin, alice = login("admin"), login("alice")
    r = admin.put("/api/v1/admin/quota", json={"quota_bytes": 100})
    assert r.status_code == 200 and r.json() == {"quota_bytes": 100}
    assert limits.quota(conn, users["alice"].id) == 100
    assert _up(alice, "a.txt", b"x" * 120)["error"]["code"] == "quota_exceeded"
    # 個別に上限なし（0）にする・既定に戻す（null）
    assert admin.patch(f"/api/v1/admin/users/{users['alice'].id}", json={"quota_bytes": 0}).status_code == 200
    assert _up(alice, "a.txt", b"x" * 120)["status"] == "created"
    u = next(x for x in admin.get("/api/v1/admin/users").json()["users"] if x["id"] == users["alice"].id)
    assert u["quota"] == {"bytes": 0, "custom": True, "used": 120}
    r = admin.patch(f"/api/v1/admin/users/{users['alice'].id}", json={"quota_bytes": None, "quota_default": True})
    assert r.status_code == 200 and limits.quota(conn, users["alice"].id) == 100
    assert admin.patch(f"/api/v1/admin/users/{users['alice'].id}", json={"quota_bytes": -1}).status_code == 422
    assert alice.put("/api/v1/admin/quota", json={"quota_bytes": 0}).status_code == 403


def test_quota_screens(login, conn, users):
    admin, alice = login("admin"), login("alice")
    _up(alice, "a.txt", b"x" * 1000)
    r = admin.post(f"/admin/users/{users['alice'].id}", data={"csrf_token": admin.csrf, "action": "quota",
                                                              "quota_gb": "0.000001"}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin?done=quota")
    assert limits.quota(conn, users["alice"].id) == 1073
    page = alice.get(f"/f/{alice.root}").text
    assert 'class="quota-meter"' in page and "/ 1.0 KB" in page
    settings_page = alice.get("/settings").text
    assert "今のファイル" in settings_page and "ゴミ箱" in settings_page and "上限" in settings_page
    admin_page = admin.get("/admin").text
    assert 'name="quota_gb"' in admin_page and "全体の既定" in admin_page
    r = admin.post("/admin/quota", data={"csrf_token": admin.csrf, "quota_gb": "50"}, follow_redirects=False)
    assert r.status_code == 303 and limits.default_quota(conn) == 50 * limits.GB
    r = admin.post("/admin/quota", data={"csrf_token": admin.csrf, "quota_gb": "abc"})
    assert r.status_code == 422
    # 上限なしなら、サイドは使用量だけ
    limits.set_default_quota(conn, 0)
    assert 'class="quota-meter"' not in login("bob").get("/settings").text


# ---------------------------------------------------------------------------
# ゴミ箱の保持日数
# ---------------------------------------------------------------------------

def test_trash_retention_setting(login, conn, users):
    alice = login("alice")
    assert alice.get("/api/v1/me/trash-retention").json() == {"days": 30, "default": 30, "custom": False}
    assert "30 日たつと" in alice.get("/trash").text
    r = alice.post("/settings/trash-retention", data={"csrf_token": alice.csrf, "days": "7"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings?done=trash_retention")
    assert "7 日たつと" in alice.get("/trash").text
    assert alice.put("/api/v1/me/trash-retention", json={"days": 90}).json()["days"] == 90
    assert alice.put("/api/v1/me/trash-retention", json={"days": 3}).status_code == 422
    assert alice.put("/api/v1/me/trash-retention", json={"days": None}).json() == \
        {"days": 30, "default": 30, "custom": False}
    # ほかの人には効かない
    alice.put("/api/v1/me/trash-retention", json={"days": 7})
    assert "30 日たつと" in login("bob").get("/trash").text


def test_purge_uses_each_owners_retention(login, conn, users, settings):
    alice, bob = login("alice"), login("bob")
    a = _up(alice, "a.txt", b"a")["document"]["id"]
    b = _up(bob, "b.txt", b"b")["document"]["id"]
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "古い"}).json()
    alice.delete(f"/api/v1/documents/{a}")
    bob.delete(f"/api/v1/documents/{b}")
    alice.delete(f"/api/v1/folders/{f['id']}")
    ten_days_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE documents SET deleted_at = ? WHERE deleted_at IS NOT NULL", (ten_days_ago,))
    conn.execute("UPDATE folders SET deleted_at = ? WHERE deleted_at IS NOT NULL", (ten_days_ago,))
    limits.set_trash_retention(conn, users["alice"].id, 7)                 # alice は 7 日、bob は既定 30 日
    store = BlobStore(settings.files_dir, settings.tmp_dir)
    n = dm.purge_expired(conn, store, limits.retention_cutoffs(conn, 30))
    assert n == 2                                                           # alice の文書とフォルダ
    assert conn.execute("SELECT count(*) FROM documents WHERE id = ?", (a,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM documents WHERE id = ?", (b,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM folders WHERE id = ?", (f["id"],)).fetchone()[0] == 0
    # 前と同じく、日時 1 つを渡せば全員に同じ期限
    assert dm.purge_expired(conn, store, dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")) == 1


def test_admin_screen_user_actions(login, conn, users):
    """管理の画面のユーザーの操作（以前は監査ログの記録で例外になっていた）。"""
    admin = login("admin")
    uid = users["bob"].id
    for action in ("disable", "enable", "make_admin", "make_user"):
        r = admin.post(f"/admin/users/{uid}", data={"csrf_token": admin.csrf, "action": action},
                       follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin?done=user_updated", action
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'user_update' ORDER BY id DESC").fetchone()
    assert '"op": "make_user"' in row[0]
