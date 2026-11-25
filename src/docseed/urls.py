"""URL の起点（DOCSEED_BASE_PATH）。https://example.com/docseed/ のように、パスの下で動かすため。

- BasePathMiddleware が、届いた「/docseed/…」から接頭辞を外す。外側のプロキシ（nginx の
  proxy_pass http://pi/; など）が先に外して送ってきた場合も、そのまま受け付ける。
  アプリの中のルーティングやパスの判定（request.url.path）は、接頭辞なしの「/d/1」のままで動く。
  scope["root_path"] には入れない（Starlette は path が root_path で始まる前提で Mount を扱うため、
  外した path と組み合わせると /static が見つからなくなる）。そのため request.base_url にも接頭辞は付かない
- URL を作る側（テンプレートの {{ base }}、href()、icon()、public_root()）は、リクエストごとの接頭辞を BASE から読む
- アプリが返すリダイレクト（Location・HX-Redirect など）と Cookie の Path には、ここでまとめて接頭辞を付ける
"""

from __future__ import annotations

import re
from contextvars import ContextVar

BASE: ContextVar[str] = ContextVar("docseed_base_path", default="")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def base() -> str:
    """今のリクエストの接頭辞（「/docseed」。無ければ ""）。"""
    return BASE.get()


def href(path: str) -> str:
    """アプリの中のパス（「/d/1」）を、ブラウザに渡す URL（「/docseed/d/1」）にする。"""
    return BASE.get() + path


def public_root(request) -> str:
    """ブラウザから見たアプリの起点の URL（「https://example.com/docseed」。末尾の / なし）。共有リンクなどに使う。"""
    return str(request.base_url).rstrip("/") + BASE.get()


def has_base(path: str, b: str | None = None) -> bool:
    """接頭辞が付いたパスか（「/docseed」「/docseed/…」）。"""
    b = BASE.get() if b is None else b
    return bool(b) and (path == b or path.startswith(b + "/") or path.startswith(b + "?"))


def to_browser(path: str, b: str | None = None) -> str:
    """「/d/1」にも「/docseed/d/1」にも使える。まだ接頭辞が無ければ付ける。"""
    b = BASE.get() if b is None else b
    return path if not b or has_base(path, b) else b + path


def is_local_path(path: str | None) -> bool:
    """このサーバーの中を指すパスか（「//example.net」や「/\\example.net」のような別のサイトは不可）。

    タブ・改行などの制御文字も不可。ブラウザは URL の中のタブと改行を取り除くので、「/<タブ>/example.net」は
    「//example.net」（別のサイト）として開かれてしまう（docs/design/security.md 2.2）。
    """
    return (bool(path) and path.startswith("/") and not path.startswith("//") and "\\" not in path
            and not _CONTROL.search(path))


class _Base:
    """テンプレートの {{ base }}。描くときのリクエストの接頭辞を返す（include・macro の中でも同じ値）。"""

    def __str__(self) -> str:
        return BASE.get()

    __html__ = __str__   # 接頭辞は英数字と - _ . / だけ（config.normalize_base_path）なので、そのまま出してよい

    def __repr__(self) -> str:
        return repr(BASE.get())


BASE_GLOBAL = _Base()


class BasePathMiddleware:
    """一番外側に置く ASGI ミドルウェア。"""

    def __init__(self, app, base_path: str) -> None:
        self.app = app
        self.base = base_path

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.base:
            token = BASE.set("")
            try:
                return await self.app(scope, receive, send)
            finally:
                BASE.reset(token)
        b = self.base
        path: str = scope.get("path", "")
        if path == b and scope["type"] == "http":
            # 「/docseed」だけで開いたときは「/docseed/」へ（相対の解決をそろえる）
            qs = scope.get("query_string", b"")
            loc = (b + "/" + ("?" + qs.decode("latin-1") if qs else "")).encode("latin-1")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", loc), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        scope = dict(scope)
        if path.startswith(b + "/"):
            scope["path"] = path[len(b):]
            raw = scope.get("raw_path")
            if raw is not None and raw.startswith(b.encode() + b"/"):
                scope["raw_path"] = raw[len(b):]

        async def send_with_base(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [_rewrite_header(k, v, b) for k, v in message.get("headers", [])]
            await send(message)

        token = BASE.set(b)
        try:
            await self.app(scope, receive, send_with_base)
        finally:
            BASE.reset(token)


# アプリはパスを接頭辞なし（「/d/1」）で書く。ブラウザに渡す前に、ここでまとめて接頭辞を付ける
_PATH_HEADERS = {b"location", b"hx-redirect", b"hx-location", b"hx-push-url", b"hx-replace-url",
                 b"content-location", b"refresh"}
_COOKIE_PATH = re.compile(rb"(;\s*path=)([^;]*)", re.IGNORECASE)


def _rewrite_header(k: bytes, v: bytes, b: str) -> tuple[bytes, bytes]:
    name = k.lower()
    if name in _PATH_HEADERS:
        if name == b"refresh":   # 「0; url=/x」
            m = re.match(rb"(\s*\d+\s*;\s*url=)(.*)", v, re.IGNORECASE)
            if m and is_local_path(m.group(2).decode("latin-1")):
                v = m.group(1) + to_browser(m.group(2).decode("latin-1"), b).encode("latin-1")
        elif is_local_path(s := v.decode("latin-1")):
            v = to_browser(s, b).encode("latin-1")
    elif name == b"set-cookie":
        def fix(m: re.Match) -> bytes:
            path = m.group(2).decode("latin-1").strip()
            if not is_local_path(path):
                return m.group(0)
            new = b if path == "/" else to_browser(path, b)
            return m.group(1) + new.encode("latin-1")
        v = _COOKIE_PATH.sub(fix, v)
    return k, v
