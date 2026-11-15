"""描画サービス（docs/design/extensions.md 6.10）。別コンテナで `python -m docseed.webclip.browser` として動く。

fetcher から `POST /render {"url": ..., "max_bytes": ...}` を受け、ヘッドレス Chromium でページを開き、
JavaScript を動かしたあとの HTML を返す。

- ページが出す通信（画像・スクリプト・リダイレクトの先を含む）は、すべて取得と同じ確認を通す:
  http / https だけ、許可したポートだけ、名前を引いて返ったすべての IP がグローバルであること。
  確認に通らない通信は止める（LAN・Pi 自身へは出ない）。
- 1 件ずつ処理する。Cookie・保存領域・Service Worker は使い捨てにする。ダウンロードはしない。
- このサービスには保存領域（docseed のデータ）を渡さない。外へつなぐのは描画のためだけ。

注意: Chromium の WebSocket は経路の割り込みの対象にならないので、LAN への出口は別途
（Pi のルーターや Docker のネットワークで）止めること。docs/admin/webclip.md を参照。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from .safefetch import ALLOWED_PORTS, FetchError, check_url, ip_is_public, system_resolver

log = logging.getLogger("docseed.renderer")

PAGE_TIMEOUT_MS = 20_000       # 最初の読み込み
IDLE_TIMEOUT_MS = 5_000        # 通信が落ち着くまでの待ち（来なくても描画した時点で保存する）
MAX_REQUESTS = 300             # 1 ページが出す通信の上限
MAX_BODY = 64 * 1024           # 依頼の大きさの上限
USER_AGENT = "docseed/renderer (+self-hosted; webclip)"


class Guard:
    """1 回の描画で使う、通信の確認。名前の引き直しを減らすため、ホストごとに結果を覚える。"""

    def __init__(self, *, resolver: Callable[[str, int], list[str]] = system_resolver,
                 ip_allowed: Callable = ip_is_public, ports=ALLOWED_PORTS):
        self.resolver = resolver
        self.ip_allowed = ip_allowed
        self.ports = frozenset(ports)
        self.count = 0
        self._hosts: dict[tuple[str, int], bool] = {}

    def allowed(self, url: str) -> bool:
        self.count += 1
        if self.count > MAX_REQUESTS:
            return False
        if url.startswith(("data:", "blob:")):      # ネットワークを使わない
            return True
        try:
            check_url(url)
        except FetchError:
            return False
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        if port not in self.ports or u.hostname is None:
            return False
        key = (u.hostname, port)
        if key not in self._hosts:
            self._hosts[key] = self._check_host(u.hostname, port)
        return self._hosts[key]

    def _check_host(self, host: str, port: int) -> bool:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return bool(self.ip_allowed(literal))
        try:
            ips = self.resolver(host, port)
        except (OSError, UnicodeError):
            return False
        if not ips:
            return False
        try:
            return all(self.ip_allowed(ipaddress.ip_address(s.split("%", 1)[0])) for s in ips)
        except ValueError:
            return False


class Renderer:
    def __init__(self, *, executable_path: str | None = None, no_sandbox: bool = False,
                 proxy: str | None = None, guard_factory: Callable[[], Guard] = Guard):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch: dict = {"args": ["--no-sandbox"] if no_sandbox else []}
        if executable_path:
            launch["executable_path"] = executable_path
        if proxy:
            launch["proxy"] = {"server": proxy}
        self.browser = self._pw.chromium.launch(**launch)
        self.guard_factory = guard_factory

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()

    def render(self, url: str, *, max_bytes: int) -> dict:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        guard = self.guard_factory()
        ctx = self.browser.new_context(service_workers="block", accept_downloads=False, locale="ja-JP",
                                       user_agent=USER_AGENT, ignore_https_errors=False)
        try:
            page = ctx.new_page()

            def route(r):
                if guard.allowed(r.request.url):
                    r.continue_()
                else:
                    r.abort("blockedbyclient")

            page.route("**/*", route)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except PlaywrightTimeout:
                raise FetchError("fetch_timeout", "描画が時間内に終わりませんでした", retry=True) from None
            except PlaywrightError as e:
                if "BLOCKED" in str(e):
                    raise FetchError("url_blocked", "取得できない通信先への移動を止めました") from None
                raise FetchError("fetch_connect_error", "描画のための接続に失敗しました", retry=True) from None
            status = resp.status if resp is not None else 0
            if status >= 400:
                raise FetchError("fetch_http_error", f"ページを描画できませんでした（HTTP {status}）",
                                 http_status=status)
            try:
                page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass
            final = page.url
            try:
                check_url(final)
            except FetchError:
                final = url
            html = page.content()
        finally:
            ctx.close()
        if len(html.encode("utf-8")) > max_bytes:
            raise FetchError("fetch_too_large", "描画後のページが大きすぎます")
        return {"html": html, "final_url": final, "status": status}


def make_handler(renderer: Renderer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "docseed-renderer"

        def _send(self, code: int, body: dict | str) -> None:
            data = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8" if isinstance(body, str)
                             else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._send(200, "ok")
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/render":
                self._send(404, {"message": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                self._send(400, {"message": "依頼が正しくありません"})
                return
            try:
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                url = req["url"]
                max_bytes = int(req["max_bytes"])
                check_url(url)
            except (ValueError, KeyError, TypeError, FetchError):
                self._send(400, {"message": "依頼が正しくありません"})
                return
            try:
                self._send(200, renderer.render(url, max_bytes=max_bytes))
            except FetchError as e:
                self._send(422, {"error": e.code, "message": str(e)})
            except Exception:  # noqa: BLE001 - 描画の失敗は取得側で保存を続けるので、詳細は記録だけする
                log.exception("render failed")
                self._send(500, {"error": "render_failed", "message": "描画に失敗しました"})

        def log_message(self, fmt, *args):  # 通信の記録は出さない（URL を残さないため）
            return

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    logging.basicConfig(level=os.environ.get("DOCSEED_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    renderer = Renderer(
        executable_path=os.environ.get("DOCSEED_RENDER_CHROMIUM") or None,
        no_sandbox=os.environ.get("DOCSEED_RENDER_NO_SANDBOX", "") == "1",
        proxy=os.environ.get("DOCSEED_WEBCLIP_PROXY", "").strip() or None,
    )
    # 1 件ずつ処理する（HTTPServer は 1 つの要求が終わるまで次を受けない）
    server = HTTPServer((host, port), make_handler(renderer))
    log.info("renderer listening on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        renderer.close()


if __name__ == "__main__":
    try:
        serve(port=int(os.environ.get("DOCSEED_RENDER_PORT", "8080")))
    except KeyboardInterrupt:
        sys.exit(0)
