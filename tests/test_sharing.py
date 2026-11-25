import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from docseed.db import connect
from docseed.inbox import InboxScanner
from docseed.storage import BlobStore


def public(app):
    return TestClient(app, base_url="http://testserver")


def token_of(url: str) -> str:
    return url.rsplit("/s/", 1)[1]


# ---------------------------------------------------------------------------
# フォルダのメンバー
# ---------------------------------------------------------------------------

def test_members_flow(login, users):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "家族"}).json()
    sub = alice.post("/api/v1/folders", json={"parent_id": f["id"], "name": "旅行"}).json()
    bid = users["bob"].id
    # bob は追加前は見えない
    assert bob.get(f"/api/v1/folders/{f['id']}").status_code == 404
    assert alice.put(f"/api/v1/folders/{f['id']}/members/{bid}", json={"role": "viewer"}).status_code == 204
    assert bob.get(f"/api/v1/folders/{sub['id']}").json()["my_role"] == "viewer"
    # 下のフォルダから見ると「上位で共有」として表示される
    m = alice.get(f"/api/v1/folders/{sub['id']}/members").json()["members"]
    assert [x["role"] for x in m] == ["owner", "viewer"] and m[1]["inherited_from"] == "家族"
    # メンバー（閲覧のみ）はメンバーを変えられない
    r = bob.put(f"/api/v1/folders/{f['id']}/members/{users['admin'].id}", json={"role": "viewer"})
    assert r.status_code == 403
    r = alice.put(f"/api/v1/folders/{f['id']}/members/{users['alice'].id}", json={"role": "editor"})
    assert r.status_code == 422  # 所有者自身は追加できない
    assert alice.delete(f"/api/v1/folders/{f['id']}/members/{bid}").status_code == 204
    assert bob.get(f"/api/v1/folders/{f['id']}").status_code == 404


def test_user_directory(login):
    alice = login("alice")
    names = [u["username"] for u in alice.get("/api/v1/users").json()["users"]]
    assert "bob" in names and "alice" not in names


def test_folder_archive(login):
    alice = login("alice")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "書類"}).json()
    sub = alice.post("/api/v1/folders", json={"parent_id": f["id"], "name": "2026"}).json()
    alice.upload(f["id"], {"a.txt": b"A"})
    alice.upload(sub["id"], {"見積書.txt": b"B"})
    r = alice.get(f"/api/v1/folders/{f['id']}/archive")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["2026/見積書.txt", "a.txt"]
    assert z.read("2026/見積書.txt") == b"B"


# ---------------------------------------------------------------------------
# 共有リンク
# ---------------------------------------------------------------------------

def test_document_link(app, login):
    alice = login("alice")
    d = alice.upload(alice.root, {"見積書.pdf": b"%PDF-1.4 x"}).json()["results"][0]["document"]
    r = alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"]})
    assert r.status_code == 201
    link = r.json()
    assert link["expires_at"] and link["permission"] == "read"
    anon = public(app)
    page = anon.get(f"/s/{token_of(link['url'])}")
    assert page.status_code == 200 and "見積書.pdf" in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and "noindex" in page.headers["x-robots-tag"]
    dl = anon.get(f"/s/{token_of(link['url'])}/download")
    assert dl.status_code == 200 and dl.content == b"%PDF-1.4 x"
    assert alice.get("/api/v1/shares").json()["shares"][0]["download_count"] == 1
    # 他の文書は取れない
    other = alice.upload(alice.root, {"秘密.txt": b"s"}).json()["results"][0]["document"]
    assert anon.get(f"/s/{token_of(link['url'])}/download/{other['id']}").status_code == 404
    assert anon.get(f"/s/{token_of(link['url'])}/preview/{other['id']}").status_code == 404


def test_link_counts_ranged_downloads(app, login):
    """上限の無いリンクでも、先頭からの Range は 1 回のダウンロードとして数える（続きの取得は数えない）。"""
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"abcdef"}).json()["results"][0]["document"]
    tok = token_of(alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"]}).json()["url"])
    anon = public(app)
    assert anon.get(f"/s/{tok}/download", headers={"Range": "bytes=0-"}).status_code == 206
    assert anon.get(f"/s/{tok}/download", headers={"Range": "bytes=3-"}).content == b"def"
    assert alice.get("/api/v1/shares").json()["shares"][0]["download_count"] == 1


def test_link_preview_only_for_previewable(app, login):
    """プレビュー・表示は表示できる形式だけ（ほかの形式をダウンロードとして返すと、回数に数えずに取れる）。"""
    alice = login("alice")
    d = alice.upload(alice.root, {"a.zip": b"PK\x05\x06" + b"\0" * 18}).json()["results"][0]["document"]
    tok = token_of(alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"],
                                                      "max_downloads": 1}).json()["url"])
    anon = public(app)
    assert anon.get(f"/s/{tok}/preview/{d['id']}").status_code == 404
    assert anon.get(f"/s/{tok}/view/{d['id']}").status_code == 404
    assert anon.get(f"/s/{tok}/download").status_code == 200


def test_folder_link_scope(app, login):
    alice = login("alice")
    shared = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "公開"}).json()
    inner = alice.post("/api/v1/folders", json={"parent_id": shared["id"], "name": "中"}).json()
    ok = alice.upload(inner["id"], {"ok.txt": b"ok"}).json()["results"][0]["document"]
    secret = alice.upload(alice.root, {"secret.txt": b"no"}).json()["results"][0]["document"]
    link = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": shared["id"]}).json()
    tok = token_of(link["url"])
    anon = public(app)
    assert "中" in anon.get(f"/s/{tok}").text
    assert "ok.txt" in anon.get(f"/s/{tok}?folder={inner['id']}").text
    assert anon.get(f"/s/{tok}/download/{ok['id']}").content == b"ok"
    # 共有したフォルダの外は見えない
    assert anon.get(f"/s/{tok}?folder={alice.root}").status_code == 404
    assert anon.get(f"/s/{tok}/download/{secret['id']}").status_code == 404
    assert anon.get(f"/s/{tok}/archive?folder={alice.root}").status_code == 404
    z = zipfile.ZipFile(io.BytesIO(anon.get(f"/s/{tok}/archive").content))
    assert z.namelist() == ["中/ok.txt"]


def test_password_link(app, login):
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    tok = token_of(alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"],
                                                      "password": "himitsu"}).json()["url"])
    anon = public(app)
    assert "パスワード" in anon.get(f"/s/{tok}").text
    assert anon.get(f"/s/{tok}/download", follow_redirects=False).status_code == 303
    assert anon.post(f"/s/{tok}/auth", data={"password": "wrong"}).status_code == 401
    r = anon.post(f"/s/{tok}/auth", data={"password": "himitsu"}, follow_redirects=False)
    assert r.status_code == 303
    assert anon.get(f"/s/{tok}/download").content == b"a"


def test_password_change_invalidates_link_cookie(app, login):
    """リンクのパスワードを変えると、前に入力した人はもう一度入力する（security.md 4.2）。"""
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    link = alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"],
                                              "password": "himitsu"}).json()
    tok = token_of(link["url"])
    anon = public(app)
    anon.post(f"/s/{tok}/auth", data={"password": "himitsu"})
    assert anon.get(f"/s/{tok}/download").content == b"a"
    assert alice.patch(f"/api/v1/shares/{link['id']}", json={"password": "atarashii"}).status_code == 200
    assert anon.get(f"/s/{tok}/download", follow_redirects=False).status_code == 303
    anon.post(f"/s/{tok}/auth", data={"password": "atarashii"})
    assert anon.get(f"/s/{tok}/download").content == b"a"


def test_password_attempts_limited(app, login):
    from docseed.web.public import pass_limiter
    pass_limiter._fail.clear()
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    tok = token_of(alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"],
                                                      "password": "himitsu"}).json()["url"])
    anon = public(app)
    for _ in range(10):
        anon.post(f"/s/{tok}/auth", data={"password": "x"})
    assert anon.post(f"/s/{tok}/auth", data={"password": "himitsu"}).status_code == 429
    pass_limiter._fail.clear()


def test_link_invalidation(app, login, conn):
    alice = login("alice")
    d = alice.upload(alice.root, {"a.txt": b"a"}).json()["results"][0]["document"]
    anon = public(app)

    def make(**kw):
        return alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"], **kw}).json()

    # 無効化
    link = make()
    assert alice.delete(f"/api/v1/shares/{link['id']}").status_code == 204
    assert anon.get(f"/s/{token_of(link['url'])}").status_code == 404
    # 期限切れ
    link = make()
    conn.execute("UPDATE shares SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (link["id"],))
    assert "期限" in anon.get(f"/s/{token_of(link['url'])}").text
    # 回数の上限
    link = make(max_downloads=1)
    assert anon.get(f"/s/{token_of(link['url'])}/download").status_code == 200
    assert anon.get(f"/s/{token_of(link['url'])}/download").status_code == 404
    # 回数の上限があるリンクは Range に応えず、毎回数える（数えない取得を繰り返させない）
    link = make(max_downloads=2)
    tok = token_of(link["url"])
    for rng in ("bytes=0-", "bytes=1-"):
        r = anon.get(f"/s/{tok}/download", headers={"Range": rng})
        assert r.status_code == 200 and r.content == b"a" and r.headers["accept-ranges"] == "none"
    assert anon.get(f"/s/{tok}/download", headers={"Range": "bytes=1-"}).status_code == 404
    # 対象をゴミ箱へ
    link = make()
    alice.delete(f"/api/v1/documents/{d['id']}")
    assert anon.get(f"/s/{token_of(link['url'])}").status_code == 404


def test_link_dies_when_creator_loses_access(app, login, users):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "共同"}).json()
    alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "editor"})
    link = bob.post("/api/v1/shares", json={"target_type": "folder", "target_id": f["id"]}).json()
    anon = public(app)
    assert anon.get(f"/s/{token_of(link['url'])}").status_code == 200
    # 所有者はメンバーが作ったリンクも見て無効にできる
    listed = alice.get("/api/v1/shares", params={"target_type": "folder", "target_id": f["id"]}).json()["shares"]
    assert [x["id"] for x in listed] == [link["id"]]
    alice.delete(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}")
    assert anon.get(f"/s/{token_of(link['url'])}").status_code == 404


def test_viewer_cannot_create_link(login, users):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "見るだけ"}).json()
    alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "viewer"})
    r = bob.post("/api/v1/shares", json={"target_type": "folder", "target_id": f["id"]})
    assert r.status_code == 403


def test_upload_link(app, login, conn):
    alice = login("alice")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "受け取り"}).json()
    alice.upload(f["id"], {"既存.txt": b"x"})
    tok = token_of(alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": f["id"],
                                                      "permission": "upload"}).json()["url"])
    anon = public(app)
    page = anon.get(f"/s/{tok}")
    assert "ファイルを送る" in page.text and "既存.txt" not in page.text  # 中身は見せない
    r = anon.post(f"/s/{tok}/upload", files=[("files", ("書類.pdf", io.BytesIO(b"%PDF-1.4 sent")))])
    assert r.status_code == 200 and "送信しました" in r.text
    docs = alice.get(f"/api/v1/folders/{f['id']}/children").json()["documents"]
    assert "書類.pdf" in [d["filename"] for d in docs]
    row = conn.execute("SELECT uploaded_via_share_id, created_by FROM documents WHERE filename = '書類.pdf'").fetchone()
    assert row[0] is not None
    # 受け取り用リンクではダウンロード・ZIP はできない
    assert anon.get(f"/s/{tok}/archive").status_code == 404
    assert anon.get(f"/s/{tok}/download/{docs[0]['id']}").status_code == 404
    # 文書には受け取り用リンクを作れない
    d = alice.upload(alice.root, {"x.txt": b"x"}).json()["results"][0]["document"]
    assert alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"],
                                              "permission": "upload"}).status_code == 422


def test_share_dialog_renders(login):
    alice = login("alice")
    r = alice.get("/x/share", params={"type": "folder", "id": alice.root})
    assert r.status_code == 200 and "リンクを作成" in r.text and "Bob" in r.text


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------

@pytest.fixture()
def scanner(settings, app):
    return InboxScanner(settings.inbox_dir, BlobStore(settings.files_dir, settings.tmp_dir), max_bytes=10_000)


def _age(p, seconds=60):
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_inbox_import(scanner, settings, login, users):
    alice = login("alice")
    conn = connect(settings.db_path)
    scanner.ensure_user_dirs(conn)
    d = settings.inbox_dir / "alice"
    assert d.is_dir() and (settings.inbox_dir / "bob").is_dir()
    (d / "スキャン").mkdir()
    f1 = d / "スキャン" / "領収書.pdf"
    f1.write_bytes(b"%PDF-1.4 inbox")
    (d / ".hidden").write_bytes(b"x")
    (d / "~$書きかけ.docx").write_bytes(b"x")
    big = d / "big.bin"
    big.write_bytes(b"x" * 20_000)
    for p in (f1, big):
        _age(p)
    assert scanner.scan(conn) == 0          # 1 回目は大きさを記録するだけ（書き込み中かもしれない）
    assert scanner.scan(conn) == 1          # 変化が無ければ取り込む
    assert not f1.exists() and not (d / "スキャン").exists()
    assert (d / ".hidden").exists() and (d / "~$書きかけ.docx").exists()
    # 大きすぎるファイルは .failed へ
    assert (d / ".failed" / "big.bin").exists() and (d / ".failed" / "big.bin.error.txt").exists()
    top = alice.get(f"/api/v1/folders/{alice.root}/children").json()["folders"]
    inbox = [f for f in top if f["name"] == "Inbox"][0]
    sub = alice.get(f"/api/v1/folders/{inbox['id']}/children").json()["folders"][0]
    assert sub["name"] == "スキャン"
    assert alice.get(f"/api/v1/folders/{sub['id']}/children").json()["documents"][0]["filename"] == "領収書.pdf"


def test_inbox_waits_for_writes(scanner, settings, login):
    login("alice")
    conn = connect(settings.db_path)
    scanner.ensure_user_dirs(conn)
    f = settings.inbox_dir / "alice" / "書き込み中.txt"
    f.write_bytes(b"a")
    assert scanner.scan(conn) == 0
    f.write_bytes(b"ab")                       # まだ書き込まれている
    assert scanner.scan(conn) == 0
    _age(f)
    assert scanner.scan(conn) == 0             # 大きさ・時刻が変わったので記録し直し
    assert scanner.scan(conn) == 1


def _upload_link(alice, folder_id, **kw):
    r = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": folder_id,
                                           "permission": "upload", **kw})
    return r


def test_upload_link_expiry_is_limited(login):
    alice = login("alice")
    assert _upload_link(alice, alice.root, expires="never").status_code == 422
    assert _upload_link(alice, alice.root, expires="31d").status_code == 422
    r = _upload_link(alice, alice.root, expires="30d")
    assert r.status_code == 201
    # 後から無期限にもできない
    assert alice.patch(f"/api/v1/shares/{r.json()['id']}", json={"expires": "never"}).status_code == 422
    # 閲覧用は今まで通り無期限にできる
    assert alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": alice.root,
                                              "expires": "never"}).status_code == 201


def _send(anon, tok, name, data):
    return anon.post(f"/s/{tok}/upload", files=[("files", (name, io.BytesIO(data)))])


def test_upload_link_limits(app, login, monkeypatch):
    import dataclasses
    import shutil
    from types import SimpleNamespace

    alice = login("alice")
    tok1 = token_of(_upload_link(alice, alice.root).json()["url"])
    tok2 = token_of(_upload_link(alice, alice.root).json()["url"])
    anon = public(app)
    gb = 1024 ** 3
    # 1 リンク 1536 バイト、すべてのリンクの 24 時間で 3000 バイト
    app.state.settings = dataclasses.replace(app.state.settings, share_upload_link_gb=1536 / gb,
                                             share_upload_daily_gb=3000 / gb)
    assert "送信しました" in _send(anon, tok1, "a.bin", b"a" * 1536).text
    assert "合計容量" in _send(anon, tok1, "b.bin", b"b").text            # 1 リンクの合計
    assert "送信しました" in _send(anon, tok2, "c.bin", b"c" * 1000).text  # 24 時間の残りは 464
    assert "送信しました" not in _send(anon, tok2, "d.bin", b"d" * 500).text
    assert "送信しました" in _send(anon, tok2, "e.bin", b"e" * 464).text
    assert "混み合って" in _send(anon, tok2, "f.bin", b"f").text          # すべてのリンクの合計

    # 空きが 10% を下回ったら受け取らない。ログインしたユーザーは 5% までは使える
    app.state.settings = dataclasses.replace(app.state.settings, share_upload_link_gb=10, share_upload_daily_gb=20,
                                             share_upload_min_free_pct=10.0)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=92, free=8))
    assert "空き容量" in _send(anon, tok2, "g.bin", b"g").text
    r = alice.upload(alice.root, {"mine.txt": b"x"})
    assert r.status_code < 300 and r.json()["results"][0].get("document"), r.text
