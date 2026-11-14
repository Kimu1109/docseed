"""分割・再開できるアップロード（tus 1.0。docs/design/api.md 3.3.2）。"""

from __future__ import annotations

import base64

from docseed import resumable as ru
from docseed.db import Tx

OCTET = {"Content-Type": "application/offset+octet-stream", "Tus-Resumable": "1.0.0"}


def _meta(**kv) -> str:
    return ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}" for k, v in kv.items())


def _create(client, length: int, folder_id: int, filename: str = "見積書.pdf", **extra):
    return client.post("/api/v1/uploads", headers={
        "Tus-Resumable": "1.0.0", "Upload-Length": str(length),
        "Upload-Metadata": _meta(filename=filename, folder_id=folder_id, **extra)})


def _patch(client, url: str, offset: int, body: bytes):
    return client.patch(url, content=body, headers={**OCTET, "Upload-Offset": str(offset)})


def test_options_advertises_tus(login):
    alice = login("alice")
    r = alice.http.options("/api/v1/uploads")
    assert r.status_code == 204
    assert r.headers["tus-version"] == "1.0.0"
    assert "creation" in r.headers["tus-extension"] and "termination" in r.headers["tus-extension"]
    assert int(r.headers["tus-max-size"]) > 0


def test_chunked_upload_and_resume(login, settings):
    alice = login("alice")
    body = b"%PDF-1.4 " + bytes(range(256)) * 40
    r = _create(alice, len(body), alice.root)
    assert r.status_code == 201, r.text
    url = r.headers["location"]
    assert url.startswith("/api/v1/uploads/") and r.headers["upload-offset"] == "0"
    assert "upload-expires" in r.headers

    r = _patch(alice, url, 0, body[:3000])
    assert r.status_code == 204 and r.headers["upload-offset"] == "3000"
    # 途中で切れた後の再開: HEAD で受け取り済みの大きさを聞いて、そこから送る
    h = alice.http.head(url, headers={"Tus-Resumable": "1.0.0"})
    assert h.status_code == 200 and h.headers["upload-offset"] == "3000"
    assert h.headers["upload-length"] == str(len(body)) and h.headers["cache-control"] == "no-store"
    st = alice.get(url).json()
    assert st["offset"] == 3000 and st["complete"] is False and st["result"] is None

    r = _patch(alice, url, 3000, body[3000:])
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["complete"] is True and res["offset"] == len(body)
    assert res["result"]["status"] == "created"
    doc = res["result"]["document"]
    assert doc["filename"] == "見積書.pdf" and doc["size_bytes"] == len(body) and doc["mime_type"] == "application/pdf"
    assert alice.get(f"/api/v1/documents/{doc['id']}/content").content == body
    # 受け取り中のファイルは文書の実体へ移り、一時ファイルは残らない
    assert not list(settings.tmp_dir.glob("tus-*"))
    # 最後の応答を受け取れなかった場合: もう一度問い合わせると同じ結果が返る（2 回保存しない）
    again = _patch(alice, url, len(body), b"")
    assert again.status_code == 200 and again.json()["result"]["document"]["id"] == doc["id"]
    assert alice.get(url).json()["result"]["document"]["id"] == doc["id"]
    listing = alice.get(f"/api/v1/folders/{alice.root}/children").json()
    assert [d["filename"] for d in listing["documents"]] == ["見積書.pdf"]


def test_offset_mismatch_is_409(login):
    alice = login("alice")
    url = _create(alice, 10, alice.root, "a.txt").headers["location"]
    _patch(alice, url, 0, b"hello")
    r = _patch(alice, url, 0, b"hello")      # 届いていたのに、届かなかったと思って送り直した
    assert r.status_code == 409 and r.json()["code"] == "offset_mismatch"
    assert alice.http.head(url).headers["upload-offset"] == "5"


def test_patch_requires_octet_stream(login):
    alice = login("alice")
    url = _create(alice, 3, alice.root, "a.txt").headers["location"]
    r = alice.patch(url, content=b"abc", headers={"Upload-Offset": "0", "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_creation_with_upload_and_folder_path(login):
    alice = login("alice")
    r = alice.post("/api/v1/uploads", content=b"one", headers={
        **OCTET, "Upload-Length": "3",
        "Upload-Metadata": _meta(filename="x.txt", folder_id=alice.root, relative_path="2026/10/x.txt")})
    assert r.status_code == 201 and r.headers["location"]
    res = r.json()["result"]
    assert res["status"] == "created"
    children = alice.get(f"/api/v1/folders/{alice.root}/children").json()
    assert [f["name"] for f in children["folders"]] == ["2026"]


def test_empty_file(login):
    alice = login("alice")
    r = _create(alice, 0, alice.root, "空.txt")
    assert r.status_code == 201 and r.json()["result"]["status"] == "created"


def test_too_large_is_rejected_before_sending(app, login):
    alice = login("alice")
    object.__setattr__(app.state.settings, "max_upload_mb", 1)
    r = _create(alice, 2 * 1024 * 1024, alice.root)
    assert r.status_code == 413
    url = _create(alice, 4, alice.root, "a.txt").headers["location"]
    r = _patch(alice, url, 0, b"12345")      # Upload-Length を超えて送る
    assert r.status_code == 413
    assert alice.http.head(url).headers["upload-offset"] == "0"


def test_quota_checked_before_sending(login, conn, users):
    from docseed import limits
    alice = login("alice")
    with Tx(conn):
        conn.execute("UPDATE users SET quota_bytes = 100 WHERE id = ?", (users["alice"].id,))
    assert limits.quota(conn, users["alice"].id) == 100
    r = _create(alice, 1000, alice.root)
    assert r.status_code == 507 and r.json()["code"] == "quota_exceeded"


def test_finalize_error_is_reported_as_result(login):
    alice = login("alice")
    alice.upload(alice.root, {"a.txt": b"old"})
    url = _create(alice, 3, alice.root, "a.txt", on_conflict="error").headers["location"]
    r = _patch(alice, url, 0, b"new")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["status"] == "error" and res["error"]["code"] == "name_conflict"


def test_permissions(login):
    alice, bob = login("alice"), login("bob")
    r = _create(bob, 3, alice.root, "x.txt")     # 他の人のフォルダには作れない
    assert r.status_code == 404
    url = _create(alice, 3, alice.root, "x.txt").headers["location"]
    assert bob.http.head(url).status_code == 404       # 他の人のアップロードは見えない
    assert _patch(bob, url, 0, b"abc").status_code == 404
    assert bob.delete(url).status_code == 404
    assert alice.http.head(url).status_code == 200


def test_requires_csrf_with_session(login):
    alice = login("alice")
    url = _create(alice, 3, alice.root, "x.txt").headers["location"]
    r = alice.http.patch(url, content=b"abc", headers={**OCTET, "Upload-Offset": "0"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"


def test_metadata_validation(login):
    alice = login("alice")
    r = alice.post("/api/v1/uploads", headers={"Upload-Length": "3", "Upload-Metadata": _meta(filename="a.txt")})
    assert r.status_code == 422
    r = alice.post("/api/v1/uploads", headers={"Upload-Metadata": _meta(filename="a.txt", folder_id=alice.root)})
    assert r.status_code == 422
    r = alice.post("/api/v1/uploads", headers={"Upload-Length": "3", "Upload-Metadata": "filename ***"})
    assert r.status_code == 422
    r = alice.post("/api/v1/uploads", headers={"Tus-Resumable": "0.2.2", "Upload-Length": "3",
                                                "Upload-Metadata": _meta(filename="a.txt", folder_id=alice.root)})
    assert r.status_code == 412


def test_terminate(login, settings):
    alice = login("alice")
    url = _create(alice, 10, alice.root, "a.txt").headers["location"]
    _patch(alice, url, 0, b"abc")
    assert alice.delete(url).status_code == 204
    assert alice.http.head(url).status_code == 404
    assert not list(settings.tmp_dir.glob("tus-*"))


def test_busy_upload_is_423(login):
    alice = login("alice")
    url = _create(alice, 10, alice.root, "a.txt").headers["location"]
    with ru.lock(url.rsplit("/", 1)[1]):        # 届かなくなった前の接続がまだ書いている
        r = _patch(alice, url, 0, b"abc")
    assert r.status_code == 423 and r.json()["code"] == "upload_busy"
    assert _patch(alice, url, 0, b"abc").status_code == 204


def test_expire_stale(login, conn, settings, app):
    alice = login("alice")
    url = _create(alice, 10, alice.root, "a.txt").headers["location"]
    _patch(alice, url, 0, b"abc")
    upload_id = url.rsplit("/", 1)[1]
    orphan = settings.tmp_dir / "tus-orphan"
    orphan.write_bytes(b"x")
    import os
    os.utime(orphan, (0, 0))
    with Tx(conn):
        conn.execute("UPDATE resumable_uploads SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (upload_id,))
    assert alice.http.head(url).status_code == 410
    assert ru.expire_stale(conn, app.state.store) == 2
    assert not list(settings.tmp_dir.glob("tus-*"))
    assert conn.execute("SELECT count(*) FROM resumable_uploads").fetchone()[0] == 0


def test_api_token_can_upload(login):
    alice = login("alice")
    tok = alice.post("/api/v1/me/tokens", json={"name": "scanner", "scopes": ["read", "write"]})
    assert tok.status_code in (200, 201), tok.text
    h = {"Authorization": f"Bearer {tok.json()['token']}"}
    r = alice.http.post("/api/v1/uploads", content=b"abc", headers={
        **h, **OCTET, "Upload-Length": "3", "Upload-Metadata": _meta(filename="s.txt", folder_id=alice.root)})
    assert r.status_code == 201 and r.json()["result"]["status"] == "created"
