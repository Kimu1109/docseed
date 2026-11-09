"""ページのメタデータと文字コード。docs/design/extensions.md 6.3（文字コード）・6.4。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from lxml import html as lxml_html

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)


def decode_html(body: bytes, header_charset: str | None) -> str:
    """HTTP ヘッダ → <meta charset> → 推定（charset-normalizer）の順で文字コードを決めて str にする。"""
    for enc in (header_charset, _meta_charset(body)):
        if enc:
            try:
                return body.decode(_norm(enc))
            except (LookupError, UnicodeDecodeError):
                continue
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes
    best = from_bytes(body).best()
    return str(best) if best is not None else body.decode("utf-8", "replace")


def _meta_charset(body: bytes) -> str | None:
    m = _META_CHARSET.search(body[:4096])
    return m.group(1).decode("ascii", "ignore") if m else None


def _norm(enc: str) -> str:
    e = enc.strip().lower()
    return {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
            "windows-31j": "cp932", "euc-jp": "euc_jp"}.get(e, e)


@dataclass
class PageMeta:
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    image_url: str | None = None
    author: str | None = None
    published_at: str | None = None
    canonical_url: str | None = None
    og_type: str | None = None
    twitter_card: str | None = None
    oembed_url: str | None = None
    raw: dict = field(default_factory=dict)       # og:* / twitter:* / article:* のすべて


def _clean(s: str | None, limit: int = 2000) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] or None


def parse(text: str, url: str) -> PageMeta:
    """<head> を読み、extensions.md 6.4 の順に値を決める。"""
    m = PageMeta()
    try:
        doc = lxml_html.document_fromstring(text.encode("utf-8"), parser=lxml_html.HTMLParser(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 壊れた HTML でも URL だけで保存できるようにする
        doc = None
    props: dict[str, str] = {}
    names: dict[str, str] = {}
    if doc is not None:
        for el in doc.iter("meta"):
            key = (el.get("property") or "").strip().lower()
            name = (el.get("name") or "").strip().lower()
            content = el.get("content")
            if content is None:
                continue
            for k in (key, name):
                if k.startswith(("og:", "twitter:", "article:")):
                    m.raw.setdefault(k, content.strip())
            if key:
                props.setdefault(key, content)
            if name:
                names.setdefault(name, content)

    def first(*vals):
        for v in vals:
            v = _clean(v)
            if v:
                return v
        return None

    title_el = doc.find(".//title") if doc is not None else None
    m.title = first(props.get("og:title"), names.get("twitter:title"), props.get("twitter:title"),
                    title_el.text_content() if title_el is not None else None)
    m.description = first(props.get("og:description"), names.get("twitter:description"),
                          props.get("twitter:description"), names.get("description"))
    m.site_name = first(props.get("og:site_name"))
    img = first(props.get("og:image:secure_url"), props.get("og:image"), props.get("og:image:url"),
                names.get("twitter:image"), names.get("twitter:image:src"), props.get("twitter:image"))
    m.image_url = urljoin(url, img) if img else None
    m.author = first(props.get("article:author"), names.get("author"))
    m.published_at = first(props.get("article:published_time"))
    m.og_type = first(props.get("og:type"))
    m.twitter_card = first(names.get("twitter:card"), props.get("twitter:card"))
    if doc is not None:
        for link in doc.iter("link"):
            rel = (link.get("rel") or "").lower().split()
            href = link.get("href")
            if not href:
                continue
            if "canonical" in rel and not m.canonical_url:
                m.canonical_url = urljoin(url, href.strip())
            if "alternate" in rel and (link.get("type") or "").lower() == "application/json+oembed" \
                    and not m.oembed_url:
                m.oembed_url = urljoin(url, href.strip())
        if not m.author or not m.published_at:
            _json_ld(doc, m)
    if not m.canonical_url and props.get("og:url"):
        m.canonical_url = urljoin(url, props["og:url"].strip())
    return m


def _json_ld(doc, m: PageMeta) -> None:
    """JSON-LD（schema.org の Article など）から著者と公開日を補う。"""
    for el in doc.iter("script"):
        if (el.get("type") or "").lower() != "application/ld+json":
            continue
        try:
            data = json.loads(el.text_content() or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not m.published_at and isinstance(it.get("datePublished"), str):
                m.published_at = it["datePublished"][:40]
            a = it.get("author")
            if not m.author and a:
                a = a[0] if isinstance(a, list) and a else a
                name = a.get("name") if isinstance(a, dict) else a if isinstance(a, str) else None
                m.author = _clean(name, 200)


def finish(m: PageMeta, final_url: str, extracted_title: str | None = None) -> PageMeta:
    """足りない値を URL などで補う（タイトル → trafilatura のタイトル → URL、サイト名 → ホスト名）。"""
    if not m.title:
        m.title = _clean(extracted_title) or final_url
    if not m.site_name:
        m.site_name = urlsplit(final_url).hostname
    if not m.canonical_url:
        m.canonical_url = final_url
    return m


def oembed_text(data: dict) -> str:
    """oEmbed の応答から、本文の補いに使う文字（title・author_name・html の中の文字）。"""
    parts = []
    for k in ("title", "author_name"):
        if isinstance(data.get(k), str):
            parts.append(data[k].strip())
    h = data.get("html")
    if isinstance(h, str) and h.strip():
        try:
            parts.append(lxml_html.fragment_fromstring(h, create_parent="div").text_content().strip())
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(p for p in parts if p)


def file_name(title: str) -> str:
    """文書のファイル名 <タイトル>.url（255 バイトまで）。/ などは置き換える。"""
    t = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title)
    t = re.sub(r"\s+", " ", t).strip(" .") or "web"
    while len((t + ".url").encode("utf-8")) > 255:
        t = t[:-1]
    return t + ".url"
