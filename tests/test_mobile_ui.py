"""スマホ用の画面（docs/design/ui.md 3.1）。HTML は PC と同じで、スマホだけの部品を CSS で出し分ける。"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "docseed" / "web" / "static"
CSS = (STATIC / "app.css").read_text()
JS = (STATIC / "app.js").read_text()


def test_tabbar_and_drawer(login):
    alice = login("alice")
    page = alice.get(f"/f/{alice.root}").text
    assert 'viewport-fit=cover' in page
    tabbar = page[page.index('<nav class="tabbar"'):]
    tabbar = tabbar[:tabbar.index("</nav>")]
    for href in (f"/f/{alice.root}", "/favorites", "/search", "/shared"):
        assert f'href="{href}"' in tabbar, href
    assert "data-nav-toggle" in tabbar and 'class="side-backdrop"' in page
    # 今いる画面のタブに印が付く
    assert re.search(r'href="/f/\d+" aria-current="page"', tabbar)
    assert 'aria-current="page"' in login("bob").get("/search").text.split('<nav class="tabbar"')[1].split('href="/search"')[1][:40]


def test_folder_mobile_parts_only_for_editors(login, users):
    alice, bob = login("alice"), login("bob")
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "家族"}).json()
    alice.upload(f["id"], {"a.txt": b"x"})
    page = alice.get(f"/f/{f['id']}").text
    # 右下の「＋」・撮影・行の下の日付とサイズ・並べ替え・下から出るメニューの見出し・選択
    for s in ('class="fab"', 'id="dlg-add"', 'id="camera-input" accept="image/*" capture="environment"',
              'class="m-meta"', 'class="m-sort"', 'class="sheet-title"', "data-select-row", "data-camera-pick"):
        assert s in page, s
    assert alice.put(f"/api/v1/folders/{f['id']}/members/{users['bob'].id}", json={"role": "viewer"}).status_code == 204
    viewer = bob.get(f"/f/{f['id']}").text
    assert 'class="m-meta"' in viewer
    for s in ('class="fab"', "camera-input", "data-select-row"):
        assert s not in viewer, s


def test_mobile_css_and_script_hooks():
    # PC ではスマホの部品を出さず、スマホでは PC だけの部品を隠す
    assert ".tabbar, .fab, .m-only, .m-meta" in CSS
    mobile = CSS[CSS.index("@media (max-width: 860px) {\n  :root { --side-w: 0px; }"):]
    for s in (".pc-only { display: none !important; }", ".tabbar {", ".rowmenu-panel {", ".fab {",
              ".search-form:not(.filters-open) .search-filters", ".plain { position: relative; display: block; overflow-x: auto;"):
        assert s in mobile, s
    for s in ('"[data-filters-toggle]"', '"[data-camera-pick]"', '"[data-select-row]"', '"#camera-input"', "pointerdown"):
        assert s in JS, s
