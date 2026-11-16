"""Markdown の文書のプレビュー（文書の画面・ゴミ箱の中の文書・共有ページ）。docs/design/ui.md S03・S10。"""

from fastapi.testclient import TestClient

from docseed.mdview import is_markdown, render

MD = """# 議事録

- [x] 予算を決める
- [ ] 日程を決める

| 項目 | 金額 |
|---|---|
| 会場 | 10,000 |

[サイト](https://example.com/) [危ない](javascript:alert(1)) [となり](other.md) [見出し](#議事録)

![図](https://tracker.example/x.png)

<script>alert(1)</script>
"""


def test_is_markdown():
    assert is_markdown("text/markdown", "a.md")
    assert is_markdown("text/x-markdown", "a.txt")
    assert is_markdown("text/plain", "README.markdown")
    assert not is_markdown("text/plain", "memo.txt")
    assert not is_markdown("application/pdf", "a.md")
    assert not is_markdown(None)


def test_render_is_restricted():
    html = str(render(MD))
    assert "<h1>議事録</h1>" in html                              # 見出しに id を付けない
    assert '<div class="doc-table"><table>' in html
    assert 'type="checkbox"' in html and 'disabled="disabled"' in html
    # 外へのリンクは新しいタブで。javascript: と相対リンク・ページ内リンクはリンクにしない
    assert '<a href="https://example.com/" target="_blank" rel="noopener noreferrer nofollow">サイト</a>' in html
    # javascript: のリンクは markdown-it が文字のままにする
    assert 'href="javascript:' not in html and "[危ない](javascript:alert(1))" in html
    assert 'href="other.md"' not in html and 'href="#' not in html
    assert html.count('class="doc-nolink"') == 2
    # 画像は読み込まず、代わりの文字を出す。生の HTML は文字のまま
    assert "<img" not in html and "tracker.example" not in html
    assert '<span class="md-img">[画像：図]</span>' in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_document_page_renders_markdown(login):
    alice = login("alice")
    doc = alice.upload(alice.root, {"議事録.md": MD.encode()}).json()["results"][0]["document"]
    page = alice.get(f"/d/{doc['id']}").text
    preview = page.split('aria-label="プレビュー"')[1].split("</section>")[0]
    assert '<article class="doc md-view" data-md-pane="rendered"><h1>議事録</h1>' in preview
    assert 'data-md-view="source"' in preview
    assert '<pre class="textview" data-md-pane="source" hidden># 議事録' in preview   # 元の文字も切り替えて見られる
    assert "<script>" not in preview
    # ゴミ箱の中の文書でも同じように見られる
    alice.delete(f"/api/v1/documents/{doc['id']}")
    page = alice.get(f"/trash/document/{doc['id']}").text
    assert '<article class="doc md-view" data-md-pane="rendered"><h1>議事録</h1>' in page


def test_plain_text_is_not_rendered(login):
    alice = login("alice")
    doc = alice.upload(alice.root, {"memo.txt": b"# not a heading"}).json()["results"][0]["document"]
    page = alice.get(f"/d/{doc['id']}").text
    assert '<pre class="textview"># not a heading</pre>' in page
    assert "md-view" not in page


RENDERED = '<article class="doc md-view" data-md-pane="rendered"><h1>議事録</h1>'


def test_share_document_link(app, login):
    """文書の共有リンク: 共有ページに整えて出す。"""
    alice = login("alice")
    doc = alice.upload(alice.root, {"議事録.md": MD.encode()}).json()["results"][0]["document"]
    link = alice.post("/api/v1/shares", json={"target_type": "document", "target_id": doc["id"]}).json()
    tok = link["url"].rsplit("/s/", 1)[1]
    anon = TestClient(app, base_url="http://testserver")
    page = anon.get(f"/s/{tok}").text
    assert RENDERED in page and 'data-md-view="source"' in page
    assert "<script>" not in page.split('aria-label="プレビュー"')[1]
    assert f'href="/s/{tok}/download"' in page
    # /md/ は文書の共有では共有ページそのものへ
    r = anon.get(f"/s/{tok}/md/{doc['id']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/s/{tok}"


def test_share_folder_link(app, login):
    """フォルダの共有リンク: 一覧の Markdown は整えた表示の画面へ。共有の外の文書は見られない。"""
    alice = login("alice")
    shared = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "公開"}).json()
    doc, memo = (r["document"] for r in alice.upload(
        shared["id"], {"議事録.md": MD.encode(), "memo.txt": b"x"}).json()["results"])
    secret = alice.upload(alice.root, {"秘密.md": b"# secret"}).json()["results"][0]["document"]
    link = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": shared["id"]}).json()
    tok = link["url"].rsplit("/s/", 1)[1]
    anon = TestClient(app, base_url="http://testserver")
    listing = anon.get(f"/s/{tok}").text
    assert f'href="/s/{tok}/md/{doc['id']}">議事録.md</a>' in listing
    assert f"/s/{tok}/preview/" in listing                         # md でないテキストは今までどおり
    page = anon.get(f"/s/{tok}/md/{doc['id']}").text
    assert RENDERED in page
    assert f'href="/s/{tok}/download/{doc['id']}"' in page and f'href="/s/{tok}?folder={shared['id']}"' in page
    assert "secret" not in anon.get(f"/s/{tok}/md/{secret['id']}").text
    assert anon.get(f"/s/{tok}/md/{secret['id']}").status_code == 404
    assert anon.get(f"/s/{tok}/md/{memo['id']}").status_code == 404       # Markdown でないものは出さない
