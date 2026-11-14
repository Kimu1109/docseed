"""ヘルプ: docs/ の文書を画面で読む（docs/design/ui.md S12、docseed/helpdocs.py）。"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from docseed import helpdocs
from docseed.app import create_app
from docseed.config import Settings
from docseed.db import connect
from docseed.users import create_user

from .conftest import PASSWORD

ROOT = helpdocs.docs_dir()
KEYS = sorted(helpdocs.index(ROOT))


def test_needs_login(app, users):
    r = TestClient(app).get("/help/user", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_user_guide_page(login):
    page = login("alice").get("/help/user/files").text
    assert "<title>ファイルとフォルダ | docseed</title>" in page
    assert '<h2 id="過去の版">過去の版</h2>' in page              # 見出しの id は GitHub と同じ形
    assert 'href="/help/user/basics#' in page                    # 相対リンクは画面の URL に
    assert ".md" not in re.sub(r"<code>.*?</code>|docs/[a-z/-]+\.md", "", page.split('<article class="doc">')[1])
    assert '<div class="doc-table"><table>' in page              # 表は横に送れる箱に入る
    assert 'aria-current="page">利用者ガイド' in page


def test_sidebar_and_keyboard(login):
    page = login("alice").get("/favorites").text
    assert 'href="/help/user" data-goto="help"' in page and "<kbd>g</kbd> <kbd>h</kbd>" in page


def _tabs(page: str) -> str:
    return page.split('<nav class="help-guides"')[1].split("</nav>")[0]


def test_guides_tabs_by_role(login):
    alice = _tabs(login("alice").get("/help/user").text)
    assert "利用者ガイド</a>" in alice and "管理者ガイド</a>" not in alice
    # 管理者向けの文書も、利用者ガイドのリンクから開ける（隠すものは書いていない）
    assert login("alice").get("/help/admin/https").status_code == 200
    admin = _tabs(login("admin").get("/help/admin").text)
    assert "管理者ガイド</a>" in admin and "文書の地図</a>" in admin and 'aria-current="page">管理者ガイド' in admin


@pytest.mark.parametrize("path", ["/help/nope", "/help/../pyproject", "/help/dev/templates/user-feature",
                                  "/help/user/files.md", "/help/%2e%2e/README"])
def test_unknown_pages_are_404(login, path):
    assert login("alice").get(path).status_code == 404


def test_raw_html_is_not_rendered():
    """生の HTML は文字として出す（文書に <script> などを書かれても動かない）。"""
    md = helpdocs._md({"x.md": "x.md"})
    out = md.render('<script>alert(1)</script>\n\n[a](javascript:alert(1)) [b](https://example.com)',
                    {"rel": "x.md"})
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert 'href="javascript:' not in out
    assert 'href="https://example.com" target="_blank" rel="noopener noreferrer"' in out


def test_links_outside_docs_are_not_links():
    md = helpdocs._md({"user": "user/README.md"})
    out = md.render("[r](../../README.md) [u](README.md#x)", {"rel": "user/README.md"})
    assert '<a class="doc-nolink">r</a>' in out and '<a href="/help/user#x">u</a>' in out


def test_every_page_renders_and_links_resolve(login):
    """すべての文書が画面に出て、中のリンク（/help/…#…）がすべて画面の中の見出しまで届く。"""
    alice = login("alice")
    ids: dict[str, set[str]] = {}
    links: dict[str, list[str]] = {}
    for key in KEYS:
        url = helpdocs.url_of(key)
        r = alice.get(url)
        assert r.status_code == 200, url
        body = r.text.split('<article class="doc">')[1].split("</article>")[0]
        ids[url] = {unescape(x) for x in re.findall(r'<h[1-6] id="([^"]+)"', body)}
        links[url] = [unescape(x) for x in re.findall(r'<a href="(/help[^"]*)"', body)]
    broken = []
    for src, targets in links.items():
        for t in targets:
            path, _, frag = t.partition("#")
            if path not in ids:
                broken.append(f"{src} → {t}")
            elif frag and unquote(frag) not in ids[path]:
                broken.append(f"{src} → {t}（見出しがありません）")
    assert not broken, "\n".join(broken)


def test_with_base_path(tmp_path):
    s = Settings(data_dir=tmp_path / "data", secret_key="x" * 40, cookie_secure=False, base_path="/docseed",
                 share_upload_min_free_pct=0.0)
    app = create_app(s)
    c = connect(s.db_path)
    create_user(c, "alice", "Alice", PASSWORD)
    c.close()
    http = TestClient(app, base_url="http://testserver")
    assert http.post("/docseed/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    page = http.get("/docseed/help/user/faq").text
    assert 'href="/docseed/help/user/files#' in page and 'href="/help/' not in page
    assert 'href="/docseed/help/user" data-goto="help"' in page


def test_available_during_second_factor_setup(app, conn, users):
    """確認コードの設定を求められている間も、ヘルプ（設定のしかた）は読める。"""
    from docseed.authn import set_auth_settings
    set_auth_settings(conn, require_2fa=True)
    http = TestClient(app, base_url="http://testserver")
    assert http.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert http.get("/favorites", follow_redirects=False).status_code != 200
    assert http.get("/help/user/login").status_code == 200
