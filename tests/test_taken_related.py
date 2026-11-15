"""撮影日での絞り込み（taken:）と関連する文書。"""

import json

import pytest

from docseed import related
from docseed.search import taken_range

from .test_search import indexed  # noqa: F401 - フィクスチャ

# ---------------------------------------------------------------------------
# taken:
# ---------------------------------------------------------------------------

def test_taken_range():
    assert taken_range("2026") == ("2026-01-01", "2027-01-01")
    assert taken_range("2026-05") == ("2026-05-01", "2026-06-01")
    assert taken_range("2026-12") == ("2026-12-01", "2027-01-01")
    assert taken_range("2026-5-3") == ("2026-05-03", "2026-05-04")
    assert taken_range("2026/05/03") == ("2026-05-03", "2026-05-04")
    assert taken_range("2026-05-01..2026-05-10") == ("2026-05-01", "2026-05-11")
    assert taken_range("..2025") == (None, "2026-01-01")
    assert taken_range("2026-05..") == ("2026-05-01", None)
    for bad in ("x", "2026-13", "..", "2026-05-01..x", "2026-02-30"):
        assert taken_range(bad) is None, bad


def _photo(alice, conn, name: str, taken_at: str | None, body: bytes) -> int:
    d = alice.upload(alice.root, {name: body}).json()["results"][0]["document"]
    meta = {"width": 10, "height": 10} | ({"taken_at": taken_at} if taken_at else {})
    conn.execute("UPDATE blobs SET image_meta = ? WHERE id = (SELECT blob_id FROM documents WHERE id = ?)",
                 (json.dumps(meta), d["id"]))
    return d["id"]


def names(r):
    return [x["document"]["filename"] for x in r.json()["results"]]


@pytest.fixture()
def photos(login, conn):
    alice = login("alice")
    _photo(alice, conn, "春.jpg", "2026-04-29T10:00:00+09:00", b"\xff\xd8\xff haru")
    _photo(alice, conn, "連休.jpg", "2026-05-03T09:12:00", b"\xff\xd8\xff renkyu")
    _photo(alice, conn, "夏.jpg", "2026-08-10T18:30:00+09:00", b"\xff\xd8\xff natsu")
    _photo(alice, conn, "日時なし.jpg", None, b"\xff\xd8\xff none")
    return alice


def test_taken_filter_lists_photos_without_text(photos):
    """文字の無い写真（索引が無い）も、撮影日だけで一覧にできる。新しい順。"""
    alice = photos
    r = alice.get("/api/v1/search", params={"taken": "2026"})
    assert names(r) == ["夏.jpg", "連休.jpg", "春.jpg"] and r.json()["mode"] == "filter"
    assert names(alice.get("/api/v1/search", params={"q": "taken:2026-05"})) == ["連休.jpg"]
    assert names(alice.get("/api/v1/search", params={"taken": "2026-04-29..2026-05-03"})) == ["連休.jpg", "春.jpg"]
    assert names(alice.get("/api/v1/search", params={"taken": "2026-06.."})) == ["夏.jpg"]
    assert r.json()["results"][0]["document"]["taken_at"] == "2026-08-10T18:30:00+09:00"
    # 読めない指定はお知らせを出して無視する（ほかに条件が無いので何も出さない）
    r = alice.get("/api/v1/search", params={"taken": "きのう"}).json()
    assert r["total"] == 0 and "taken:" in r["notices"][0]


def test_taken_filter_respects_access(photos, login):
    bob = login("bob")
    assert names(bob.get("/api/v1/search", params={"taken": "2026"})) == []


def test_filter_only_listing_for_other_filters(photos):
    alice = photos
    # 種類だけでも一覧にできる
    assert len(names(alice.get("/api/v1/search", params={"type": "image"}))) == 4
    assert names(alice.get("/api/v1/search", params={"type": "pdf"})) == []


def test_search_page_taken(photos):
    alice = photos
    page = alice.get("/search", params={"taken_from": "2026-05-01", "taken_to": "2026-12-31"}).text
    assert "夏.jpg" in page and "連休.jpg" in page and "春.jpg" not in page
    assert "2026/08/10 18:30" in page                     # 撮影日時を出す
    assert 'name="taken_from" value="2026-05-01"' in page
    # 絞り込みが無ければ何も出さない
    assert "春.jpg" not in alice.get("/search").text
    assert "条件に合うファイルは見つかりませんでした" in alice.get("/search", params={"taken_from": "2030-01-01"}).text


def test_taken_with_query(indexed, conn):  # noqa: F811
    """検索語と一緒に使うと、撮影日のある文書だけに絞る（撮影日の無い文書は外れる）。"""
    alice, ids = indexed
    assert alice.get("/api/v1/search", params={"q": "振込先"}).json()["total"] == 1
    assert alice.get("/api/v1/search", params={"q": "振込先 taken:2026"}).json()["total"] == 0
    conn.execute("UPDATE blobs SET image_meta = ? WHERE id = (SELECT blob_id FROM documents WHERE id = ?)",
                 (json.dumps({"taken_at": "2026-03-01T12:00:00"}), ids["請求書_A社.txt"]))
    assert names(alice.get("/api/v1/search", params={"q": "振込先 taken:2026-03"})) == ["請求書_A社.txt"]
    assert alice.get("/api/v1/search", params={"q": "振込先 taken:2026-04"}).json()["total"] == 0


# ---------------------------------------------------------------------------
# 関連する文書
# ---------------------------------------------------------------------------

@pytest.fixture()
def low_floor(monkeypatch):
    # 偽の埋め込み（文字 3-gram）は類似度の水準が低いので、下限を下げる
    monkeypatch.setattr(related, "SIM_MIN", 0.05)


def test_related_documents(indexed, low_floor, login, users):  # noqa: F811
    alice, ids = indexed
    # 請求書に近い内容の文書を足す
    body = "請求書\n\n下記の振込先まで請求金額をお振り込みください。".encode()
    near = alice.upload(alice.root, {"請求書_B社.txt": body})
    near_id = near.json()["results"][0]["document"]["id"]
    _process(indexed)
    r = alice.get(f"/api/v1/documents/{ids['請求書_A社.txt']}/related").json()["documents"]
    assert r and r[0]["id"] == near_id and r[0]["path"] == "/" and 0 < r[0]["similarity"] <= 1
    assert ids["請求書_A社.txt"] not in [x["id"] for x in r]
    # 画面（開いた後に読み込む）
    page = alice.get(f"/d/{ids['請求書_A社.txt']}").text
    assert f'hx-get="/x/d/{ids["請求書_A社.txt"]}/related"' in page
    frag = alice.get(f"/x/d/{ids['請求書_A社.txt']}/related").text
    assert "関連する文書" in frag and "請求書_B社.txt" in frag and "マイファイル" in frag
    # 似ている文書が無ければ何も出さない
    related.SIM_MIN = 0.999
    assert alice.get(f"/x/d/{ids['請求書_A社.txt']}/related").text.strip() == ""


def test_related_excludes_copies_and_unreadable(indexed, low_floor, login, users):  # noqa: F811
    alice, ids = indexed
    copy = alice.post(f"/api/v1/documents/{ids['契約書.txt']}/copy", json={"folder_id": alice.root}).json()
    r = alice.get(f"/api/v1/documents/{ids['契約書.txt']}/related").json()["documents"]
    assert copy["id"] not in [x["id"] for x in r]                   # 同じ中身のコピーは出さない
    # 他人の文書は出さない・他人は開けない
    bob = login("bob")
    assert bob.get(f"/api/v1/documents/{ids['契約書.txt']}/related").status_code == 404
    bob_doc = bob.upload(bob.root, {"契約書_写し.txt": DOC_CONTRACT}).json()["results"][0]["document"]
    _process(indexed)
    r = alice.get(f"/api/v1/documents/{ids['契約書.txt']}/related").json()["documents"]
    assert bob_doc["id"] not in [x["id"] for x in r]


DOC_CONTRACT = "業務委託契約書\n\n本契約の有効期間は締結日から一年間とし、自動更新する。".encode()


def _process(indexed):  # noqa: F811
    """追加した文書を偽の埋め込みで処理する。"""
    from docseed.jobs.process import Context
    from docseed.jobs.worker import Worker

    from .test_search import TrigramEmbedder
    alice, _ = indexed
    app_settings = alice.http.app.state.settings
    w = Worker(app_settings, Context(app_settings.files_dir, ocr_factory=lambda: None,
                                     embedder_factory=TrigramEmbedder))
    while w.run_once():
        pass
