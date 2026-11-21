"""画面の言語（日本語 / 英語）。docs/design/ui.md 1.2。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docseed import i18n
from docseed.users import create_user

from .conftest import PASSWORD, Client

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "docseed"
TEMPLATES = SRC / "web" / "templates"
JP = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
# 訳を引く文言か（「（」「。」のような全角の記号だけのものも含める）
NEEDS_TR = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef]")
JS_CALL = re.compile(r'\bT\("((?:[^"\\]|\\.)*)"')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


# _("…") / N_("…") と、raise NotFound("…") のような例外の文言（続けて書いた文字列もつなげる）
STR = r'"(?:[^"\\\n]|\\.)*"'
CALL = re.compile(r"\b(?:_|N_)\(\s*((?:" + STR + r"\s*)+)")
CALL_SQ = re.compile(r"\b_\(\s*'((?:[^'\\]|\\.)*)'")
ERRORS = ("AppError", "Unauthenticated", "Forbidden", "ReauthRequired", "SecondFactorSetupRequired", "CsrfFailed",
          "NotFound", "NameConflict", "InvalidMove", "FileTooLarge", "ValidationFailed", "RateLimited",
          "InsufficientStorage")
RAISE = re.compile(r"raise\s+(?:\w+\.)?(?:" + "|".join(ERRORS) + r")\(\s*((?:" + STR + r"\s*)+)[,)]")


def _join(literals: str) -> str:
    return "".join(_unescape(x[1:-1]) for x in re.findall(STR, literals))


def _keys() -> dict[str, str]:
    """テンプレート・Python・JavaScript で訳を引いている文言（文言 → 最初に見つかったファイル）。"""
    keys: dict[str, str] = {}

    def add(k: str, where: str) -> None:
        if NEEDS_TR.search(k):
            keys.setdefault(k, where)

    for f in sorted(TEMPLATES.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in CALL.finditer(text):
            add(_join(m.group(1)), f.name)
        for m in CALL_SQ.finditer(text):
            add(_unescape(m.group(1)), f.name)
    for f in sorted(SRC.rglob("*.py")):
        # docstring・コメントの例（_("… {cmd} …") など）は数えない
        text = "\n".join(line for line in f.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith(("#", '"""')))
        for m in CALL.finditer(text):
            add(_join(m.group(1)), f.name)
        for m in RAISE.finditer(text):
            add(_join(m.group(1)), f.name)
    for f in (SRC / "web" / "static").glob("*.js"):
        for m in JS_CALL.finditer(f.read_text(encoding="utf-8")):
            add(_unescape(m.group(1)), f.name)
    from docseed import errors
    for name in ERRORS:
        add(getattr(errors, name).title, "errors.py")
    return keys


def _placeholders(s: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\w*)\}", s)))


def test_catalog_covers_every_message():
    cat = i18n.catalog("en")
    missing = [f"{f}: {k}" for k, f in _keys().items() if k not in cat]
    assert not missing, "locales/en.json に訳がありません:\n" + "\n".join(missing)


def test_catalog_placeholders_match():
    bad = [k for k, v in i18n.catalog("en").items() if _placeholders(k) != _placeholders(v)]
    assert not bad, "差し込みの名前が訳と合いません: " + ", ".join(bad)


def test_catalog_has_no_unused_entries():
    keys = _keys()
    unused = [k for k in i18n.catalog("en") if k not in keys]
    assert not unused, "使われていない訳があります（文言を変えたら古い訳を消す）:\n" + "\n".join(unused)


def test_catalog_is_sorted_json():
    text = (SRC / "locales" / "en.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert list(data) == sorted(data), "locales/en.json はキーの順に並べる"


def test_templates_have_no_bare_japanese():
    """テンプレートの日本語は _() の中だけに書く（コメントは除く）。"""
    found = []
    for f in sorted(TEMPLATES.glob("*.html")):
        text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        text = CALL_SQ.sub("_(…", CALL.sub("_(…", text))
        for i, line in enumerate(text.splitlines(), 1):
            if JP.search(line.replace("日本語", "")):
                found.append(f"{f.name}:{i}: {line.strip()[:120]}")
    assert not found, "訳を通していない日本語があります:\n" + "\n".join(found)


def test_js_has_no_bare_japanese():
    found = []
    for f in sorted((SRC / "web" / "static").glob("*.js")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = re.sub(r"(^|\s)//.*$", "", line)
            code = re.sub(r"/\*.*?\*/", "", code)
            code = JS_CALL.sub("T(…", code)
            if JP.search(code):
                found.append(f"{f.name}:{i}: {line.strip()[:120]}")
    assert not found, "訳を通していない日本語があります:\n" + "\n".join(found)


@pytest.mark.parametrize(("cookie", "header", "want"), [
    (None, None, "ja"),
    (None, "en-US,en;q=0.9", "en"),
    (None, "fr-FR,en;q=0.5,ja;q=0.8", "ja"),
    (None, "de", "ja"),
    ("en", "ja", "en"),
    ("xx", "en", "en"),
    (None, "en;q=0", "ja"),
])
def test_detect(cookie, header, want):
    assert i18n.detect(cookie, header) == want


def test_translate_formats_and_escapes():
    from markupsafe import Markup
    assert i18n.translate("en", "{n} 件", n=3) == "3 items"
    assert i18n.translate("ja", "{n} 件", n=3) == "3 件"
    assert i18n.translate("en", "訳の無い文") == "訳の無い文"
    out = i18n.translate("en", "残りの復旧コード: {n} 個", n=Markup("<b>3</b>"))
    assert isinstance(out, Markup) and out == "Recovery codes left: <b>3</b>"
    assert "&lt;x&gt;" in i18n.translate("en", "{name} さんのファイル", name="<x>", dummy=Markup(""))


# ---------------------------------------------------------------------------
# 英語の画面に日本語が残っていないか（主な画面を描いて確かめる）
# ---------------------------------------------------------------------------

def _visible_japanese(html: str) -> list[str]:
    html = re.sub(r'<script type="application/json" id="i18n">.*?</script>', "", html, flags=re.S)
    html = html.replace("日本語", "")      # 言語の選択肢の名前は、その言語で書く
    return sorted({m.group(0) for m in re.finditer(r"[^<>\"\n]*[぀-ヿ一-鿿][^<>\"\n]*", html)})


@pytest.fixture()
def en_admin(app, conn):
    create_user(conn, "root", "Root", PASSWORD, role="admin")
    create_user(conn, "carol", "Carol", PASSWORD)
    c = Client(app, "root")
    c.http.headers["Accept-Language"] = "en-US,en;q=0.9"
    return c


def test_english_pages_have_no_japanese(app, en_admin):
    c = en_admin
    sub = c.post("/api/v1/folders", json={"parent_id": c.root, "name": "Reports"}).json()["id"]
    up = c.upload(sub, {"notes.txt": b"hello world from docseed", "memo.md": b"# Title\n\nbody"}).json()
    doc_id = up["results"][0]["document"]["id"]
    gone = c.upload(c.root, {"old.txt": b"old file"}).json()["results"][0]["document"]["id"]
    c.delete(f"/api/v1/documents/{gone}")
    pages = ["/", f"/f/{c.root}", f"/f/{sub}", f"/d/{doc_id}", "/search", "/search?q=hello", "/search?q=zzzz",
             "/shared", "/favorites", "/tags", "/trash", f"/trash/d/{gone}", "/settings", "/settings/security",
             "/admin", "/suggestions/rejected", "/d/999999"]
    problems = {}
    for p in pages:
        r = c.get(p)
        assert r.status_code in (200, 404), (p, r.status_code)
        jp = _visible_japanese(r.text)
        if jp:
            problems[p] = jp
    anon = TestClient(app, base_url="http://testserver", headers={"Accept-Language": "en"})
    for p in ["/login", "/s/invalid-token"]:
        jp = _visible_japanese(anon.get(p).text)
        if jp:
            problems[p] = jp
    assert not problems, json.dumps(problems, ensure_ascii=False, indent=1)


def test_language_setting_overrides_browser(app, en_admin):
    c = en_admin
    assert '<html lang="en"' in c.get("/settings").text
    r = c.post("/settings/lang", data={"lang": "ja", "csrf_token": c.csrf}, follow_redirects=False)
    assert r.status_code == 303 and "docseed_lang=ja" in r.headers["set-cookie"]
    c.http.cookies.delete(i18n.COOKIE)        # 設定の言語は Cookie が無くても効く（別の端末）
    page = c.get("/settings").text
    assert '<html lang="ja"' in page and "画面の言語" in page
    c.post("/settings/lang", data={"lang": "auto", "csrf_token": c.csrf})
    assert '<html lang="en"' in c.get("/settings").text


def test_login_page_language_links(app):
    anon = TestClient(app, base_url="http://testserver")
    page = anon.get("/login").text
    assert '<html lang="ja"' in page and 'href="/lang/en?next=' in page
    r = anon.get("/lang/en", params={"next": "/login"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = anon.get("/login").text
    assert '<html lang="en"' in page and "Log in" in page
    # 外のサイトへは戻さない
    assert anon.get("/lang/ja", params={"next": "//evil.example"}, follow_redirects=False).headers["location"] == "/"


def test_js_catalog_in_page(en_admin):
    page = en_admin.get("/settings").text
    m = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', page, re.S)
    assert m
    data = json.loads(m.group(1))
    assert data.get("サーバーに接続できません") == "Cannot connect to the server"
    assert "マイファイル" not in data        # JavaScript で使う文言だけを渡す


def test_api_messages_follow_language(app, en_admin):
    """API のエラーの文言も、Accept-Language・Cookie の言語にする（訳が無ければ日本語のまま）。"""
    r = en_admin.get("/api/v1/documents/999999")
    assert r.status_code == 404 and r.json()["detail"] == "File not found" and r.json()["title"] == "Not found"
    en_admin.http.headers["Accept-Language"] = "ja"
    assert en_admin.get("/api/v1/documents/999999").json()["detail"] == "ファイルが見つかりません"
