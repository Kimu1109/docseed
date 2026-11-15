"""Phase 8: URL の取り込み（Web クリップ）。docs/design/extensions.md 6 章・10 章。

手元に HTTP サーバーを立て、偽の名前の解決（site.test → 127.0.0.1 など）で確かめる。
既定の安全の確認（グローバルな IP だけ）は、テスト用の許可（127.0.0.1 だけ）とは別に確かめる。
"""

from __future__ import annotations

import io
import ipaddress
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from docseed.db import connect
from docseed.jobs import queue
from docseed.webclip import meta as mt
from docseed.webclip.fetcher import Fetcher
from docseed.webclip.safefetch import FetchError, SafeFetcher, check_url, ip_is_public
from tests.test_pipeline import make_pdf, run_all, worker  # noqa: F401 - fixture


@pytest.fixture()
def settings(settings):  # noqa: F811 - conftest の settings に Web クリップの有効化を足す
    return replace(settings, webclip_enabled=True)


def png_bytes(size=(300, 200)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, (200, 80, 40)).save(b, "PNG")
    return b.getvalue()


ARTICLE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>ページのタイトル（title 要素）</title>
<meta property="og:title" content="領収書の電子保存のすすめ">
<meta property="og:description" content="紙の領収書をスキャンして電子で保存する手順をまとめました。">
<meta property="og:site_name" content="家計の記録">
<meta property="og:image" content="/img/cover.png">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<meta property="article:published_time" content="2026-10-01T09:00:00+09:00">
<link rel="canonical" href="https://example.com/articles/receipts">
<link rel="alternate" type="application/json+oembed" href="/oembed?url=x">
<script>document.write("実行されない")</script>
</head><body><header>メニュー</header><article><h1>領収書の電子保存のすすめ</h1>
""" + "".join(f"<p>第{i}段落。電子帳簿保存法に沿って、領収書は受け取ってから速やかにスキャンし、"
              f"日付・金額・取引先で検索できるように保存します。</p>" for i in range(1, 8)) + """
</article><footer>著作権表示</footer></body></html>"""


class Site:
    """パスごとの応答（status, headers, body）を返す HTTP サーバー。"""

    def __init__(self):
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.requests: list[dict] = []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append({"path": self.path, "host": self.headers.get("Host"),
                                      "cookie": self.headers.get("Cookie"), "ua": self.headers.get("User-Agent")})
                status, headers, body = site.routes.get(self.path.split("?")[0], (404, {}, b"not found"))
                self.send_response(status)
                chunked = headers.pop("_chunked", False) if isinstance(headers, dict) else False
                for k, v in headers.items():
                    self.send_header(k, v)
                if not chunked:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str, host: str = "site.test") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def site():
    s = Site()
    yield s
    s.close()


def fetcher_for(site: Site, names: dict | None = None, calls: list | None = None) -> SafeFetcher:
    """site.test は 127.0.0.1（テストのために 127.0.0.1 だけ許可する）。ほかの名前は names で決める。"""
    table = {"site.test": ["127.0.0.1"], **(names or {})}

    def resolver(host, port):
        if calls is not None:
            calls.append(host)
        v = table[host]
        return v() if callable(v) else v
    return SafeFetcher(resolver=resolver, ports={80, 443, site.port},
                       ip_allowed=lambda ip: ip == ipaddress.ip_address("127.0.0.1"))


def setup_article(site: Site):
    site.routes["/article"] = (200, {"Content-Type": "text/html; charset=utf-8"}, ARTICLE.encode())
    site.routes["/img/cover.png"] = (200, {"Content-Type": "image/png"}, png_bytes())
    site.routes["/oembed"] = (200, {"Content-Type": "application/json"}, json.dumps(
        {"type": "rich", "title": "oEmbed のタイトル", "author_name": "記録係",
         "html": "<blockquote><p>埋め込みの本文（oEmbed）</p></blockquote>"}).encode())


# ---------------------------------------------------------------------------
# 安全な取得（SSRF 対策）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", ["192.168.1.10", "10.0.0.1", "172.16.0.5", "127.0.0.1", "169.254.169.254",
                                "100.64.1.1", "::1", "fd00::1", "::ffff:192.168.1.1", "224.0.0.1", "0.0.0.0"])
def test_private_addresses_blocked_by_default(ip):
    assert not ip_is_public(ipaddress.ip_address(ip))
    f = SafeFetcher(resolver=lambda h, p: [ip])
    with pytest.raises(FetchError) as e:
        f.fetch("http://evil.test/", max_bytes=1000)
    assert e.value.code == "url_blocked" and not e.value.retry


def test_any_private_address_in_answer_is_blocked():
    f = SafeFetcher(resolver=lambda h, p: ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(FetchError) as e:
        f.fetch("http://mixed.test/", max_bytes=1000)
    assert e.value.code == "url_blocked"


def test_ip_literal_and_ports():
    f = SafeFetcher()
    for url in ("http://127.0.0.1/", "http://[::1]/", "http://192.168.0.1:8080/"):
        with pytest.raises(FetchError) as e:
            f.fetch(url, max_bytes=10)
        assert e.value.code == "url_blocked"
    with pytest.raises(FetchError) as e:
        SafeFetcher(resolver=lambda h, p: ["93.184.216.34"]).fetch("http://example.com:22/", max_bytes=10)
    assert e.value.code == "url_blocked" and "22" in str(e.value)


@pytest.mark.parametrize("url", ["ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)",
                                 "http://user:pass@example.com/", "https://", "http://example.com:99999/",
                                 "https://example.com/" + "a" * 2100])
def test_check_url_rejects(url):
    with pytest.raises(FetchError) as e:
        check_url(url)
    assert e.value.code == "url_invalid"


def test_redirect_to_private_is_blocked(site):
    site.routes["/go"] = (302, {"Location": site.url("/admin", host="router.test")}, b"")
    f = fetcher_for(site, {"router.test": ["192.168.1.1"]})
    with pytest.raises(FetchError) as e:
        f.fetch(site.url("/go"), max_bytes=1000)
    assert e.value.code == "url_blocked"


def test_redirects_are_followed_and_rechecked(site):
    site.routes["/a"] = (301, {"Location": "/b"}, b"")
    site.routes["/b"] = (200, {"Content-Type": "text/plain"}, b"ok")
    calls: list = []
    r = fetcher_for(site, calls=calls).fetch(site.url("/a"), max_bytes=1000)
    assert r.url == site.url("/b") and r.body == b"ok" and calls == ["site.test", "site.test"]
    site.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(FetchError):
        fetcher_for(site).fetch(site.url("/loop"), max_bytes=1000)


def test_dns_rebinding_uses_checked_address(site):
    """1 回目は許可した IP、2 回目は LAN の IP を返す名前でも、確かめた IP にだけつなぐ。"""
    answers = iter([["127.0.0.1"], ["10.0.0.9"], ["10.0.0.9"]])
    calls: list = []
    site.routes["/x"] = (200, {"Content-Type": "text/plain"}, b"pinned")
    f = fetcher_for(site, {"rebind.test": lambda: next(answers)}, calls)
    r = f.fetch(site.url("/x", host="rebind.test"), max_bytes=1000)
    assert r.body == b"pinned" and calls == ["rebind.test"]
    # Host ヘッダは元の名前、Cookie は送らない、User-Agent は docseed
    req = site.requests[-1]
    assert req["host"] == f"rebind.test:{site.port}" and req["cookie"] is None and req["ua"].startswith("docseed/")


def test_size_limits(site):
    site.routes["/big"] = (200, {"Content-Type": "text/html"}, b"x" * 5000)
    with pytest.raises(FetchError) as e:
        fetcher_for(site).fetch(site.url("/big"), max_bytes=4000)
    assert e.value.code == "fetch_too_large" and not e.value.retry
    # Content-Length を付けない（チャンクで送る）場合も途中で打ち切る
    site.routes["/stream"] = (200, {"Content-Type": "text/html", "Transfer-Encoding": "chunked", "_chunked": True},
                              b"1000\r\n" + b"y" * 4096 + b"\r\n0\r\n\r\n")
    with pytest.raises(FetchError) as e:
        fetcher_for(site).fetch(site.url("/stream"), max_bytes=1000)
    assert e.value.code == "fetch_too_large"
    # 種類ごとの上限
    r = fetcher_for(site).fetch(site.url("/big"), max_bytes=lambda mime: 10_000 if mime == "text/html" else 10)
    assert r.size == 5000


def test_http_errors_and_retry(site):
    site.routes["/gone"] = (410, {}, b"")
    site.routes["/busy"] = (503, {}, b"")
    site.routes["/slow"] = (429, {}, b"")
    for path, retry in (("/missing", False), ("/gone", False), ("/busy", True), ("/slow", True)):
        with pytest.raises(FetchError) as e:
            fetcher_for(site).fetch(site.url(path), max_bytes=100)
        assert e.value.code == "fetch_http_error" and e.value.retry is retry


def test_connection_refused_is_retryable():
    f = SafeFetcher(resolver=lambda h, p: ["127.0.0.1"], ports={1},
                    ip_allowed=lambda ip: True, connect_timeout=1, total_timeout=2)
    with pytest.raises(FetchError) as e:
        f.fetch("http://closed.test:1/", max_bytes=10)
    assert e.value.retry


# ---------------------------------------------------------------------------
# メタデータ
# ---------------------------------------------------------------------------

def test_meta_ogp_and_twitter():
    m = mt.finish(mt.parse(ARTICLE, "https://example.com/a?x=1"), "https://example.com/a?x=1")
    assert m.title == "領収書の電子保存のすすめ" and m.site_name == "家計の記録"
    assert m.description.startswith("紙の領収書") and m.image_url == "https://example.com/img/cover.png"
    assert m.canonical_url == "https://example.com/articles/receipts"
    assert m.oembed_url == "https://example.com/oembed?url=x"
    assert m.published_at.startswith("2026-10-01") and m.og_type == "article"
    assert m.twitter_card == "summary_large_image" and m.raw["og:title"] == "領収書の電子保存のすすめ"


def test_meta_fallbacks_and_json_ld():
    page = """<html><head><title> 普通の  タイトル </title>
    <meta name="twitter:description" content="ツイッターの説明"><meta name="twitter:image" content="https://cdn.example/i.png">
    <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Article",
      "author":[{"@type":"Person","name":"山田 太郎"}],"datePublished":"2025-05-05"}]}</script></head></html>"""
    m = mt.finish(mt.parse(page, "https://blog.example.org/p/1"), "https://blog.example.org/p/1")
    assert m.title == "普通の タイトル" and m.description == "ツイッターの説明"
    assert m.image_url == "https://cdn.example/i.png" and m.author == "山田 太郎" and m.published_at == "2025-05-05"
    assert m.site_name == "blog.example.org" and m.canonical_url == "https://blog.example.org/p/1"
    empty = mt.finish(mt.parse("", "https://x.example/"), "https://x.example/")
    assert empty.title == "https://x.example/"


def test_charset_detection():
    sjis = "<html><head><meta charset=\"Shift_JIS\"><title>日本語</title></head></html>".encode("cp932")
    assert "日本語" in mt.decode_html(sjis, None)
    euc = "<html><title>日本語のページです。文字コードの判定</title></html>".encode("euc_jp")
    assert "日本語" in mt.decode_html(euc, "EUC-JP")
    assert "日本語" in mt.decode_html("日本語".encode(), None)


def test_oembed_text_and_file_name():
    t = mt.oembed_text({"title": "T", "author_name": "A", "html": "<blockquote><p>本文<b>太字</b></p></blockquote>"})
    assert t == "T\nA\n本文太字"
    assert mt.file_name("a/b: c?") == "a b c.url"
    assert len(mt.file_name("あ" * 200).encode()) <= 255


# ---------------------------------------------------------------------------
# 取り込みの流れ（api → fetcher → worker）
# ---------------------------------------------------------------------------

def run_fetcher(settings, site, **kw) -> Fetcher:
    f = Fetcher(settings, fetcher_for(site, **kw))
    while f.run_once():
        pass
    return f


def test_webclip_end_to_end(login, settings, site, worker):  # noqa: F811
    setup_article(site)
    alice = login("alice")
    r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/article")})
    assert r.status_code == 202 and r.json()["status"] == "pending" and r.json()["already_saved"] == []
    cid = r.json()["id"]
    # 取得待ちの間は一覧に「取得中」の行が出る
    assert "取得中" in alice.get(f"/f/{alice.root}").text
    # worker は fetch_url を取らない
    conn = connect(settings.db_path)
    assert queue.claim(conn) is None
    run_fetcher(settings, site)
    c = alice.get(f"/api/v1/webclips/{cid}").json()
    assert c["status"] == "done" and c["title"] == "領収書の電子保存のすすめ" and c["site_name"] == "家計の記録"
    assert c["canonical_url"] == "https://example.com/articles/receipts" and c["http_status"] == 200
    doc = alice.get(f"/api/v1/documents/{c['document_id']}").json()
    assert doc["kind"] == "webclip" and doc["filename"] == "領収書の電子保存のすすめ.url"
    assert doc["mime_type"] == "text/html" and doc["webclip"]["image_url"]
    assert doc["webclip"]["url"] == site.url("/article")
    # サムネイルは og:image から
    t = alice.get(doc["webclip"]["image_url"])
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp"
    # 文字の読み取り: タイトル・サイト名・説明・本文・oEmbed が 1 ページ目に入る
    run_all(worker)
    text = alice.get(f"/api/v1/documents/{doc['id']}/text").json()["pages"][0]["text"]
    assert text.startswith("領収書の電子保存のすすめ\n\n家計の記録\n\n紙の領収書")
    assert "第7段落" in text and "埋め込みの本文(oEmbed)" in text    # 全角の括弧は正規化される
    assert "メニュー" not in text and "実行されない" not in text
    assert alice.get(f"/api/v1/documents/{doc['id']}").json()["processing"]["warnings"] == []
    hits = alice.get("/api/v1/search", params={"q": "電子帳簿保存法", "mode": "keyword"}).json()
    assert hits["results"][0]["document"]["id"] == doc["id"]
    # 画面: 種類は Web クリップ。保存した HTML は表示しない
    page = alice.get(f"/d/{doc['id']}").text
    assert "Web クリップ" in page and "元のページを開く" in page and 'rel="noopener noreferrer"' in page
    assert "<iframe" not in page
    # 同じページをもう一度追加すると、既に保存されていることを知らせる（保存はできる）
    r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "https://example.com/articles/receipts"})
    assert r.status_code == 202 and r.json()["already_saved"][0]["id"] == doc["id"]


def test_refetch_replaces_content(login, settings, site, worker):  # noqa: F811
    site.routes["/p"] = (200, {"Content-Type": "text/html"}, "<html><title>版 1</title><p>古い本文</p></html>".encode())
    alice = login("alice")
    cid = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/p")}).json()["id"]
    run_fetcher(settings, site)
    did = alice.get(f"/api/v1/webclips/{cid}").json()["document_id"]
    run_all(worker)
    v2 = "<html><title>版 2</title><p>新しい本文</p></html>".encode()
    site.routes["/p"] = (200, {"Content-Type": "text/html"}, v2)
    r = alice.post(f"/d/{did}/refetch", data={"csrf_token": alice.csrf}, follow_redirects=False)
    assert r.status_code == 303
    run_fetcher(settings, site)
    run_all(worker)
    c = alice.get(f"/api/v1/webclips/{cid}").json()
    assert c["document_id"] == did and c["title"] == "版 2"
    text = alice.get(f"/api/v1/documents/{did}/text").json()["pages"][0]["text"]
    assert "新しい本文" in text and "古い本文" not in text
    conn = connect(settings.db_path)
    # 前に取得した中身は過去の版として残り、版を消すと blob も消える
    assert conn.execute("SELECT count(*) FROM blobs WHERE mime_type = 'text/html'").fetchone()[0] == 2
    vers = alice.get(f"/api/v1/documents/{did}/versions").json()["versions"]
    assert len(vers) == 1
    assert "古い本文" in alice.get(f"/api/v1/documents/{did}/versions/{vers[0]['id']}/content").text
    assert alice.delete(f"/api/v1/documents/{did}/versions/{vers[0]['id']}").status_code == 204
    assert conn.execute("SELECT count(*) FROM blobs WHERE mime_type = 'text/html'").fetchone()[0] == 1
    # 本文が短いページには警告
    warn = alice.get(f"/api/v1/documents/{did}").json()["processing"]["warnings"]
    assert warn == [{"code": "webclip_text_short"}]
    assert "本文を取り出せませんでした" in alice.get(f"/d/{did}").text


def test_file_urls_are_saved_as_files(login, settings, site, worker):  # noqa: F811
    site.routes["/docs/manual.pdf"] = (200, {"Content-Type": "application/pdf"}, make_pdf(["Manual text here"]))
    site.routes["/fake.png"] = (200, {"Content-Type": "image/png"}, b"<html>not an image</html>")
    site.routes["/app.bin"] = (200, {"Content-Type": "application/octet-stream"}, b"\x00\x01")
    alice = login("alice")
    ids = {p: alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url(p)}).json()["id"]
           for p in ("/docs/manual.pdf", "/fake.png", "/app.bin")}
    run_fetcher(settings, site)
    pdf = alice.get(f"/api/v1/webclips/{ids['/docs/manual.pdf']}").json()
    d = alice.get(f"/api/v1/documents/{pdf['document_id']}").json()
    assert d["kind"] == "file" and d["filename"] == "manual.pdf" and d["mime_type"] == "application/pdf"
    for p in ("/fake.png", "/app.bin"):
        c = alice.get(f"/api/v1/webclips/{ids[p]}").json()
        assert c["status"] == "failed" and c["error"]["code"] == "fetch_unsupported_type" and c["document_id"] is None


def test_failures_and_retries(login, settings, site):
    site.routes["/busy"] = (503, {}, b"")
    alice = login("alice")
    missing = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/nope")}).json()["id"]
    busy = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/busy")}).json()["id"]
    run_fetcher(settings, site)
    conn = connect(settings.db_path)
    m = alice.get(f"/api/v1/webclips/{missing}").json()
    assert m["status"] == "failed" and m["error"]["code"] == "fetch_http_error" and m["http_status"] == 404
    b = alice.get(f"/api/v1/webclips/{busy}").json()
    assert b["status"] == "pending" and b["error"]["code"] == "fetch_http_error"   # 1 分後にやり直す
    job = conn.execute("SELECT * FROM jobs WHERE type = 'fetch_url' AND status = 'pending'").fetchone()
    assert job["attempts"] == 1 and job["run_after"] > job["created_at"]
    # 一覧には失敗の理由と「もう一度取得」
    html = alice.get(f"/f/{alice.root}").text
    assert "取得に失敗" in html and "HTTP 404" in html and f"/x/webclips/{missing}/retry" in html
    site.routes["/nope"] = (200, {"Content-Type": "text/plain"}, b"now it exists")
    r = alice.post(f"/x/webclips/{missing}/retry", data={"current": alice.root})
    assert r.status_code == 200
    run_fetcher(settings, site)
    assert alice.get(f"/api/v1/webclips/{missing}").json()["status"] == "done"
    # 取り消し
    assert alice.delete(f"/api/v1/webclips/{busy}").status_code == 204
    assert conn.execute("SELECT count(*) FROM jobs WHERE type = 'fetch_url' AND status = 'pending'").fetchone()[0] == 0


def test_blocked_url_is_not_retried(login, settings, site):
    alice = login("alice")
    cid = alice.post("/api/v1/webclips", json={"folder_id": alice.root,
                                               "url": site.url("/x", host="nas.test")}).json()["id"]
    run_fetcher(settings, site, names={"nas.test": ["192.168.1.2"]})
    c = alice.get(f"/api/v1/webclips/{cid}").json()
    assert c["status"] == "failed" and c["error"]["code"] == "url_blocked"


def test_api_validation_and_permissions(login, settings, app):
    alice, bob = login("alice"), login("bob")
    r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "ftp://example.com/"})
    assert r.status_code == 422
    r = bob.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "https://example.com/"})
    assert r.status_code == 404
    cid = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "https://example.com/"}).json()["id"]
    assert bob.get(f"/api/v1/webclips/{cid}").status_code == 404
    app.state.settings = replace(settings, webclip_enabled=False)
    try:
        r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "https://example.com/"})
        assert r.status_code == 409 and r.json()["code"] == "webclip_disabled"
        assert "URL を追加" not in alice.get(f"/f/{alice.root}").text
    finally:
        app.state.settings = settings


def test_bookmarklet_page(login, settings):
    alice = login("alice")
    assert "javascript:location.href=" in alice.get("/settings").text
    page = alice.get("/clip", params={"url": "https://example.com/a"}).text
    assert 'value="https://example.com/a"' in page                     # GET だけでは保存しない
    conn = connect(settings.db_path)
    assert conn.execute("SELECT count(*) FROM web_clips").fetchone()[0] == 0
    r = alice.post("/clip", data={"csrf_token": alice.csrf, "url": "https://example.com/a",
                                  "folder_id": alice.root}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/f/{alice.root}?done=clip")
    assert conn.execute("SELECT count(*) FROM web_clips").fetchone()[0] == 1


def test_web_form_and_purge_cleans_up(login, settings, site, worker):  # noqa: F811
    setup_article(site)
    alice = login("alice")
    html = alice.post(f"/f/{alice.root}/webclips", data={"url": site.url("/article")}).text
    assert "URL を受け付けました" in html
    run_fetcher(settings, site)
    conn = connect(settings.db_path)
    did = conn.execute("SELECT document_id FROM web_clips").fetchone()[0]
    img = conn.execute("SELECT image_blob_id FROM web_clips").fetchone()[0]
    assert img is not None
    alice.delete(f"/api/v1/documents/{did}")
    # ゴミ箱の間は og:image も残る。完全に削除するとクリップと og:image の blob も消える
    assert conn.execute("SELECT count(*) FROM blobs WHERE id = ?", (img,)).fetchone()[0] == 1
    assert alice.delete(f"/api/v1/trash/document/{did}").status_code == 204
    assert conn.execute("SELECT count(*) FROM web_clips").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


def test_uploaded_html_uses_trafilatura(login, worker):  # noqa: F811
    alice = login("alice")
    d = alice.upload(alice.root, {"page.html": ARTICLE.encode()}).json()["results"][0]["document"]
    run_all(worker)
    text = alice.get(f"/api/v1/documents/{d['id']}/text").json()["pages"][0]["text"]
    assert "第3段落" in text and "メニュー" not in text and "著作権表示" not in text


def test_https_pins_ip_but_verifies_name(tmp_path):
    """IP に直接つなぎながら、SNI と証明書の名前の確認は元の名前で行う。"""
    import ssl

    from cryptography.hazmat.primitives import serialization

    from tests.test_tls import _ca, _leaf

    ca_key, ca_cert = _ca()
    key, cert = _leaf(ca_key, ca_cert, 30, name="secure.test")
    (tmp_path / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    site = Site()
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
        site.server.socket = ctx.wrap_socket(site.server.socket, server_side=True)
        site.routes["/s"] = (200, {"Content-Type": "text/plain"}, b"secure")
        trust = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
        ok = SafeFetcher(resolver=lambda h, p: ["127.0.0.1"], ports={site.port}, verify=trust,
                         ip_allowed=lambda ip: True)
        r = ok.fetch(f"https://secure.test:{site.port}/s", max_bytes=100)
        assert r.body == b"secure" and site.requests[-1]["host"] == f"secure.test:{site.port}"
        # 証明書の名前と違う名前では、同じ IP でもつながらない
        with pytest.raises(FetchError) as e:
            ok.fetch(f"https://other.test:{site.port}/s", max_bytes=100)
        assert e.value.code == "fetch_connect_error"
    finally:
        site.close()


def test_fetcher_stale_alert(login, settings):
    from docseed.monitor import status
    alice = login("alice")
    alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": "https://example.com/"})
    conn = connect(settings.db_path)
    assert not any(a["code"] == "fetcher_stale" for a in status(conn, settings)["alerts"])
    conn.execute("UPDATE jobs SET created_at = '2000-01-01T00:00:00Z' WHERE type = 'fetch_url'")
    assert any(a["code"] == "fetcher_stale" for a in status(conn, settings)["alerts"])
