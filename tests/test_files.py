import hashlib

from docseed.db import Tx, now


def test_upload_and_download(login, settings):
    alice = login("alice")
    body = "こんにちは\n".encode() * 100
    r = alice.upload(alice.root, {"メモ.txt": body})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["status"] == "created" and not res["deduplicated"]
    doc = res["document"]
    assert doc["mime_type"] == "text/plain"
    assert doc["sha256"] == hashlib.sha256(body).hexdigest()
    assert doc["processing"]["status"] == "pending"
    # 実体は内容アドレスで保存される
    sha = doc["sha256"]
    assert (settings.files_dir / sha[:2] / sha[2:4] / sha).read_bytes() == body

    r = alice.get(f"/api/v1/documents/{doc['id']}/content")
    assert r.status_code == 200 and r.content == body
    assert r.headers["content-disposition"].startswith("attachment")
    assert "filename*=utf-8''" in r.headers["content-disposition"].lower()
    assert r.headers["x-content-type-options"] == "nosniff"


def test_processing_job_is_queued(login, conn):
    alice = login("alice")
    doc = alice.upload(alice.root, {"a.pdf": b"%PDF-1.4 test"}).json()["results"][0]["document"]
    assert doc["mime_type"] == "application/pdf"
    job = conn.execute("SELECT * FROM jobs WHERE type = 'process_blob'").fetchone()
    assert job is not None and job["status"] == "pending"


def test_dedup_and_rename(login, conn):
    alice = login("alice")
    a = alice.upload(alice.root, {"見積書.pdf": b"%PDF-1.4 same"}).json()["results"][0]
    b = alice.upload(alice.root, {"見積書.pdf": b"%PDF-1.4 same"}).json()["results"][0]
    assert b["deduplicated"] is True
    assert b["document"]["filename"] == "見積書 (2).pdf"
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert a["document"]["id"] != b["document"]["id"]


def test_on_conflict_error_and_replace(login):
    alice = login("alice")
    alice.upload(alice.root, {"a.txt": b"one"})
    r = alice.upload(alice.root, {"a.txt": b"two"}, on_conflict="error").json()["results"][0]
    assert r["status"] == "error" and r["error"]["code"] == "name_conflict"
    r = alice.upload(alice.root, {"a.txt": b"three"}, on_conflict="replace").json()["results"][0]
    assert r["status"] == "replaced"
    assert alice.get(f"/api/v1/documents/{r['document']['id']}/content").content == b"three"


def test_relative_path_creates_folders(login):
    alice = login("alice")
    r = alice.upload(alice.root, {"x.txt": b"x"}, relative_paths="2026/10/x.txt").json()["results"][0]
    assert r["status"] == "created"
    children = alice.get(f"/api/v1/folders/{alice.root}/children").json()
    assert [f["name"] for f in children["folders"]] == ["2026"]


def test_filename_is_sanitized(login):
    alice = login("alice")
    r = alice.upload(alice.root, {"../../etc/passwd": b"x"}).json()["results"][0]
    assert "/" not in r["document"]["filename"]


def test_inline_preview_only_for_safe_types(login):
    alice = login("alice")
    html = alice.upload(alice.root, {"x.html": b"<script>alert(1)</script>"}).json()["results"][0]["document"]
    r = alice.get(f"/api/v1/documents/{html['id']}/content?inline=1")
    assert r.headers["content-disposition"].startswith("attachment")  # HTML は表示させない
    txt = alice.upload(alice.root, {"x.txt": b"hello"}).json()["results"][0]["document"]
    r = alice.get(f"/api/v1/documents/{txt['id']}/content?inline=1")
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["content-type"].startswith("text/plain")
    assert "sandbox" in r.headers["content-security-policy"]


def test_upload_size_limit(app, login, settings):
    alice = login("alice")
    object.__setattr__(app.state.settings, "max_upload_mb", 0)
    r = alice.upload(alice.root, {"big.bin": b"x" * 10}).json()["results"][0]
    assert r["status"] == "error" and r["error"]["code"] == "file_too_large"
    assert not any(settings.tmp_dir.iterdir())


def test_rename_move_and_tags(login):
    alice = login("alice")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "経理"}).json()
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    r = alice.patch(f"/api/v1/documents/{d['id']}", json={"filename": "b.txt", "folder_id": f["id"],
                                                          "tags": ["経理", "2026", "経理"]})
    assert r.status_code == 200
    assert r.json()["filename"] == "b.txt" and r.json()["folder_id"] == f["id"]
    assert r.json()["tags"] == ["2026", "経理"]


def test_folder_rules(login):
    alice = login("alice")
    a = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "A"}).json()
    b = alice.post("/api/v1/folders", json={"parent_id": a["id"], "name": "B"}).json()
    assert alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "a"}).status_code == 409
    # 自分の子孫へは移動できない
    assert alice.patch(f"/api/v1/folders/{a['id']}", json={"parent_id": b["id"]}).status_code == 409
    # ルートは削除できない
    assert alice.delete(f"/api/v1/folders/{alice.root}").status_code == 409
    assert alice.get(f"/api/v1/folders/{b['id']}").json()["path"][-1]["name"] == "B"


def test_other_users_files_are_hidden(login):
    alice = login("alice")
    bob = login("bob")
    d = alice.upload(alice.root, {"secret.txt": b"s"}).json()["results"][0]["document"]
    assert bob.get(f"/api/v1/documents/{d['id']}").status_code == 404
    assert bob.get(f"/api/v1/documents/{d['id']}/content").status_code == 404
    assert bob.get(f"/api/v1/folders/{alice.root}/children").status_code == 404
    assert bob.upload(alice.root, {"x.txt": b"x"}).json()["results"][0]["error"]["code"] == "not_found"
    # 管理者でも他人のファイルは見られない
    admin = login("admin")
    assert admin.get(f"/api/v1/documents/{d['id']}").status_code == 404


def test_folder_member_roles(login, conn, users):
    alice = login("alice")
    bob = login("bob")
    shared = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "共有"}).json()
    sub = alice.post("/api/v1/folders", json={"parent_id": shared["id"], "name": "サブ"}).json()
    with Tx(conn):
        conn.execute("INSERT INTO folder_members VALUES (?, ?, 'viewer', ?, ?)",
                     (shared["id"], users["bob"].id, users["alice"].id, now()))
    # 閲覧は配下も可、変更は不可
    assert bob.get(f"/api/v1/folders/{sub['id']}").json()["my_role"] == "viewer"
    assert bob.post("/api/v1/folders", json={"parent_id": sub["id"], "name": "x"}).status_code == 403
    assert bob.get("/api/v1/folders/shared-with-me").json()["folders"][0]["name"] == "共有"
    with Tx(conn):
        conn.execute("UPDATE folder_members SET role = 'editor'")
    assert bob.post("/api/v1/folders", json={"parent_id": sub["id"], "name": "x"}).status_code == 201


def test_trash_restore_and_purge(login, conn, settings):
    alice = login("alice")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "古い"}).json()
    d = alice.upload(f["id"], {"old.txt": b"old content"}).json()["results"][0]["document"]
    loose = alice.upload(alice.root, {"loose.txt": b"loose"}).json()["results"][0]["document"]

    assert alice.delete(f"/api/v1/folders/{f['id']}").status_code == 204
    assert alice.delete(f"/api/v1/documents/{loose['id']}").status_code == 204
    items = alice.get("/api/v1/trash").json()["items"]
    assert {(i["type"], i["name"]) for i in items} == {("folder", "古い"), ("document", "loose.txt")}
    folder_item = next(i for i in items if i["type"] == "folder")
    assert folder_item["files"] == 1
    assert alice.get(f"/api/v1/documents/{d['id']}").status_code == 404

    # 同名のフォルダを作ってから復元すると別名になる
    alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "古い"})
    assert alice.post(f"/api/v1/trash/folder/{f['id']}/restore").status_code == 204
    assert alice.get(f"/api/v1/folders/{f['id']}").json()["name"] == "古い (2)"
    assert alice.get(f"/api/v1/documents/{d['id']}").status_code == 200

    # 完全に削除すると、参照されなくなった実体も消える
    sha = loose["sha256"]
    assert alice.delete(f"/api/v1/trash/document/{loose['id']}").status_code == 204
    assert not (settings.files_dir / sha[:2] / sha[2:4] / sha).exists()
    assert conn.execute("SELECT count(*) FROM blobs WHERE sha256 = ?", (sha,)).fetchone()[0] == 0

    alice.delete(f"/api/v1/folders/{f['id']}")
    assert alice.delete("/api/v1/trash").json()["purged"] == 1
    assert alice.get("/api/v1/trash").json()["items"] == []
    assert conn.execute("SELECT count(*) FROM folders WHERE id = ?", (f["id"],)).fetchone()[0] == 0


def test_shared_blob_survives_partial_purge(login, conn, settings):
    alice = login("alice")
    a = alice.upload(alice.root, {"a.txt": b"same"}).json()["results"][0]["document"]
    b = alice.upload(alice.root, {"b.txt": b"same"}).json()["results"][0]["document"]
    alice.delete(f"/api/v1/documents/{a['id']}")
    alice.delete(f"/api/v1/trash/document/{a['id']}")
    assert alice.get(f"/api/v1/documents/{b['id']}/content").content == b"same"


def test_audit_log(login, conn):
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    alice.get(f"/api/v1/documents/{d['id']}/content")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "login" in actions and "upload" in actions and "download" in actions


def test_healthz(app):
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json()["schema_version"] == 3
