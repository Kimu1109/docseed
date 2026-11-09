"""ログイン以外の流量制限と、fail2ban などが読むセキュリティログ。docs/design/security.md 9 章。

- RequestRateLimit: パスごとの規則で、接続元 IP ごとの回数を数える（メモリ上。api は 1 プロセスのため）
- 共有リンク（/s/）で「無効なリンク」を開いた回数も IP ごとに数え、多ければ /s/ を止める
  （リンクのトークンを当てずっぽうに試す攻撃の対策）
- security_event: 1 行 1 件の決まった形式で logs/security.log に書く（deploy/fail2ban がこれを読む）
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

sec_log = logging.getLogger("docseed.security")
sec_log.propagate = False
_IP_SAFE = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")


def setup_security_log(logs_dir: Path) -> None:
    """logs/security.log に書く（5MB × 3 世代）。api の起動時に呼ぶ。"""
    for h in list(sec_log.handlers):
        sec_log.removeHandler(h)
        h.close()
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(logs_dir / "security.log", maxBytes=5 * 1024 * 1024,
                                                 backupCount=3, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s docseed-security %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    sec_log.addHandler(h)
    sec_log.setLevel(logging.INFO)


def security_event(event: str, ip: str | None, **detail: str | int) -> None:
    """例: 2026-11-09T14:00:00+0000 docseed-security event=login_failed ip=203.0.113.5

    ip は fail2ban がそのまま遮断に使うので、IP アドレスの形でないものは書かない。
    """
    if not ip or not _IP_SAFE.match(ip):
        return
    extra = "".join(f" {k}={re.sub(r'[^0-9A-Za-z_.:/-]', '_', str(v))[:64]}" for k, v in detail.items())
    sec_log.info("event=%s ip=%s%s", event, ip, extra)


# 監査ログのうち、セキュリティログにも書くもの（audit.audit から呼ぶ）
AUDIT_EVENTS = {"login_failed", "second_factor_failed", "reauth_failed"}


class SlidingWindow:
    """キーごとに、直近 window_s 秒の回数を数える。"""

    MAX_KEYS = 20000

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: int, *, count: bool = True) -> int:
        """今回を数えた（count=False なら数えずに）うえで上限を超えていれば、待つべき秒数を返す。超えていなければ 0。"""
        t = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.MAX_KEYS:
                    self._prune(t, window_s)
                q = self._hits[key] = deque()
            while q and t - q[0] >= window_s:
                q.popleft()
            if len(q) >= limit:
                return int(window_s - (t - q[0])) + 1
            if count:
                q.append(t)
            return 0

    def _prune(self, t: float, window_s: int) -> None:
        for k in [k for k, q in self._hits.items() if not q or t - q[-1] >= window_s]:
            del self._hits[k]
        while len(self._hits) >= self.MAX_KEYS:
            del self._hits[next(iter(self._hits))]

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


@dataclass(frozen=True)
class Rule:
    name: str
    methods: frozenset[str]
    pattern: re.Pattern
    limit: int
    window_s: int


def _rule(name: str, methods: str, pattern: str, limit: int, window_s: int) -> Rule:
    return Rule(name, frozenset(methods.split(",")), re.compile(pattern), limit, window_s)


# 上から順にすべて確かめる（1 つのリクエストが複数の規則に当たることがある）。
# 回数は普通の使い方では届かない程度にしてある（共有フォルダのサムネイルの一覧など）
RULES: tuple[Rule, ...] = (
    # ログインの開始（失敗の回数は security.LoginLimiter が別に数える。ここは成功も含めた量の上限）
    _rule("login", "POST", r"^/(login(/second-factor)?|api/v1/auth/login|auth/passkeys/login/options)$", 30, 60),
    _rule("oidc", "GET", r"^/auth/oidc/[^/]+/start$", 30, 60),
    # 共有リンク（ログイン不要）
    _rule("share", "GET,POST,HEAD", r"^/s/", 600, 60),
    _rule("share_upload", "POST", r"^/s/[^/]+/upload$", 30, 3600),
    # 検索（重い処理）
    _rule("search", "GET", r"^/(api/v1/)?search$", 60, 60),
)
SHARE_MISS_LIMIT = (20, 900)   # 無効なリンクを 15 分に 20 回開いたら、その IP の /s/ を止める


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.window = SlidingWindow()
        self.share_miss = SlidingWindow()

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)
        path, method = request.url.path, request.method
        ip = request.client.host if request.client else "-"
        is_share = path.startswith("/s/")
        if is_share:
            n, w = SHARE_MISS_LIMIT
            retry = self.share_miss.hit(f"miss:{ip}", n, w, count=False)
            if retry:
                return self._limited(request, ip, "share_invalid", retry)
        for r in RULES:
            if method in r.methods and r.pattern.search(path):
                retry = self.window.hit(f"{r.name}:{ip}", r.limit, r.window_s)
                if retry:
                    return self._limited(request, ip, r.name, retry)
        resp = await call_next(request)
        if is_share and getattr(request.state, "bad_share_token", False):
            security_event("share_invalid", ip)
            n, w = SHARE_MISS_LIMIT
            self.share_miss.hit(f"miss:{ip}", n, w)
        return resp

    @staticmethod
    def _limited(request: Request, ip: str, rule: str, retry: int) -> Response:
        security_event("rate_limited", ip, rule=rule)
        headers = {"Retry-After": str(retry)}
        msg = "アクセスが多すぎます。しばらくしてから再度お試しください"
        if request.url.path.startswith("/api/"):
            body = {"type": "https://docseed.local/errors/rate_limited", "title": msg, "status": 429,
                    "code": "rate_limited", "detail": msg, "instance": request.url.path}
            return JSONResponse(body, status_code=429, headers=headers, media_type="application/problem+json")
        return PlainTextResponse(msg, status_code=429, headers=headers)
