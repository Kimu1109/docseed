"""文書の検査（docs/dev/docs-guide.md「テストが確かめること」）。

機能を足したときの文書の直し忘れを、機械で見つけられる範囲で見つける:
- Markdown の相対リンクと、見出しへのリンク（#…）が切れていない
- コード・compose・スクリプト・.env.example にある DOCSEED_* が、設定の一覧（docs/admin/configuration.md）にある
- cli.py のコマンドが、コマンドの一覧（docs/admin/cli.md）にある
- 利用者ガイド・管理者ガイドのページが、そのガイドの README.md から張られている
- 利用者ガイドに管理者向けの話（.env・docker compose・DOCSEED_*）が出てこない
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
# ひな形は書きかけのリンク（〇〇.md）を含むので検査しない。scripts/poc は計測の作業場所
SKIP_DIRS = (DOCS / "dev" / "templates", ROOT / "scripts" / "poc", ROOT / ".git", ROOT / ".venv")

FENCE = re.compile(r"^(```|~~~)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
INLINE_CODE = re.compile(r"`[^`]*`")
ENV = re.compile(r"\bDOCSEED_[A-Z0-9_]*[A-Z0-9]")


def markdown_files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.md")):
        if any(p.is_relative_to(d) for d in SKIP_DIRS):
            continue
        out.append(p)
    return out


def prose_lines(text: str) -> list[str]:
    """コードブロックの外の行。"""
    lines, in_code = [], False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            in_code = not in_code
            continue
        if not in_code:
            lines.append(line)
    return lines


def slug(heading: str) -> str:
    """GitHub の見出しの id（github-slugger と同じ考え方: 小文字、記号を除き、空白を -）。"""
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)   # 見出しの中のリンクは文字だけ
    h = h.replace("`", "").strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


@cache
def anchors(path: Path) -> frozenset[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in prose_lines(path.read_text(encoding="utf-8")):
        m = HEADING.match(line)
        if not m:
            continue
        s = slug(m.group(2))
        n = seen.get(s, 0)
        seen[s] = n + 1
        out.add(s if n == 0 else f"{s}-{n}")
    return frozenset(out)


def links(path: Path) -> list[str]:
    out = []
    for line in prose_lines(path.read_text(encoding="utf-8")):
        out += LINK.findall(INLINE_CODE.sub("", line))
    return out


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


@pytest.mark.parametrize("path", markdown_files(), ids=rel)
def test_links_resolve(path: Path):
    broken = []
    for target in links(path):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):     # https: mailto: など
            continue
        file_part, _, frag = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            broken.append(f"{target}（ファイルがありません）")
            continue
        if frag and dest.suffix == ".md" and frag not in anchors(dest):
            broken.append(f"{target}（見出し #{frag} がありません。ある見出し: {', '.join(sorted(anchors(dest)))}）")
    assert not broken, f"{rel(path)} のリンク切れ:\n  " + "\n  ".join(broken)


def env_sources() -> list[Path]:
    files = [*(ROOT / "src").rglob("*.py"), ROOT / "compose.yaml", ROOT / ".env.example",
             *(ROOT / "docker").rglob("*"), *(ROOT / "scripts" / "ops").rglob("*"),
             *(ROOT / "scripts").glob("*.py"), *(ROOT / "scripts").glob("*.sh")]
    return [p for p in files if p.is_file()]


def test_every_setting_is_documented():
    doc = (DOCS / "admin" / "configuration.md").read_text(encoding="utf-8")
    documented = set(ENV.findall(doc))
    missing: dict[str, set[str]] = {}
    for p in env_sources():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name in ENV.findall(text):
            if name not in documented:
                missing.setdefault(name, set()).add(rel(p))
    assert not missing, ("docs/admin/configuration.md に無い設定があります（行を足してください）:\n  "
                         + "\n  ".join(f"{k}（{', '.join(sorted(v))}）" for k, v in sorted(missing.items())))


def test_every_command_is_documented():
    cli = (ROOT / "src" / "docseed" / "cli.py").read_text(encoding="utf-8")
    commands = re.findall(r'sub\.add_parser\("([a-z0-9-]+)"', cli)
    assert commands, "cli.py からコマンドを読み取れませんでした（add_parser の書き方が変わった？）"
    doc = (DOCS / "admin" / "cli.md").read_text(encoding="utf-8")
    missing = [c for c in commands if f"`docseed {c}" not in doc]
    assert not missing, f"docs/admin/cli.md に無いコマンドがあります: {', '.join(missing)}"


@pytest.mark.parametrize("guide", ["user", "admin", "dev"])
def test_pages_are_listed_in_guide_index(guide: str):
    index = DOCS / guide / "README.md"
    linked = {(index.parent / t.partition("#")[0]).resolve() for t in links(index) if not t.startswith("#")}
    if guide == "dev":   # 開発者ガイドのページは README か docs-guide.md から張る
        linked |= {(DOCS / "dev" / t.partition("#")[0]).resolve() for t in links(DOCS / "dev" / "docs-guide.md")}
    pages = [p for p in (DOCS / guide).rglob("*.md") if p.name != "README.md"]
    missing = [rel(p) for p in pages if p.resolve() not in linked]
    assert not missing, f"{rel(index)} から張られていないページがあります（表に行を足してください）: {missing}"


def test_docs_map_links_every_guide():
    linked = {t.partition("#")[0] for t in links(DOCS / "README.md")}
    for guide in ("user/README.md", "admin/README.md", "dev/README.md", "design.md"):
        assert guide in linked, f"docs/README.md に {guide} へのリンクがありません"


@pytest.mark.parametrize("path", sorted((DOCS / "user").glob("*.md")), ids=rel)
def test_user_guide_has_no_admin_terms(path: Path):
    bad = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for word in (".env", "docker compose", "DOCSEED_"):
            if word in line:
                bad.append(f"{i} 行目: {word}")
    assert not bad, (f"{rel(path)} に管理者向けの言葉があります（管理者ガイドに書いて、リンクにしてください）:\n  "
                     + "\n  ".join(bad))


def test_slug_matches_github():
    assert slug("パスの下で動かす（`https://example.com/docseed/`）") == "パスの下で動かすhttpsexamplecomdocseed"
    assert slug("HTTPS と入口") == "https-と入口"
    assert slug("音声・動画") == "音声動画"
    assert slug("手元の証明書を使う（certbot の Let's Encrypt の証明書など）") == \
        "手元の証明書を使うcertbot-の-lets-encrypt-の証明書など"
