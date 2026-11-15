"""Markdown の文書を画面で読める形にする（文書の画面のプレビュー）。docs/design/ui.md S03。

利用者がアップロードした Markdown を HTML にするので、ヘルプ（helpdocs.py）より強く絞る。

- 生の HTML は書けない（html=False。<…> はそのまま文字として出す）。javascript: などのリンクは markdown-it が捨てる
- リンクは http・https・mailto だけを残し、新しいタブで開く（rel="noopener noreferrer nofollow"）。
  相対リンク・ページ内リンクは、指す先がサーバーに無いのでリンクにしない
- 画像は読み込まない（外のサーバーへ見たことが伝わる・CSP で止まる・相対パスは指す先が無い）。代わりの文字を出す
- 見出しに id を付けない（画面のほかの部分の id と重ならないように）
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from mdit_py_plugins.tasklists import tasklists_plugin

MARKDOWN_MIMES = {"text/markdown", "text/x-markdown"}
MARKDOWN_EXTS = ("md", "markdown", "mdown", "mkd")
SAFE_LINK = re.compile(r"^(https?:|mailto:)", re.I)


def is_markdown(mime: str | None, filename: str = "") -> bool:
    """Markdown として表示する文書か（種類が text/markdown か、テキストで拡張子が .md など）。"""
    m = (mime or "").lower()
    if m in MARKDOWN_MIMES:
        return True
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return m.startswith("text/plain") and ext in MARKDOWN_EXTS


def _md() -> MarkdownIt:
    md = (MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
          .enable(["table", "strikethrough"])
          .use(tasklists_plugin))

    def link_open(renderer, tokens, idx, options, env):
        t = tokens[idx]
        target = t.attrGet("href") or ""
        if SAFE_LINK.match(target):
            t.attrSet("target", "_blank")
            t.attrSet("rel", "noopener noreferrer nofollow")
        else:
            t.attrs.pop("href", None)
            t.attrSet("class", "doc-nolink")
        return renderer.renderToken(tokens, idx, options, env)

    def image(renderer, tokens, idx, options, env):
        alt = renderer.renderInlineAsText(tokens[idx].children or [], options, env)
        return f'<span class="md-img">[画像{"：" + str(escape(alt)) if alt else ""}]</span>'

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("image", image)
    # 表は横に送れる箱に入れる（スマホで画面全体を横にはみ出させない）
    md.add_render_rule("table_open", lambda *a: '<div class="doc-table"><table>\n')
    md.add_render_rule("table_close", lambda *a: "</table></div>\n")
    return md


_MD = _md()


def render(text: str) -> Markup:
    """Markdown の文字列 → 画面に埋め込める HTML。"""
    return Markup(_MD.render(text))
