"""docs/ → GitHub の Wiki の変換（scripts/wiki_sync.py、docs/dev/docs-guide.md「GitHub の Wiki」）。"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from docseed import helpdocs

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("wiki_sync", ROOT / "scripts" / "wiki_sync.py")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)

REPO = "Owner/docseed"
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


@pytest.fixture(scope="module")
def wiki(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wiki")
    (out / ".git").mkdir()                      # clone した Wiki のつもり。.git は残す
    (out / "古いページ.md").write_text("消える", encoding="utf-8")
    ws.build(ROOT / "docs", out, REPO)
    return out


def headings(text: str) -> set[str]:
    out, in_code = set(), False
    for line in text.splitlines():
        if ws.FENCE.match(line):
            in_code = not in_code
        elif not in_code and (m := re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)):
            out.add(helpdocs.slug(m.group(1)))
    return out


@pytest.mark.parametrize("rel, name", [("README.md", "Home"), ("user/README.md", "user"),
                                       ("user/files.md", "user-files"), ("design.md", "design"),
                                       ("design/api.md", "design-api")])
def test_page_names(rel, name):
    assert ws.page_name(rel) == name


def test_pages_written_and_old_removed(wiki):
    names = {p.name for p in wiki.iterdir()}
    assert {"Home.md", "_Sidebar.md", "user.md", "user-files.md", "admin-configuration.md"} <= names
    assert ".git" in names and "古いページ.md" not in names
    assert not any("templates" in n for n in names)            # ひな形は出さない（画面のヘルプと同じ）
    assert ws.EXCLUDE == helpdocs.EXCLUDE


def test_same_pages_as_in_app_help(wiki):
    """Wiki と画面のヘルプは同じ文書の集まり。"""
    assert set(ws.collect(ROOT / "docs")) == set(helpdocs.index(helpdocs.docs_dir()).values())


def test_every_link_resolves_in_wiki(wiki):
    pages = {p.stem: p.read_text(encoding="utf-8") for p in wiki.glob("*.md")}
    ids = {name: headings(text) for name, text in pages.items()}
    broken = []
    for name, text in pages.items():
        body = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in LINK.findall(re.sub(r"`[^`]*`", "", body)):
            if target.startswith("https://") or target.startswith("http://"):
                continue
            page, _, frag = target.partition("#")
            page = page or name
            if page not in pages:
                broken.append(f"{name} → {target}（ページがありません）")
            elif frag and frag not in ids[page]:
                broken.append(f"{name} → {target}（見出しがありません）")
    assert not broken, "\n".join(broken)


def test_links_outside_docs_go_to_repository():
    pages = {"user/README.md": "user"}
    out = ws.convert("[r](../../README.md) [s](../../src/docseed/cli.py) [u](README.md#x) `[c](x.md)`\n",
                     "user/README.md", pages, "main/docs", f"https://github.com/{REPO}")
    assert f"[r](https://github.com/{REPO}/blob/main/README.md)" in out
    assert f"[s](https://github.com/{REPO}/blob/main/src/docseed/cli.py)" in out
    assert "[u](user#x)" in out and "`[c](x.md)`" in out       # コードの中は書き換えない
    out = ws.convert("[`DOCSEED_X`](README.md) と `a`\n", "user/README.md", pages, "main/docs", "https://x")
    assert out == "[`DOCSEED_X`](user) と `a`\n"                # リンクの文字の中のコード


def test_code_blocks_untouched():
    text = "```bash\n[a](b.md)\n```\n"
    assert ws.convert(text, "x.md", {}, "main/docs", "https://github.com/x/y") == text


def test_footer_and_sidebar(wiki):
    page = (wiki / "user-files.md").read_text(encoding="utf-8")
    assert f"https://github.com/{REPO}/blob/main/docs/user/files.md" in page and "自動で作っています" in page
    side = (wiki / "_Sidebar.md").read_text(encoding="utf-8")
    # ガイドの目次の順に並ぶ（はじめに → ファイルとフォルダ）
    assert side.index("(user-basics)") < side.index("(user-files)") < side.index("(user-faq)")
    assert "[管理者ガイド](admin)" in side


def test_workflow_runs_script():
    wf = (ROOT / ".github" / "workflows" / "wiki.yml").read_text(encoding="utf-8")
    assert "scripts/wiki_sync.py" in wf and "${{ github.repository }}.wiki" in wf
    assert "contents: write" in wf and "branches: [main]" in wf
