"""文書（docs/）を画面で読めるようにする（ヘルプ）。docs/design/ui.md S12。

- リポジトリの docs/ の Markdown を markdown-it-py で HTML にする。イメージ（wheel）では docseed/_docs に入る
  （pyproject.toml の force-include）。開発中（pip install -e）はリポジトリの docs/ をそのまま読む
- 生の HTML は書けない（html=False。<…> はそのまま文字として出す）。javascript: などのリンクも markdown-it が捨てる
- 相対リンク（../admin/https.md#…）は画面の URL（/help/admin/https#…）に書き換える。docs/ の外を指すリンクは
  リンクにしない（README.md・ソースはサーバーにないため）
- 見出しの id は GitHub と同じ形（tests/test_docs.py もこの slug でリンクを確かめる）。GitHub で読んでも画面で読んでも、
  見出しへのリンクが同じように効く
- docs/dev/templates/（ひな形）は出さない

URL: docs/README.md → /help、docs/user/README.md → /help/user、docs/user/files.md → /help/user/files
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from .i18n import N_
from .urls import href

EXCLUDE = ("dev/templates/",)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

# 画面の上に並べるガイド（キーの先頭 → 表示名）。管理者だけに出すものは admin_only
GUIDES = [
    ("user", N_("利用者ガイド"), False),
    ("admin", N_("管理者ガイド"), True),
    ("dev", N_("開発者ガイド"), True),
    ("design", N_("設計書"), True),
]


def slug(heading: str) -> str:
    """GitHub の見出しの id（github-slugger と同じ考え方: 小文字にし、記号を除き、空白を - にする）。"""
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)   # 見出しの中のリンクは文字だけ
    h = h.replace("`", "").strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def docs_dir() -> Path | None:
    """文書のディレクトリ。リポジトリの docs/（開発中）か、パッケージに入れた _docs（イメージ）。"""
    here = Path(__file__).resolve().parent
    for d in (here.parent.parent / "docs", here / "_docs"):
        if (d / "README.md").is_file():
            return d
    return None


def key_of(rel: str) -> str:
    """docs/ からの相対パス（user/files.md）→ 画面のキー（user/files）。README.md はそのディレクトリ。"""
    p = PurePosixPath(rel)
    if p.name == "README.md":
        return str(p.parent) if str(p.parent) != "." else ""
    return str(p.with_suffix(""))


def url_of(key: str) -> str:
    return href("/help" + (f"/{key}" if key else ""))


@cache
def index(root: Path) -> dict[str, str]:
    """画面のキー → docs/ からの相対パス。"""
    out = {}
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(EXCLUDE):
            continue
        out[key_of(rel)] = rel
    return out


def _md(pages: dict[str, str]) -> MarkdownIt:
    md = (MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
          .enable(["table", "strikethrough"])
          .use(anchors_plugin, min_level=1, max_level=6, slug_func=slug)
          .use(tasklists_plugin))
    by_rel = {rel: key for key, rel in pages.items()}

    def link_open(renderer, tokens, idx, options, env):
        t = tokens[idx]
        target = t.attrGet("href") or ""
        if SCHEME.match(target):
            t.attrSet("target", "_blank")
            t.attrSet("rel", "noopener noreferrer")
        elif target and not target.startswith("#"):
            path, _, frag = target.partition("#")
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(env["rel"]), path))
            if not rel.endswith(".md"):
                rel = posixpath.join(rel, "README.md")
            key = by_rel.get(rel)
            if key is None:            # docs/ の外（README.md・ソース）やひな形: リンクにしない
                t.attrs.pop("href", None)
                t.attrSet("class", "doc-nolink")
            else:
                t.attrSet("href", url_of(key) + (f"#{frag}" if frag else ""))
        return renderer.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    # 表は横に送れる箱に入れる（スマホで画面全体を横にはみ出させない）
    md.add_render_rule("table_open", lambda *a: '<div class="doc-table"><table>\n')
    md.add_render_rule("table_close", lambda *a: "</table></div>\n")
    return md


@dataclass(frozen=True)
class Page:
    key: str
    rel: str            # docs/ からの相対パス
    title: str
    html: str
    guide: str          # GUIDES のキー（user / admin / dev / design）。地図と調査は ""


_rendered: dict[tuple, Page] = {}


def get_page(key: str) -> Page | None:
    root = docs_dir()
    if root is None:
        return None
    pages = index(root)
    rel = pages.get(key.strip("/"))
    if rel is None:
        return None
    path = root / rel
    ck = (str(path), path.stat().st_mtime_ns, href(""))
    if ck not in _rendered:
        text = path.read_text(encoding="utf-8")
        md = _md(pages)
        tokens = md.parse(text, {"rel": rel})
        title = next((tokens[i + 1].content for i, t in enumerate(tokens)
                      if t.type == "heading_open" and t.tag == "h1"), rel)
        html = md.renderer.render(tokens, md.options, {"rel": rel})
        first = rel.split("/", 1)[0]
        guide = "design" if rel == "design.md" else (first if first in {g for g, _, _ in GUIDES} else "")
        _rendered[ck] = Page(key.strip("/"), rel, _plain(title), html, guide)
    return _rendered[ck]


def _plain(md_inline: str) -> str:
    """見出しの Markdown（`code`・**太字**・[リンク](…)）から文字だけを取り出す（<title> 用）。"""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md_inline)
    return re.sub(r"[`*_]", "", s)
