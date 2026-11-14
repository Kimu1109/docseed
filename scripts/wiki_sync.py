"""docs/ を GitHub の Wiki の形に変換する（一方向: リポジトリ → Wiki）。docs/dev/docs-guide.md「GitHub の Wiki」。

.github/workflows/wiki.yml が main への push のたびに実行し、出力を Wiki のリポジトリ（<repo>.wiki.git）に push する。
Wiki は読むための写しで、編集の元は常に docs/。Wiki で直した内容は、次の同期で消える。

変換:
- ページ名は docs/ のパスから作る（Wiki はディレクトリを URL に使わないので、平らな名前にする）
    docs/README.md → Home、docs/user/README.md → user、docs/user/files.md → user-files、docs/design.md → design
- 相対リンク（../admin/https.md#…）は Wiki のページ名（admin-https#…）に書き換える。見出しの id は GitHub と同じなので、
  # の後ろはそのまま使える
- docs/ の外（ルートの README.md・ソース）を指すリンクは、リポジトリの該当ファイルの URL にする
- docs/dev/templates/（ひな形）は出さない（画面のヘルプ helpdocs.EXCLUDE と同じ）
- _Sidebar.md（ガイドごとのページの一覧）と、各ページの末尾に元のファイルへのリンクを付ける

使い方:
    python scripts/wiki_sync.py --docs docs --out <Wiki の作業ディレクトリ> --repo Owner/docseed [--ref main]

標準ライブラリだけで動く（Actions で依存を入れずに実行するため）。
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

EXCLUDE = ("dev/templates/",)
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"(?<!!)(\[[^\]]*\])\(([^)\s]+)\)")
INLINE_CODE = re.compile(r"`[^`]*`")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")

# サイドバーに並べる順（docs/ の先頭のディレクトリ → 見出し）
GUIDES = [("user", "利用者ガイド"), ("admin", "管理者ガイド"), ("dev", "開発者ガイド"), ("design", "設計書"),
          ("research", "調査・計測")]


def page_name(rel: str) -> str:
    """docs/ からの相対パス → Wiki のページ名。"""
    p = PurePosixPath(rel)
    if rel == "README.md":
        return "Home"
    parts = list(p.parent.parts) if p.name == "README.md" else [*p.parent.parts, p.stem]
    return "-".join(parts)


def collect(docs: Path) -> dict[str, str]:
    """docs/ からの相対パス → ページ名。名前がぶつかったら止める。"""
    out: dict[str, str] = {}
    for f in sorted(docs.rglob("*.md")):
        rel = f.relative_to(docs).as_posix()
        if rel.startswith(EXCLUDE):
            continue
        out[rel] = page_name(rel)
    names = list(out.values())
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SystemExit(f"Wiki のページ名がぶつかります: {dup}（docs/ のファイル名を変えてください）")
    return out


def rewrite_target(target: str, rel: str, pages: dict[str, str], docs_prefix: str, repo_url: str) -> str:
    if not target or target.startswith("#") or SCHEME.match(target):
        return target
    path, _, frag = target.partition("#")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
    cand = joined if joined.endswith(".md") else posixpath.join(joined, "README.md")
    if cand in pages:
        return pages[cand] + (f"#{frag}" if frag else "")
    # docs/ の外やひな形: リポジトリのファイルへ
    repo_path = posixpath.normpath(posixpath.join(docs_prefix, joined))
    kind = "blob" if posixpath.splitext(joined)[1] else "tree"
    return f"{repo_url}/{kind}/{repo_path}" + (f"#{frag}" if frag else "")


def _rewrite_links(s: str, rel: str, pages: dict[str, str], docs_prefix: str, repo_url: str) -> str:
    return LINK.sub(lambda m: f"{m.group(1)}({rewrite_target(m.group(2), rel, pages, docs_prefix, repo_url)})", s)


def convert(text: str, rel: str, pages: dict[str, str], docs_prefix: str, repo_url: str) -> str:
    out, in_code = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        # インラインのコード（`…`）の中は書き換えない: いったん印に置き換えて、リンクを書き換えてから戻す
        # （[`X`](x.md) のように、リンクの文字にコードがあっても書き換えられるように）
        codes: list[str] = []
        s = INLINE_CODE.sub(lambda m, c=codes: (c.append(m.group(0)), f"\0{len(c) - 1}\0")[1], line)
        s = _rewrite_links(s, rel, pages, docs_prefix, repo_url)
        s = re.sub(r"\0(\d+)\0", lambda m, c=codes: c[int(m.group(1))], s)
        out.append(s)
    return "\n".join(out) + "\n"


def title_of(text: str, fallback: str) -> str:
    in_code = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        elif not in_code and (m := H1.match(line)):
            return re.sub(r"[`*]", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1)))
    return fallback


def link_order(text: str, rel: str) -> list[str]:
    """文書の中のリンク先（docs/ からの相対パス）を、出てくる順に。"""
    out = []
    for target in LINK.findall(INLINE_CODE.sub("", text)):
        path = target[1].partition("#")[0]
        if path and not SCHEME.match(path):
            r = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
            out.append(r if r.endswith(".md") else posixpath.join(r, "README.md"))
    return out


def sidebar(docs: Path, titles: dict[str, str], pages: dict[str, str]) -> str:
    """ガイドごとのページの一覧。並びはガイドの目次（README.md など）に出てくる順、目次に無いものは後ろ。"""
    lines = ["**[docseed の文書](Home)**", ""]
    for guide, label in GUIDES:
        index = f"{guide}/README.md" if f"{guide}/README.md" in pages else f"{guide}.md"
        members = [r for r in pages if r.startswith(guide + "/") and r != index]
        if index not in pages and not members:
            continue
        order = link_order((docs / index).read_text(encoding="utf-8"), index) if index in pages else []
        rank = {r: i for i, r in reversed(list(enumerate(order)))}
        head = f"[{label}]({pages[index]})" if index in pages else label
        lines += [f"**{head}**", ""]
        for r in sorted(members, key=lambda r: (rank.get(r, len(order)), r)):
            lines.append(f"- [{titles[r]}]({pages[r]})")
        lines.append("")
    return "\n".join(lines)


def build(docs: Path, out: Path, repo: str, ref: str = "main") -> dict[str, str]:
    """out の中身（.git 以外）を消して、Wiki のページを書き出す。ページ名の一覧を返す。"""
    docs = docs.resolve()
    pages = collect(docs)
    repo_url = f"https://github.com/{repo}"
    blob_base = f"{repo_url}/blob/{ref}"
    docs_prefix = f"{ref}/docs"        # リポジトリの外へのリンク: {repo_url}/blob/{ref}/<パス>
    out.mkdir(parents=True, exist_ok=True)
    for p in out.iterdir():
        if p.name == ".git":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    titles = {}
    for rel, name in pages.items():
        text = (docs / rel).read_text(encoding="utf-8")
        titles[rel] = title_of(text, name)
        body = convert(text, rel, pages, docs_prefix, repo_url)
        body += (f"\n---\n\n<sub>このページは [`docs/{rel}`]({blob_base}/docs/{rel}) から自動で作っています。"
                 "直すときはリポジトリの docs/ を直してください（Wiki で直しても、次の同期で消えます）。</sub>\n")
        (out / f"{name}.md").write_text(body, encoding="utf-8")
    (out / "_Sidebar.md").write_text(sidebar(docs, titles, pages), encoding="utf-8")
    return pages


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--docs", type=Path, default=Path("docs"))
    ap.add_argument("--out", type=Path, required=True, help="Wiki のリポジトリを clone したディレクトリ")
    ap.add_argument("--repo", required=True, help="Owner/Name（リポジトリの外を指すリンクに使う）")
    ap.add_argument("--ref", default="main", help="リポジトリのファイルへのリンクに使うブランチ")
    a = ap.parse_args(argv)
    if not (a.docs / "README.md").is_file():
        sys.exit(f"{a.docs} に README.md がありません")
    pages = build(a.docs, a.out, a.repo, a.ref)
    print(f"{len(pages)} ページを {a.out} に書き出しました")


if __name__ == "__main__":
    main()
