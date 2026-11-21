"""画面の言語（日本語 / 英語）。docs/design/ui.md 1.2。

- 文言は日本語のまま書き、その日本語をキーにして `locales/<言語>.json` から訳を引く（訳が無ければ日本語のまま）
- テンプレートでは `{{ _("マイファイル") }}`、差し込みは `{{ _("{n} 件", n=count) }}`（str.format の名前つき）
- 言語は「設定の言語（ログイン中）」→ Cookie（docseed_lang）→ ブラウザの Accept-Language → 日本語 の順に決める
- 今の言語は contextvar に置く（リクエストごと。ASGI の middleware で決め、base_context が設定で上書きする）
"""

from __future__ import annotations

import contextvars
import json
from functools import cache
from pathlib import Path

from markupsafe import Markup, escape

LANGS = ("ja", "en")
DEFAULT = "ja"
COOKIE = "docseed_lang"
# 設定画面の選択肢の名前は、その言語自身で書く（読めない言語に切り替えても戻せるように）
LANG_NAMES = {"ja": "日本語", "en": "English"}

_LOCALES = Path(__file__).parent / "locales"
_current: contextvars.ContextVar[str] = contextvars.ContextVar("docseed_lang", default=DEFAULT)


@cache
def catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT or lang not in LANGS:
        return {}
    return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def get_lang() -> str:
    return _current.get()


def set_lang(lang: str) -> None:
    _current.set(lang if lang in LANGS else DEFAULT)


def translate(lang: str, message, **kw):
    """訳して差し込む。差し込む値に Markup（リンクなど）があれば、文を escape した Markup を返す。"""
    if not isinstance(message, str) or not message:
        return message
    text = catalog(lang).get(message, message)
    if not kw:
        return text
    if any(isinstance(v, Markup) for v in kw.values()):
        try:
            return Markup(escape(text)).format(**kw)
        except (KeyError, IndexError, ValueError):
            return Markup(escape(message)).format(**kw)
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return message.format(**kw)


def gettext(message, **kw):
    """今の言語の訳。テンプレートの `_` と、Python で画面に出す文に使う。"""
    return translate(get_lang(), message, **kw)


_ = gettext


def N_(message: str) -> str:
    """訳す文言の目印（そのまま返す）。画面に出すところで _() を通す（tests/test_i18n.py が訳の有無を確かめる）。"""
    return message


class Labels(dict):
    """値を引くときに今の言語へ訳す辞書（テンプレートの kind_label など）。"""

    def __getitem__(self, key):
        return gettext(super().__getitem__(key))

    def get(self, key, default=None):
        return gettext(super().get(key, default))


def from_accept_language(header: str | None) -> str | None:
    """Accept-Language から、対応する言語のうち最も優先度の高いもの。"""
    if not header:
        return None
    best: tuple[float, int, str] | None = None
    for i, part in enumerate(header.split(",")):
        tag, _sep, params = part.strip().partition(";")
        code = tag.strip().lower().split("-")[0]
        if code not in LANGS:
            continue
        q = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                q = float(params[2:])
            except ValueError:
                q = 0.0
        if q <= 0:
            continue
        key = (q, -i, code)
        if best is None or key > best:
            best = key
    return best[2] if best else None


def detect(cookie: str | None, accept_language: str | None) -> str:
    if cookie in LANGS:
        return cookie
    return from_accept_language(accept_language) or DEFAULT


def js_catalog(lang: str) -> dict[str, str]:
    """static/*.js の T("…") に使う訳だけを渡す（画面に埋め込む量を減らす）。"""
    cat = catalog(lang)
    return {k: cat[k] for k in js_keys() if k in cat}


@cache
def js_keys() -> tuple[str, ...]:
    import re
    static = Path(__file__).parent / "web" / "static"
    keys: list[str] = []
    for name in ("app.js", "passkey.js"):
        keys += re.findall(r'\bT\("((?:[^"\\]|\\.)*)"', (static / name).read_text(encoding="utf-8"))
    return tuple(dict.fromkeys(keys))


class LanguageMiddleware:
    """リクエストごとに言語を決めて contextvar に置く（ASGI。BaseHTTPMiddleware より軽い）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            cookie = accept = None
            for k, v in scope.get("headers", []):
                if k == b"cookie":
                    for item in v.decode("latin-1").split(";"):
                        name, _sep, value = item.strip().partition("=")
                        if name == COOKIE:
                            cookie = value
                elif k == b"accept-language":
                    accept = v.decode("latin-1")
            set_lang(detect(cookie, accept))
        await self.app(scope, receive, send)
