"""お気に入り・最近開いたファイル（7）と、検索の履歴・保存した検索（8）。"""

import re

from docseed import personal as ps


def _doc(client, name: str, body: bytes = b"x", folder: int | None = None) -> int:
    return client.upload(folder or client.root, {name: body}).json()["results"][0]["document"]["id"]


# ---------------------------------------------------------------------------
# お気に入り
# ---------------------------------------------------------------------------

def test_favorites_api(login):
    alice = login("alice")
    did = _doc(alice, "a.txt")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "経理"}).json()
    assert alice.put(f"/api/v1/me/favorites/document/{did}").status_code == 204
    assert alice.put(f"/api/v1/me/favorites/folder/{f['id']}").status_code == 204
    assert alice.put(f"/api/v1/me/favorites/document/{did}").status_code == 204       # 2 回目もそのまま
    favs = alice.get("/api/v1/me/favorites").json()["favorites"]
    assert [(x["type"], x["id"]) for x in favs] == [("folder", f["id"]), ("document", did)]
    assert alice.delete(f"/api/v1/me/favorites/document/{did}").status_code == 204
    assert [x["type"] for x in alice.get("/api/v1/me/favorites").json()["favorites"]] == ["folder"]
    assert alice.put("/api/v1/me/favorites/tag/1").status_code == 422


def test_favorites_respect_access_and_trash(login, users, conn):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "共有"}).json()
    did = _doc(alice, "a.txt", folder=f["id"])
    # 見られないものは入れられない
    assert bob.put(f"/api/v1/me/favorites/document/{did}").status_code == 404
    assert bob.put(f"/api/v1/me/favorites/folder/{f['id']}").status_code == 404
    alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "viewer"})
    assert bob.put(f"/api/v1/me/favorites/document/{did}").status_code == 204
    assert len(bob.get("/api/v1/me/favorites").json()["favorites"]) == 1
    # 共有をやめたら出さない（記録は残す: また共有されたら出る）
    alice.delete(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}")
    assert bob.get("/api/v1/me/favorites").json()["favorites"] == []
    # ゴミ箱の中は出さず、完全に削除したら記録も消す
    alice.put(f"/api/v1/me/favorites/document/{did}")
    alice.delete(f"/api/v1/documents/{did}")
    assert alice.get("/api/v1/me/favorites").json()["favorites"] == []
    alice.delete(f"/api/v1/trash/document/{did}")
    alice.get("/api/v1/me/favorites")
    assert conn.execute("SELECT count(*) FROM favorites WHERE user_id = ?", (users["alice"].id,)).fetchone()[0] == 0


def test_favorite_toggle_in_pages(login):
    alice = login("alice")
    did = _doc(alice, "見積書.txt")
    page = alice.get(f"/d/{did}").text
    assert 'aria-pressed="false"' in page and "お気に入りに入れる" in page
    r = alice.post("/x/favorite", data={"type": "document", "id": did, "on": "1"}, headers={"HX-Request": "true"})
    assert 'aria-pressed="true"' in r.text and "is-fav" in r.text and "お気に入りに入れました" in r.text
    assert 'aria-pressed="true"' in alice.get(f"/d/{did}").text
    # 一覧に印・お気に入りの画面に出る・サイドのメニュー
    listing = alice.get(f"/f/{alice.root}").text
    assert 'class="fav-mark"' in listing and 'href="/favorites"' in listing
    fav_page = alice.get("/favorites").text
    assert "見積書.txt" in fav_page and "マイファイル" in fav_page
    r = alice.post("/x/favorite", data={"type": "document", "id": did, "on": "0"}, headers={"HX-Request": "true"})
    assert 'aria-pressed="false"' in r.text
    assert "まだありません" in alice.get("/favorites").text
    # フォルダの画面にもある
    assert 'hx-vals=\'{"type": "folder", "id": ' in alice.get(f"/f/{alice.root}").text


# ---------------------------------------------------------------------------
# 最近開いたファイル
# ---------------------------------------------------------------------------

def test_recent_documents(login, users, conn, monkeypatch):
    alice = login("alice")
    ids = [_doc(alice, f"{i}.txt", f"body {i}".encode()) for i in range(4)]
    for i in (0, 1, 2, 0):
        alice.get(f"/d/{ids[i]}")
    rec = alice.get("/api/v1/me/recent").json()["documents"]
    assert [x["id"] for x in rec] == [ids[0], ids[2], ids[1]]          # 開き直したものが先頭、重ならない
    assert "opened_at" in rec[0]
    # API での取得は数えない
    alice.get(f"/api/v1/documents/{ids[3]}")
    assert len(alice.get("/api/v1/me/recent").json()["documents"]) == 3
    # ゴミ箱の中は出さない
    alice.delete(f"/api/v1/documents/{ids[2]}")
    assert [x["id"] for x in alice.get("/api/v1/me/recent").json()["documents"]] == [ids[0], ids[1]]
    page = alice.get("/favorites").text
    assert "最近開いたファイル" in page and "0.txt" in page
    # 件数の上限
    monkeypatch.setattr(ps, "RECENT_KEEP", 2)
    alice.get(f"/d/{ids[3]}")
    assert conn.execute("SELECT count(*) FROM recent_documents WHERE user_id = ?",
                        (users["alice"].id,)).fetchone()[0] == 2
    # 消す
    r = alice.post("/favorites/recent/clear", data={"csrf_token": alice.csrf}, follow_redirects=False)
    assert r.headers["location"] == "/favorites?done=recent_cleared"
    assert alice.get("/api/v1/me/recent").json()["documents"] == []


def test_recent_is_per_user(login, users):
    alice, bob = login("alice"), login("bob")
    did = _doc(alice, "a.txt")
    alice.get(f"/d/{did}")
    assert bob.get("/api/v1/me/recent").json()["documents"] == []


# ---------------------------------------------------------------------------
# 検索の履歴・保存した検索
# ---------------------------------------------------------------------------

def test_search_history(login, users, monkeypatch):
    alice = login("alice")
    alice.get("/search", params={"q": "請求書"})
    alice.get("/search", params={"q": "契約", "type": "pdf"})
    alice.get("/search", params={"q": "請求書"})                       # 同じ条件は 1 件にまとめる
    alice.get("/search", params={"q": "請求書", "page": 2})            # ページ送りは数えない
    alice.get("/search")                                               # 条件なしは数えない
    alice.get("/api/v1/search", params={"q": "API からの検索"})         # API は数えない
    h = alice.get("/api/v1/me/search-history").json()["history"]
    assert [x["params"] for x in h] == [{"q": "請求書"}, {"q": "契約", "type": "pdf"}]
    assert h[1]["url"] == "/search?q=%E5%A5%91%E7%B4%84&type=pdf" and h[1]["label"] == "「契約」 ・ 種類: pdf"
    # 条件なしの検索画面に出る（結果があるときは出さない）
    page = alice.get("/search").text
    assert "最近の検索" in page and "「契約」 ・ 種類: pdf" in page
    assert "最近の検索" not in alice.get("/search", params={"q": "請求書"}).text
    # 1 件消す・すべて消す
    r = alice.post(f"/search/history/{h[0]['id']}/delete", data={"csrf_token": alice.csrf}, follow_redirects=False)
    assert r.headers["location"] == "/search?done=history_deleted"
    assert len(alice.get("/api/v1/me/search-history").json()["history"]) == 1
    assert alice.delete("/api/v1/me/search-history").status_code == 204
    assert alice.get("/api/v1/me/search-history").json()["history"] == []
    # ほかの人の履歴は消せない・見えない
    alice.get("/search", params={"q": "秘密"})
    bob = login("bob")
    assert bob.get("/api/v1/me/search-history").json()["history"] == []
    hid = alice.get("/api/v1/me/search-history").json()["history"][0]["id"]
    assert bob.delete(f"/api/v1/me/search-history/{hid}").status_code == 404
    # 件数の上限
    monkeypatch.setattr(ps, "HISTORY_KEEP", 3)
    for i in range(5):
        alice.get("/search", params={"q": f"語{i}"})
    assert [x["params"]["q"] for x in alice.get("/api/v1/me/search-history").json()["history"]] == ["語4", "語3", "語2"]


def test_saved_searches_web(login):
    alice = login("alice")
    r = alice.get("/search", params={"q": "請求書", "type": "pdf", "taken_from": "2026-01-01"})
    assert "この検索を保存" in r.text and 'name="p_taken_from" value="2026-01-01"' in r.text
    r = alice.post("/search/saved", data={"csrf_token": alice.csrf, "name": "今年の請求書", "p_q": "請求書",
                                          "p_type": "pdf", "p_taken_from": "2026-01-01", "p_mode": "hybrid"},
                   follow_redirects=False)
    assert r.status_code == 303 and "done=search_saved" in r.headers["location"]
    [s] = alice.get("/api/v1/me/saved-searches").json()["saved_searches"]
    assert s["name"] == "今年の請求書" and s["params"] == {"q": "請求書", "type": "pdf", "taken_from": "2026-01-01"}
    page = alice.get("/search").text
    assert "保存した検索" in page and "今年の請求書" in page and s["url"].replace("&", "&amp;") in page
    # 名前を変える・消す
    r = alice.post(f"/search/saved/{s['id']}/rename", data={"csrf_token": alice.csrf, "name": "請求書（今年）"},
                   follow_redirects=False)
    assert r.headers["location"] == "/search?done=search_renamed"
    assert "請求書（今年）" in alice.get("/search").text
    r = alice.post(f"/search/saved/{s['id']}/delete", data={"csrf_token": alice.csrf}, follow_redirects=False)
    assert r.headers["location"] == "/search?done=search_deleted"
    assert alice.get("/api/v1/me/saved-searches").json()["saved_searches"] == []


def test_saved_searches_api(login, monkeypatch):
    alice, bob = login("alice"), login("bob")
    r = alice.post("/api/v1/me/saved-searches", json={"params": {"q": "契約", "unknown": "x"}})
    assert r.status_code == 201 and r.json()["name"] == "契約" and r.json()["params"] == {"q": "契約"}
    sid = r.json()["id"]
    assert alice.post("/api/v1/me/saved-searches", json={"name": "空", "params": {}}).status_code == 422
    assert alice.patch(f"/api/v1/me/saved-searches/{sid}", json={"name": ""}).status_code == 422
    assert bob.patch(f"/api/v1/me/saved-searches/{sid}", json={"name": "x"}).status_code == 404
    assert bob.delete(f"/api/v1/me/saved-searches/{sid}").status_code == 404
    assert alice.patch(f"/api/v1/me/saved-searches/{sid}", json={"name": "契約書"}).status_code == 204
    assert alice.get("/api/v1/me/saved-searches").json()["saved_searches"][0]["name"] == "契約書"
    monkeypatch.setattr(ps, "SAVED_MAX", 1)
    assert alice.post("/api/v1/me/saved-searches", json={"params": {"q": "x"}}).status_code == 422
    assert alice.delete(f"/api/v1/me/saved-searches/{sid}").status_code == 204


def test_describe_search():
    assert ps.describe_search({"q": "請求書", "mode": "keyword", "tag": "経理", "after": "2026-01-01"}) == \
        "「請求書」 ・ キーワード ・ タグ: 経理 ・ 更新日: 2026-01-01〜"
    assert ps.describe_search({"taken_to": "2025-12-31"}) == "撮影日: 〜2025-12-31"
    assert re.fullmatch(r"/search\?q=.+", ps.search_url({"q": "a b"}))
