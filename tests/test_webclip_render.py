"""JavaScript で描くページの描画（docs/design/extensions.md 6.10）。

描画サービスは偽の描画（monkeypatch）と、実際の Chromium（入っていれば）の両方で確かめる。
実際の Chromium は DOCSEED_TEST_CHROMIUM（実行ファイルの場所）か Playwright の既定の場所を使う。
"""

from __future__ import annotations

import ipaddress
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from docseed.webclip import browser, render
from docseed.webclip.render import Rendered, RenderError
from docseed.webclip.safefetch import FetchError
from tests.test_pipeline import run_all, worker  # noqa: F401 - fixture
from tests.test_webclip import ARTICLE, Site, fetcher_for, run_fetcher, setup_article, site  # noqa: F401

SHELL = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>読み込み中</title>
<script src="/app.js"></script></head><body><div id="root"></div>
<noscript>このページを表示するには JavaScript を有効にしてください</noscript></body></html>"""

RENDERED = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>読み込み中</title>
<meta property="og:title" content="描画されたページ"><meta property="og:site_name" content="描画サイト">
</head><body><div id="root"><article><h1>描画されたページ</h1>""" + "".join(
    f"<p>第{i}段落。JavaScript で組み立てられた本文です。構成は描画の後に決まります。</p>" for i in range(1, 6)
) + "</article></div></body></html>"


@pytest.fixture()
def settings(settings):  # noqa: F811 - conftest の settings に Web クリップと描画サービスを足す
    return replace(settings, webclip_enabled=True, webclip_render_url="http://renderer.test:8080")


def setup_spa(srv: Site):
    srv.routes["/spa"] = (200, {"Content-Type": "text/html; charset=utf-8"}, SHELL.encode())


# ---------------------------------------------------------------------------
# 判定と、描画サービスへの依頼
# ---------------------------------------------------------------------------

def test_detects_client_rendered_pages():
    url = "https://example.com/x"
    assert render.looks_client_rendered(SHELL, url) is True
    assert render.looks_client_rendered(ARTICLE, url) is False


def test_render_client_success_and_errors(monkeypatch):
    seen = {}

    class Resp:
        def __init__(self, status, body=None, text=None):
            self.status_code, self._body, self._text = status, body, text

        def json(self):
            if self._text is not None:
                raise ValueError("not json")
            return self._body

    class FakeClient:
        responses = []

        def __init__(self, **kw):
            seen["kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            seen["url"], seen["json"] = url, json
            return FakeClient.responses.pop(0)

    monkeypatch.setattr(render.httpx, "Client", FakeClient)
    FakeClient.responses = [Resp(200, {"html": "<p>x</p>", "final_url": "https://example.com/final"})]
    r = render.render("http://renderer.test:8080/", "https://example.com/a", max_bytes=1000)
    assert r == Rendered("<p>x</p>", "https://example.com/final")
    assert seen["url"] == "http://renderer.test:8080/render" and seen["json"]["max_bytes"] == 1000
    assert seen["kw"]["trust_env"] is False       # 内部の描画サービスにはプロキシを使わない

    FakeClient.responses = [Resp(422, {"message": "描画できませんでした（ブロック）"})]
    with pytest.raises(RenderError, match="ブロック"):
        render.render("http://renderer.test:8080", "https://example.com/a", max_bytes=1000)

    FakeClient.responses = [Resp(200, text="<html>")]
    with pytest.raises(RenderError):
        render.render("http://renderer.test:8080", "https://example.com/a", max_bytes=1000)

    FakeClient.responses = [Resp(200, {"html": "<p>x</p>", "final_url": "file:///etc/passwd"})]
    with pytest.raises(RenderError):
        render.render("http://renderer.test:8080", "https://example.com/a", max_bytes=1000)

    with pytest.raises(RenderError):
        render.render("http://renderer.test:8080", "ftp://example.com/a", max_bytes=1000)


def test_render_client_unreachable():
    with pytest.raises(RenderError):
        render.render("http://127.0.0.1:9", "https://example.com/a", max_bytes=1000)


# ---------------------------------------------------------------------------
# 描画サービスの通信の確認（取得と同じ基準）
# ---------------------------------------------------------------------------

def public_only(ip):
    return ipaddress.ip_address("93.184.216.34") == ip


def test_guard_allows_only_public_http_https():
    table = {"cdn.example.com": ["93.184.216.34"], "lan.example.com": ["192.168.1.10"],
             "mixed.example.com": ["93.184.216.34", "10.0.0.5"]}
    def resolver(h, p):
        if h not in table:
            raise OSError("名前が引けません")     # 実際の名前の解決と同じ
        return table[h]

    g = browser.Guard(resolver=resolver, ip_allowed=public_only, ports={80, 443})
    assert g.allowed("https://cdn.example.com/app.js") is True
    assert g.allowed("http://cdn.example.com:80/a") is True
    assert g.allowed("https://lan.example.com/") is False                 # LAN を指す名前
    assert g.allowed("https://mixed.example.com/") is False               # 答えに 1 つでも LAN があれば拒否
    assert g.allowed("http://10.9.9.9/") is False                         # LAN の IP を直接
    assert g.allowed("http://1.1.1.1/") is False                          # 公開の IP でも、許可の確認に通らなければ拒否
    assert g.allowed("https://cdn.example.com:8443/") is False            # 許可していないポート
    assert g.allowed("https://user:pw@cdn.example.com/") is False         # 認証情報
    assert g.allowed("ftp://cdn.example.com/") is False
    assert g.allowed("data:text/plain,hi") is True and g.allowed("blob:x") is True
    assert g.allowed("https://nowhere.example.com/") is False             # 名前が引けない


def test_guard_request_limit():
    g = browser.Guard(resolver=lambda h, p: ["93.184.216.34"], ip_allowed=public_only)
    g.count = browser.MAX_REQUESTS
    assert g.allowed("https://cdn.example.com/") is False


def test_render_server_protocol(monkeypatch):
    class Fake:
        def __init__(self):
            self.calls = []

        def render(self, url, *, max_bytes):
            self.calls.append((url, max_bytes))
            if "blocked" in url:
                raise FetchError("url_blocked", "止めました")
            if "boom" in url:
                raise RuntimeError("secret detail")
            return {"html": "<p>ok</p>", "final_url": url, "status": 200}

    fake = Fake()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), browser.make_handler(fake))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import http.client

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = r.status, r.read().decode()
        c.close()
        return out

    try:
        assert call("GET", "/healthz") == (200, "ok")
        assert call("GET", "/nope")[0] == 404
        s, b = call("POST", "/render", {"url": "https://example.com/a", "max_bytes": 100})
        assert s == 200 and json.loads(b)["html"] == "<p>ok</p>" and fake.calls == [("https://example.com/a", 100)]
        assert call("POST", "/render", {"url": "ftp://127.0.0.1/", "max_bytes": 1})[0] == 400    # 形式が不正
        assert call("POST", "/render", {"url": "https://example.com/a"})[0] == 400              # 上限が無い
        assert call("POST", "/render", {"url": "https://example.com/blocked", "max_bytes": 1})[0] == 422
        s, b = call("POST", "/render", {"url": "https://example.com/boom", "max_bytes": 1})
        assert s == 500 and "secret" not in b                                  # 内部の詳細は返さない
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# 取得の流れへの組み込み（偽の描画）
# ---------------------------------------------------------------------------

def test_client_rendered_page_is_saved_rendered(login, settings, site, worker, monkeypatch):  # noqa: F811
    setup_article(site)
    setup_spa(site)
    calls = []

    def fake_render(render_url, url, *, max_bytes):
        calls.append((render_url, url, max_bytes))
        return Rendered(RENDERED, site.url("/spa"))

    monkeypatch.setattr(render, "render", fake_render)
    alice = login("alice")
    r1 = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/spa")})
    r2 = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/article")})
    run_fetcher(settings, site)
    # 本文が短いページだけ描画する（記事は描画しない）
    assert calls == [("http://renderer.test:8080", site.url("/spa"), settings.webclip_max_mb * 1024 * 1024)]
    c = alice.get(f"/api/v1/webclips/{r1.json()['id']}").json()
    assert c["status"] == "done" and c["rendered"] is True and c["title"] == "描画されたページ"
    assert c["site_name"] == "描画サイト"
    assert alice.get(f"/api/v1/webclips/{r2.json()['id']}").json()["rendered"] is False
    run_all(worker)
    doc = alice.get(f"/api/v1/documents/{c['document_id']}").json()
    text = alice.get(f"/api/v1/documents/{doc['id']}/text").json()["pages"][0]["text"]
    assert "JavaScript で組み立てられた本文" in text and "第5段落" in text
    hits = alice.get("/api/v1/search", params={"q": "組み立てられた本文", "mode": "keyword"}).json()
    assert hits["results"][0]["document"]["id"] == doc["id"]


def test_render_failure_falls_back_to_static_page(login, settings, site, worker, monkeypatch):  # noqa: F811
    setup_spa(site)

    def failing(render_url, url, *, max_bytes):
        raise RenderError("描画サービスに接続できませんでした")

    monkeypatch.setattr(render, "render", failing)
    alice = login("alice")
    r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/spa")})
    run_fetcher(settings, site)
    c = alice.get(f"/api/v1/webclips/{r.json()['id']}").json()
    assert c["status"] == "done" and c["rendered"] is False and c["title"] == "読み込み中"
    run_all(worker)
    doc = alice.get(f"/api/v1/documents/{c['document_id']}").json()
    assert "webclip_text_short" in [w["code"] for w in doc["processing"]["warnings"]]


def test_rendering_is_off_by_default(login, settings, site, monkeypatch):  # noqa: F811
    setup_spa(site)
    settings_off = replace(settings, webclip_render_url="")

    def must_not_call(*a, **k):
        raise AssertionError("描画サービスを呼んではいけない")

    monkeypatch.setattr(render, "render", must_not_call)
    alice = login("alice")
    r = alice.post("/api/v1/webclips", json={"folder_id": alice.root, "url": site.url("/spa")})
    run_fetcher(settings_off, site)
    assert alice.get(f"/api/v1/webclips/{r.json()['id']}").json()["rendered"] is False


# ---------------------------------------------------------------------------
# 実際の Chromium（入っていれば）
# ---------------------------------------------------------------------------

JS_PAGE = """<!doctype html><html><head><title>JS</title></head><body><div id="root"></div><script>
var lan = "pending";
fetch("http://10.9.9.9/", {mode: "no-cors"}).then(function () { lan = "reached"; })
  .catch(function () { lan = "blocked"; });
setTimeout(function () {
  document.getElementById("root").innerHTML = "<article><h1>描画された見出し</h1><p>" +
    "JavaScript が組み立てた本文です。".repeat(12) + "</p><p>LAN:" + lan + "</p></article>";
}, 300);
</script></body></html>"""


@pytest.fixture()
def chromium():
    pytest.importorskip("playwright.sync_api")
    import os
    path = os.environ.get("DOCSEED_TEST_CHROMIUM") or None
    try:
        r = browser.Renderer(executable_path=path, no_sandbox=True)
    except Exception as e:  # noqa: BLE001 - Chromium が無い環境では飛ばす
        pytest.skip(f"Chromium を起動できません: {str(e).splitlines()[0]}")
    yield r
    r.close()


def test_real_browser_runs_scripts_and_blocks_lan(chromium):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = JS_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # テストのサーバーは 127.0.0.1 にあるので、ループバックだけ許し、ポートは実際のものを許す
    guard = lambda: browser.Guard(ip_allowed=lambda ip: ip.is_loopback, ports={port})  # noqa: E731
    chromium.guard_factory = guard
    try:
        out = chromium.render(f"http://127.0.0.1:{port}/js", max_bytes=5_000_000)
    finally:
        srv.shutdown()
    assert "描画された見出し" in out["html"] and "組み立てた本文" in out["html"]
    assert "LAN:blocked" in out["html"] and "LAN:reached" not in out["html"]
