"""ダークモード（9）とキーボード操作（10）。"""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "docseed" / "web" / "static" / "app.css").read_text()
JS = (Path(__file__).resolve().parents[1] / "src" / "docseed" / "web" / "static" / "app.js").read_text()


def _block(selector: str) -> str:
    i = CSS.index(selector + " {")
    return CSS[i:CSS.index("}", i)]


def _tokens(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+):", block))


def test_dark_palette_covers_every_color_token():
    light = _tokens(_block(":root"))
    colors = {t for t in light if re.search(rf"{t}:\s*(#|rgba?\()", _block(":root"))}
    dark = _tokens(_block(':root[data-theme="dark"]'))
    auto = _tokens(_block(':root:not([data-theme="light"])'))
    assert colors and colors <= dark, colors - dark
    assert dark == auto                                     # 端末の設定と「ダーク」の固定は同じ色
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", CSS))
    assert used <= light, used - light                     # 使っている色はすべて :root にある


def test_no_hardcoded_colors_outside_tokens():
    """色はトークンで書く（ダークモードで変わらない色を残さない）。QR コードの白は読み取りのためそのまま。"""
    body = CSS[CSS.index("*, *::before"):]
    found = [ln for ln in body.splitlines() if re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", ln) and ".qr " not in ln]
    assert found == []


def test_theme_setting(login):
    alice = login("alice")
    page = alice.get("/settings").text
    assert "<html lang=\"ja\">" in page and 'content="light dark"' in page
    assert 'name="theme" value="auto" checked' in page
    r = alice.post("/settings/theme", data={"csrf_token": alice.csrf, "theme": "dark"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?done=theme")
    page = alice.get(f"/f/{alice.root}").text
    assert '<html lang="ja" data-theme="dark">' in page and '<meta name="color-scheme" content="dark">' in page
    assert alice.get("/api/v1/me/theme").json() == {"theme": "dark"}
    assert alice.put("/api/v1/me/theme", json={"theme": "light"}).json() == {"theme": "light"}
    assert 'data-theme="light"' in alice.get("/settings").text
    assert alice.put("/api/v1/me/theme", json={"theme": "pink"}).status_code == 422
    # ほかの人には効かない
    assert "data-theme" not in login("bob").get("/settings").text.split(">", 2)[1]


def test_login_page_follows_device(app):
    from fastapi.testclient import TestClient
    page = TestClient(app).get("/login").text
    assert '<html lang="ja">' in page and 'content="light dark"' in page


def test_keyboard_help_and_hooks(login):
    admin, alice = login("admin"), login("alice")
    page = alice.get(f"/f/{alice.root}").text
    assert 'id="dlg-keys"' in page and 'data-open-dialog="dlg-keys"' in page
    for name in ("files", "favorites", "shared", "tags", "trash"):
        assert f'data-goto="{name}"' in page
    assert 'data-goto="admin"' not in page and "<kbd>g</kbd> <kbd>a</kbd>" not in page
    admin_page = admin.get(f"/f/{admin.root}").text
    assert 'data-goto="admin"' in admin_page and "<kbd>g</kbd> <kbd>a</kbd>" in admin_page


def test_keyboard_script_targets_exist(login):
    """スクリプトが押すボタン・リンクが画面にある（名前を変えたときに気づけるように）。"""
    alice = login("alice")
    folder = alice.get(f"/f/{alice.root}").text
    for sel in ("data-upload-pick", 'data-open-dialog="dlg-folder"', "fav-btn"):
        assert sel in folder, sel
    did = alice.upload(alice.root, {"a.txt": b"x"}).json()["results"][0]["document"]["id"]
    assert 'class="doc-title"' in alice.get(f"/d/{did}").text
    for key in ('m: "[data-goto=files]"', '"#dlg-keys"', '".results .result-name"', '"[data-upload-pick]"'):
        assert key in JS, key
