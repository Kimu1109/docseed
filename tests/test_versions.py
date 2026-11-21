"""過去の版（docs/design/data-model.md 3.3、api.md 3.3）と一覧のページ送り（ui.md）。"""

import dataclasses
import io
import re

from docseed import documents as dm
from docseed.db import Tx


def _up(client, name: str, body: bytes, **form) -> dict:
    return client.upload(client.root, {name: body}, **form).json()["results"][0]


def _blob_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM blobs").fetchone()[0]


def test_replace_keeps_previous_version(login, conn):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"two", on_conflict="replace")
    r = _up(alice, "a.txt", b"three", on_conflict="replace")
    assert r["status"] == "replaced" and r["document"]["id"] == did
    vers = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert [v["size_bytes"] for v in vers] == [3, 3]          # 新しい順: two, one
    assert alice.get(f"/api/v1/documents/{did}/versions/{vers[0]['id']}/content").content == b"two"
    r = alice.get(f"/api/v1/documents/{did}/versions/{vers[1]['id']}/content")
    assert r.content == b"one" and r.headers["content-disposition"].startswith("attachment")
    assert alice.get(f"/api/v1/documents/{did}/content").content == b"three"
    assert _blob_count(conn) == 3                              # 過去の版の中身は消さない


def test_same_content_does_not_add_version(login):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"one", on_conflict="replace")
    assert alice.get(f"/api/v1/documents/{did}/versions").json()["versions"] == []
    r = alice.post(f"/api/v1/documents/{did}/versions",
                   files={"file": ("x.txt", io.BytesIO(b"one"), "text/plain")})
    assert r.status_code == 201 and r.json()["changed"] is False


def test_upload_new_version_keeps_name_and_tags(login):
    alice = login("alice")
    did = _up(alice, "見積書.txt", b"v1")["document"]["id"]
    alice.patch(f"/api/v1/documents/{did}", json={"tags": ["経理"]})
    r = alice.post(f"/api/v1/documents/{did}/versions",
                   files={"file": ("見積書_改訂.txt", io.BytesIO(b"v2"), "text/plain")})
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert r.json()["changed"] is True and doc["filename"] == "見積書.txt" and doc["tags"] == ["経理"]
    assert alice.get(f"/api/v1/documents/{did}/content").content == b"v2"
    [v] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert v["filename"] == "見積書.txt" and v["created_by"] == alice.user["id"]


def test_restore_version_is_undoable(login, conn):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"two", on_conflict="replace")
    [v1] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    r = alice.post(f"/api/v1/documents/{did}/versions/{v1['id']}/restore")
    assert r.status_code == 200, r.text
    assert alice.get(f"/api/v1/documents/{did}/content").content == b"one"
    [v2] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert alice.get(f"/api/v1/documents/{did}/versions/{v2['id']}/content").content == b"two"
    # 戻した版はもう一覧に無い
    assert alice.post(f"/api/v1/documents/{did}/versions/{v1['id']}/restore").status_code == 404
    assert _blob_count(conn) == 2
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action = 'restore_version'").fetchone()[0] == 1


def test_delete_version_frees_blob(login, conn):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"two", on_conflict="replace")
    [v] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert _blob_count(conn) == 2
    assert alice.delete(f"/api/v1/documents/{did}/versions/{v['id']}").status_code == 204
    assert _blob_count(conn) == 1
    assert alice.get(f"/api/v1/documents/{did}/versions").json()["versions"] == []


def test_version_blob_shared_with_other_document_survives(login, conn):
    """過去の版と同じ内容の文書が別にあっても、版を消したときに blob を消さない。"""
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    other = _up(alice, "b.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"two", on_conflict="replace")
    [v] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    alice.delete(f"/api/v1/documents/{did}/versions/{v['id']}")
    assert alice.get(f"/api/v1/documents/{other}/content").content == b"one"


def test_keep_limit_prunes_oldest(app, login, conn):
    app.state.settings = dataclasses.replace(app.state.settings, versions_keep=2)
    alice = login("alice")
    did = _up(alice, "a.txt", b"v0")["document"]["id"]
    for i in range(1, 5):
        _up(alice, "a.txt", f"v{i}".encode(), on_conflict="replace")
    vers = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert [alice.get(f"/api/v1/documents/{did}/versions/{v['id']}/content").content for v in vers] == [b"v3", b"v2"]
    assert _blob_count(conn) == 3                              # 今の版 + 2 つ
    # 0 にすると版を残さない
    app.state.settings = dataclasses.replace(app.state.settings, versions_keep=0)
    _up(alice, "a.txt", b"v5", on_conflict="replace")
    assert alice.get(f"/api/v1/documents/{did}/versions").json()["versions"] == []
    assert _blob_count(conn) == 1


def test_prune_all_versions(login, conn, app):
    alice = login("alice")
    did = _up(alice, "a.txt", b"v0")["document"]["id"]
    for i in range(1, 4):
        _up(alice, "a.txt", f"v{i}".encode(), on_conflict="replace")
    with Tx(conn):
        assert dm.prune_versions(conn, None, 1) == 2
    assert len(alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]) == 1


def test_purging_document_removes_versions(login, conn):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    _up(alice, "a.txt", b"two", on_conflict="replace")
    alice.delete(f"/api/v1/documents/{did}")
    assert alice.delete(f"/api/v1/trash/document/{did}").status_code == 204
    assert conn.execute("SELECT count(*) FROM document_versions").fetchone()[0] == 0
    assert _blob_count(conn) == 0


def test_version_permissions(login, users):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "共有"}).json()
    did = alice.upload(f["id"], {"a.txt": b"one"}).json()["results"][0]["document"]["id"]
    alice.upload(f["id"], {"a.txt": b"two"}, on_conflict="replace")
    [v] = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    # 共有されていない人には見えない
    assert bob.get(f"/api/v1/documents/{did}/versions").status_code == 404
    assert bob.get(f"/api/v1/documents/{did}/versions/{v['id']}/content").status_code == 404
    # 閲覧だけの人は、見る・ダウンロードはできるが、戻す・消す・新しい版はできない
    alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "viewer"})
    assert len(bob.get(f"/api/v1/documents/{did}/versions").json()["versions"]) == 1
    assert bob.get(f"/api/v1/documents/{did}/versions/{v['id']}/content").content == b"one"
    assert bob.post(f"/api/v1/documents/{did}/versions/{v['id']}/restore").status_code == 403
    assert bob.delete(f"/api/v1/documents/{did}/versions/{v['id']}").status_code == 403
    r = bob.post(f"/api/v1/documents/{did}/versions", files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")})
    assert r.status_code == 403
    # 別の文書の ID と組み合わせても取れない
    other = alice.upload(f["id"], {"b.txt": b"b"}).json()["results"][0]["document"]["id"]
    assert alice.get(f"/api/v1/documents/{other}/versions/{v['id']}/content").status_code == 404


def test_document_page_versions(login):
    alice = login("alice")
    did = _up(alice, "a.txt", b"one")["document"]["id"]
    page = alice.get(f"/d/{did}").text
    assert "新しい版をアップロード" in page and "まだありません" in page
    r = alice.post(f"/d/{did}/versions", files={"file": ("a.txt", io.BytesIO(b"two"), "text/plain")},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("done=version_uploaded")
    page = alice.get(f"/d/{did}").text
    assert "過去の版 1 件" in page and "この版に戻す" in page
    vid = int(re.search(rf"/d/{did}/versions/(\d+)/restore", page).group(1))
    r = alice.post(f"/d/{did}/versions/{vid}/restore", follow_redirects=False)
    assert r.headers["location"].endswith("done=version_restored")
    assert alice.get(f"/api/v1/documents/{did}/content").content == b"one"
    vid = int(re.search(rf"/d/{did}/versions/(\d+)/delete", alice.get(f"/d/{did}").text).group(1))
    r = alice.post(f"/d/{did}/versions/{vid}/delete", follow_redirects=False)
    assert r.headers["location"].endswith("done=version_deleted")
    assert "まだありません" in alice.get(f"/d/{did}").text


# ---------------------------------------------------------------------------
# 一覧のページ送り
# ---------------------------------------------------------------------------

def _fill(alice, n_folders: int, n_docs: int) -> None:
    for i in range(n_folders):
        alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": f"f{i:03d}"})
    files = {f"d{i:03d}.txt": f"body {i}".encode() for i in range(n_docs)}
    assert alice.upload(alice.root, files).status_code == 200


def test_api_children_pagination(login):
    alice = login("alice")
    _fill(alice, 3, 5)
    url = f"/api/v1/folders/{alice.root}/children"
    all_ = alice.get(url).json()
    assert len(all_["folders"]) == 3 and len(all_["documents"]) == 5 and all_["next_cursor"] is None
    assert all_["total"] == {"folders": 3, "documents": 5}
    seen, cursor = [], None
    while True:
        r = alice.get(url, params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [f["name"] for f in r["folders"]] + [d["filename"] for d in r["documents"]]
        cursor = r["next_cursor"]
        if cursor is None:
            break
    assert seen == ["f000", "f001", "f002"] + [f"d{i:03d}.txt" for i in range(5)]
    r = alice.get(url, params={"limit": 2, "cursor": "3", "sort": "name", "order": "desc"}).json()
    assert r["folders"] == [] and [d["filename"] for d in r["documents"]] == ["d004.txt", "d003.txt"]
    assert alice.get(url, params={"limit": 0}).status_code == 422
    assert alice.get(url, params={"limit": 2, "cursor": "x"}).status_code == 422


def test_folder_page_pagination(login, monkeypatch):
    from docseed.web.views import files
    monkeypatch.setattr(files, "LISTING_PAGE_SIZE", 4)
    alice = login("alice")
    _fill(alice, 2, 7)
    names = lambda html: re.findall(r'data-item="[^"]+" data-name="([^"]+)"', html)  # noqa: E731
    p1 = alice.get(f"/f/{alice.root}").text
    assert names(p1) == ["f000", "f001", "d000.txt", "d001.txt"]
    assert "1 / 3 ページ" in p1 and "全 9 件" in p1 and f"/f/{alice.root}?page=2" in p1
    p3 = alice.get(f"/f/{alice.root}?page=3").text
    assert names(p3) == ["d006.txt"] and "rel=\"next\"" not in p3
    # 範囲外は最後のページ
    assert names(alice.get(f"/f/{alice.root}?page=99").text) == ["d006.txt"]
    # 並べ方を保ったままページを送る
    p2 = alice.get(f"/f/{alice.root}?sort=name&order=desc&page=2").text
    assert names(p2) == ["d004.txt", "d003.txt", "d002.txt", "d001.txt"]
    assert f"/f/{alice.root}?sort=name&order=desc&page=3" in p2.replace("&amp;", "&")
    # 操作の後の描き直しは、今見ているページ（HX-Current-URL）のまま
    did = alice.get(f"/api/v1/folders/{alice.root}/children").json()["documents"][4]["id"]   # d004
    r = alice.post(f"/x/documents/{did}/delete", data={"current": alice.root},
                   headers={"HX-Request": "true",
                            "HX-Current-URL": f"http://testserver/f/{alice.root}?sort=name&order=desc&page=2"})
    assert names(r.text) == ["d003.txt", "d002.txt", "d001.txt", "d000.txt"]


def test_small_folder_has_no_pager(login):
    alice = login("alice")
    _fill(alice, 1, 2)
    assert 'aria-label="ページ送り"' not in alice.get(f"/f/{alice.root}").text


def test_file_input_fits_side_panel():
    # 「新しい版のファイル」は 320px の横の欄にある。ファイル選びの部品は UI の言語（日本語など）で幅が広くなり、
    # 欄からはみ出すと画面全体が横にスクロールできてしまうので、親の幅より広がらないようにする
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "src" / "docseed" / "web" / "static" / "app.css").read_text()
    rule = re.search(r"input\[type=file\] \{([^}]*)\}", css).group(1)
    assert "min-width: 0" in rule and "max-width: 100%" in rule
