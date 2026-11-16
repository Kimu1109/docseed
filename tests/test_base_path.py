"""URL の起点（DOCSEED_BASE_PATH）。https://example.com/docseed/ のようにパスの下で動かす。"""

from __future__ import annotations

import io
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from docseed.app import create_app
from docseed.config import Settings, normalize_base_path
from docseed.db import connect
from docseed.users import create_user

from .conftest import PASSWORD

B = "/docseed"
STATIC = Path(__file__).parent.parent / "src" / "docseed" / "web" / "static"


# ---------------------------------------------------------------------------
# 設定


@pytest.mark.parametrize("raw, want", [
    ("", ""), ("/", ""), ("docseed", "/docseed"), ("/docseed/", "/docseed"), ("/a/b", "/a/b"),
    ("  /docs-1.x_y~  ", "/docs-1.x_y~"),
])
def test_normalize_base_path(raw, want):
    assert normalize_base_path(raw) == want


@pytest.mark.parametrize("raw", ["/a b", "/a/../b", "/./a", "/a?x", "/a#x", "/日本語", "/a//b", '/a"'])
def test_normalize_base_path_rejects(raw):
    with pytest.raises(ValueError):
        normalize_base_path(raw)


def test_from_env():
    s = Settings.from_env({"DOCSEED_BASE_PATH": "docseed/", "DOCSEED_HOST": "example.com"})
    assert s.base_path == B
    assert s.public_url == "https://example.com"          # パスキーの origin（パスを含まない）
    assert s.public_base == "https://example.com/docseed"
    # DOCSEED_PUBLIC_URL にパスまで書いた場合は、それを接頭辞として使う
    s = Settings.from_env({"DOCSEED_PUBLIC_URL": "https://example.com/docseed/"})
    assert (s.base_path, s.public_url, s.public_base) == (B, "https://example.com", "https://example.com/docseed")
    # 両方あれば DOCSEED_BASE_PATH が優先
    s = Settings.from_env({"DOCSEED_PUBLIC_URL": "https://example.com/x", "DOCSEED_BASE_PATH": "/y"})
    assert (s.base_path, s.public_base) == ("/y", "https://example.com/y")
    assert Settings.from_env({}).base_path == ""


def test_base_path_clashing_with_app_paths_is_rejected(tmp_path):
    for bad in ("/s", "/api", "/static", "/d/x"):
        with pytest.raises(RuntimeError, match="重なる"):
            create_app(Settings(data_dir=tmp_path / "d", secret_key="x" * 40, base_path=bad))


# ---------------------------------------------------------------------------
# 画面


@pytest.fixture()
def bsettings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", secret_key="x" * 40, cookie_secure=False,
                    share_upload_min_free_pct=0.0, base_path=B, host="example.com", webclip_enabled=True)


@pytest.fixture()
def bapp(bsettings):
    app = create_app(bsettings)
    c = connect(bsettings.db_path)
    create_user(c, "admin", "管理者", PASSWORD, role="admin")
    c.close()
    return app


def _login(app) -> tuple[TestClient, str]:
    http = TestClient(app, base_url="http://testserver")
    r = http.post(B + "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return http, r.json()["csrf_token"]


def test_bare_prefix_redirects_to_slash(bapp):
    http = TestClient(bapp)
    r = http.get(B + "?a=1", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == B + "/?a=1"


def test_login_redirect_and_cookie_path(bapp):
    http = TestClient(bapp)
    r = http.get(B + "/search?q=x", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == B + "/login?next=/search%3Fq%3Dx" or \
        r.headers["location"].startswith(B + "/login?next=")
    page = http.get(r.headers["location"])
    assert page.status_code == 200 and 'action="/docseed/login"' in page.text
    # フォームでログイン（next は接頭辞なしでも、付きでも受け付け、戻り先には接頭辞が付く）
    for nxt in ("/search?q=x", B + "/search?q=x"):
        h = TestClient(bapp)
        r = h.post(B + "/login", data={"username": "admin", "password": PASSWORD, "next": nxt},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == B + "/search?q=x"
        cookies = r.headers.get_list("set-cookie")
        assert cookies and all("Path=/docseed" in c for c in cookies), cookies
    # 別のサイトへの戻り先は使わない
    h = TestClient(bapp)
    r = h.post(B + "/login", data={"username": "admin", "password": PASSWORD, "next": "//evil.example/"},
               follow_redirects=False)
    assert r.headers["location"] == B + "/"


def test_htmx_redirect_has_prefix(bapp):
    http = TestClient(bapp)
    r = http.get(B + "/trash", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 204 and r.headers["hx-redirect"] == B + "/login"


def test_static_and_api_docs(bapp):
    http = TestClient(bapp)
    assert http.get(B + "/static/app.js").status_code == 200
    docs = http.get(B + "/api/docs")
    assert docs.status_code == 200 and "/docseed/api/v1/openapi.json" in docs.text
    spec = http.get(B + "/api/v1/openapi.json").json()
    assert spec["servers"] == [{"url": B}]


def _session_from(r) -> str:
    return next(c.split(";")[0].split("=", 1)[1] for c in r.headers.get_list("set-cookie")
                if c.startswith("docseed_session="))


def test_stale_root_cookie_does_not_block_login(bapp):
    """前に Path=/ で付いた古い docseed_session が残っていても、ログインできる（security.md 2.2）。

    ブラウザは同じ名前の Cookie を、パスの長いもの（Path=/docseed の新しいもの）を先に、Path=/ の古いものを
    後に送る。後のほうで読むと、ログインに成功したのにログイン画面へ戻されていた。
    """
    r = TestClient(bapp).post(B + "/login", data={"username": "admin", "password": PASSWORD},
                              headers={"Cookie": "docseed_session=stale-from-root"}, follow_redirects=False)
    assert r.status_code == 303
    good = _session_from(r)
    http = TestClient(bapp)
    for cookie in (f"docseed_session={good}; docseed_device=x; docseed_session=stale-from-root",
                   f"docseed_session=stale-from-root; docseed_session={good}"):
        page = http.get(B + "/trash", headers={"Cookie": cookie}, follow_redirects=False)
        assert page.status_code == 200, cookie
    # ログアウトは使っていたセッションを消す
    r = TestClient(bapp).post(B + "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    raw, csrf = _session_from(r), r.json()["csrf_token"]
    cookie = f"docseed_session={raw}; docseed_session=stale-from-root"
    assert http.post(B + "/api/v1/auth/logout", headers={"Cookie": cookie, "X-CSRF-Token": csrf}).status_code == 204
    assert http.get(B + "/trash", headers={"Cookie": f"docseed_session={raw}"},
                    follow_redirects=False).status_code == 303


def test_outer_proxy_that_strips_the_prefix(bapp):
    """nginx の proxy_pass http://pi/; のように、外側で接頭辞を外して送ってきても動く。"""
    http = TestClient(bapp)
    assert http.get("/static/app.js").status_code == 200
    r = http.get("/trash", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(B + "/login?next=")
    page = http.get("/login")
    assert 'action="/docseed/login"' in page.text


_ATTR = re.compile(r'\b(href|src|action|hx-get|hx-post|hx-put|hx-delete|hx-patch|data-[a-z-]+|value|content)'
                   r'="(/[^"]*)"')


def _local_urls(html: str) -> list[tuple[str, str]]:
    return [(a, v) for a, v in _ATTR.findall(html) if not v.startswith("//")]


def _seed(http: TestClient, csrf: str) -> dict:
    h = {"X-CSRF-Token": csrf}
    me = http.get(B + "/api/v1/me").json()
    root = me["root_folder_id"]
    folder = http.post(B + "/api/v1/folders", json={"parent_id": root, "name": "経理"}, headers=h).json()
    up = http.post(B + "/api/v1/documents", headers=h, data={"folder_id": str(root)},
                   files=[("files", ("memo.txt", io.BytesIO("請求書のメモ".encode()), "text/plain"))])
    assert up.status_code in (200, 201), up.text
    doc = up.json()["results"][0]["document"]
    share = http.post(B + "/api/v1/shares", json={"target_type": "document", "target_id": doc["id"]}, headers=h)
    assert share.status_code in (200, 201), share.text
    return {"root": root, "folder": folder["id"], "doc": doc["id"], "share": share.json()}


def test_pages_only_link_inside_the_prefix(bapp):
    """どの画面のリンク・フォーム・htmx・画像も「/docseed/…」を指し、たどれる。"""
    http, csrf = _login(bapp)
    ids = _seed(http, csrf)
    share_url = ids["share"]["url"]
    assert share_url.startswith("http://testserver/docseed/s/")
    pages = ["/", f"/f/{ids['root']}", f"/f/{ids['folder']}", f"/d/{ids['doc']}", "/search?q=請求書", "/settings",
             "/settings/security", "/admin", "/trash", "/tags", "/shared", "/favorites", "/clip",
             "/suggestions/rejected", urlsplit(share_url).path.removeprefix(B)]
    seen: set[str] = set()
    for p in pages:
        r = http.get(B + p)
        assert r.status_code == 200, (p, r.status_code)
        assert '<meta name="docseed-base" content="/docseed">' in r.text
        for attr, url in _local_urls(r.text):
            if attr == "value" and not url.startswith(("/docseed/", "/s/", "/d/", "/f/")):
                continue   # 入力欄の値（パス以外）
            assert url == B or url.startswith(B + "/"), (p, attr, url)
            if attr in ("href", "src", "hx-get", "data-live-url") and "{" not in url:
                seen.add(url.split("#")[0])
    assert any("/static/icons.svg" in u for u in seen)
    assert len(seen) >= 30, sorted(seen)   # 画面のリンクを広くたどっている
    # 見つけたリンクをたどって、404 にならないことを確かめる（ダウンロードなどの重いものも含む）
    for url in sorted(seen):
        r = http.get(url, follow_redirects=False)
        # ルートが見つかること（空のフォルダの書庫のような 422 は、ここでは問わない）
        assert r.status_code != 404 and r.status_code < 500, (url, r.status_code)
        if r.status_code in (303, 307):
            assert r.headers["location"].startswith(B + "/"), (url, r.headers["location"])


def test_share_link_cookie_and_bookmarklet(bapp):
    http, csrf = _login(bapp)
    ids = _seed(http, csrf)
    settings_page = http.get(B + "/settings").text
    assert "https://example.com/docseed/clip?url=" in settings_page   # ブックマークレット
    assert "http://testserver/docseed/s/" in settings_page              # 自分の共有リンクの一覧
    token = ids["share"]["url"].rsplit("/", 1)[1]
    r = TestClient(bapp).get(f"{B}/s/{token}")
    assert r.status_code == 200 and r.headers.get("referrer-policy") == "no-referrer"


def test_oidc_redirect_uri_and_notify_link(bsettings):
    from docseed import notify, oidc
    from docseed.config import OidcProvider
    p = OidcProvider("google", "https://accounts.google.com", "id", "secret", "Google")
    assert oidc.redirect_uri(bsettings, p) == "https://example.com/docseed/auth/oidc/google/callback"
    assert notify._link(bsettings, "/f/1") == "\nhttps://example.com/docseed/f/1"


def test_without_base_path_nothing_changes(app, login):
    alice = login("alice")
    r = alice.get("/")
    assert '<meta name="docseed-base" content="">' in r.text
    assert 'href="/static/app.css' in r.text
    r = TestClient(app).get("/trash", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/trash"


# ---------------------------------------------------------------------------
# JS


def test_scripts_do_not_hardcode_app_paths():
    """JS のアプリのパスは BASE（<meta name="docseed-base">）から組み立てる。"""
    lit = re.compile(r"""["'`](/(?:api|x|d|f|s|auth|settings|search|admin|login|static|clip|trash)\b[^"'`]*)["'`]""")
    for name in ("app.js", "passkey.js"):
        src = (STATIC / name).read_text(encoding="utf-8")
        assert "const BASE" in src
        bad = [m.group(1) for m in lit.finditer(src)
               if not src[max(0, m.start() - 8):m.start()].rstrip().endswith("BASE +")]
        assert not bad, (name, bad)
