"""安全な取得（SSRF 対策）。docs/design/extensions.md 6.3。

LAN の機器や Pi 自身のサービスへのリクエストを作らせないために:

- http / https だけ、ポートは 80・443・8080・8443 だけ。URL の中の認証情報は拒否する
- 名前は自分で引き、返ったすべての IP がグローバルであることを確かめる
- 確かめた IP に直接つなぐ（URL のホストを IP にし、Host ヘッダと TLS の SNI・証明書の検証には元の名前を使う）。
  名前を引き直さないので、DNS リバインディングで別の IP に変わっても影響しない
- リダイレクトは自動では追わず、最大 5 回まで、毎回この確認をやり直す
- 大きさ・時間の上限。Cookie は送らない（取得のたびに新しいクライアントを使う）
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import IO
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .. import __version__

ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 5.0
TOTAL_TIMEOUT = 30.0
USER_AGENT = f"docseed/{__version__} (+self-hosted; webclip)"
MAX_URL_LEN = 2048

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    """code は extensions.md 6.8 のエラーコード。retry はジョブをやり直す価値があるか。"""

    def __init__(self, code: str, message: str, *, retry: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry = retry
        self.http_status = http_status


@dataclass
class Fetched:
    url: str                      # 最後のリダイレクト先
    status: int
    mime: str                     # Content-Type の種類（小文字、パラメータなし）
    charset: str | None
    file: IO[bytes]               # 本体（8MB を超えたら一時ファイルに逃がす。先頭に戻してある）
    size: int
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        self.file.seek(0)
        data = self.file.read()
        self.file.seek(0)
        return data

    def head(self, n: int = 8192) -> bytes:
        self.file.seek(0)
        data = self.file.read(n)
        self.file.seek(0)
        return data

    def close(self) -> None:
        self.file.close()


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """グローバルなアドレスだけを許す（プライベート・ループバック・リンクローカル・CGNAT・マルチキャスト・予約済みは拒否）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """取得の前の形式の確認（api でも使う）。不正なら url_invalid。"""
    if not url or len(url) > MAX_URL_LEN:
        raise FetchError("url_invalid", f"URL は {MAX_URL_LEN} 文字までです")
    try:
        u = urlsplit(url.strip())
        port = u.port
    except ValueError:
        raise FetchError("url_invalid", "URL の形式が正しくありません") from None
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("url_invalid", "http:// か https:// で始まる URL を入れてください")
    if u.username or u.password:
        raise FetchError("url_invalid", "URL に利用者名・パスワードを含めないでください")
    del port


def _content_type(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "", None
    parts = [p.strip() for p in value.split(";")]
    charset = None
    for p in parts[1:]:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip("\"' ") or None
    return parts[0].lower(), charset


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n >= 1024 * 1024 else f"{n} バイト"


class SafeFetcher:
    def __init__(self, *, proxy: str | None = None, resolver: Resolver | None = None,
                 ip_allowed: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] = ip_is_public,
                 ports: Iterable[int] = ALLOWED_PORTS, verify: bool | ssl.SSLContext = True,
                 connect_timeout: float = CONNECT_TIMEOUT, total_timeout: float = TOTAL_TIMEOUT):
        self.proxy = proxy or None
        self.resolver = resolver or system_resolver
        self.ip_allowed = ip_allowed
        self.ports = frozenset(ports)
        self.verify = verify
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout

    # ---- 確認 -----------------------------------------------------------------
    def _target(self, url: str) -> tuple[str, dict[str, str], dict]:
        """(実際に接続する URL, 追加のヘッダ, httpx の extensions) を返す。安全でなければ url_blocked。"""
        check_url(url)
        u = urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        if port not in self.ports:
            raise FetchError("url_blocked", f"ポート {port} への取得は許可していません（80・443・8080・8443 のみ）")
        literal = None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        if literal is not None:
            if not self.ip_allowed(literal):
                raise FetchError("url_blocked", "LAN などのプライベートなアドレスからは取得できません")
            return url, {}, {}
        if self.proxy:
            # 名前の解決はプロキシに任せる（プロキシ側で LAN への通信を止めること。docs/admin/webclip.md）
            return url, {}, {}
        try:
            ips = self.resolver(host, port)
        except (OSError, UnicodeError) as e:
            raise FetchError("fetch_connect_error", f"名前を引けませんでした（{host}）: {e}", retry=True) from None
        if not ips:
            raise FetchError("fetch_connect_error", f"名前を引けませんでした（{host}）", retry=True)
        addrs = []
        for s in ips:
            ip = ipaddress.ip_address(s.split("%", 1)[0])
            if not self.ip_allowed(ip):
                raise FetchError("url_blocked", f"{host} は LAN などのプライベートなアドレス（{ip}）を指しているため"
                                 "取得できません")
            addrs.append(ip)
        ip = addrs[0]
        netloc_ip = f"[{ip}]" if ip.version == 6 else str(ip)
        if u.port:
            netloc_ip += f":{u.port}"
        pinned = urlunsplit((u.scheme, netloc_ip, u.path or "/", u.query, ""))
        host_header = host if not u.port else f"{host}:{u.port}"
        ext = {"sni_hostname": host} if u.scheme == "https" else {}
        return pinned, {"Host": host_header}, ext

    # ---- 取得 -----------------------------------------------------------------
    def fetch(self, url: str, *, max_bytes: int | Callable[[str], int], accept: str = "*/*",
              tmp_dir: str | None = None) -> Fetched:
        """取得する。max_bytes は上限、または Content-Type の種類から上限を返す関数。"""
        deadline = time.monotonic() + self.total_timeout
        current = url.strip()
        for _hop in range(MAX_REDIRECTS + 1):
            target, extra, ext = self._target(current)
            headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "ja,en;q=0.8", **extra}
            left = max(1.0, deadline - time.monotonic())
            timeout = httpx.Timeout(min(left, self.total_timeout), connect=min(self.connect_timeout, left))
            try:
                with httpx.Client(follow_redirects=False, verify=self.verify, trust_env=False, timeout=timeout,
                                  proxy=self.proxy) as client, \
                        client.stream("GET", target, headers=headers, extensions=ext) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise FetchError("fetch_http_error", f"ページを取得できませんでした（HTTP {r.status_code}）",
                                         retry=r.status_code >= 500 or r.status_code == 429,
                                         http_status=r.status_code)
                    if r.status_code != 200:
                        raise FetchError("fetch_http_error", f"想定していない応答です（HTTP {r.status_code}）",
                                         http_status=r.status_code)
                    mime, charset = _content_type(r.headers.get("content-type"))
                    limit = max_bytes(mime) if callable(max_bytes) else max_bytes
                    too_large = FetchError("fetch_too_large", f"大きすぎます（上限 {_mb(limit)}）")
                    length = r.headers.get("content-length")
                    if length and length.isdigit() and int(length) > limit:
                        raise too_large
                    out = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, dir=tmp_dir)
                    size = 0
                    try:
                        for chunk in r.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise too_large
                            if time.monotonic() > deadline:
                                raise FetchError("fetch_timeout",
                                                 f"{self.total_timeout:.0f} 秒以内に取得できませんでした", retry=True)
                            out.write(chunk)
                    except BaseException:
                        out.close()
                        raise
                    out.seek(0)
                    return Fetched(current, r.status_code, mime, charset, out, size,
                                   {k.lower(): v for k, v in r.headers.items()})
            except httpx.TimeoutException:
                raise FetchError("fetch_timeout", f"{self.total_timeout:.0f} 秒以内に取得できませんでした",
                                 retry=True) from None
            except httpx.HTTPError as e:
                raise FetchError("fetch_connect_error", f"接続できませんでした: {e}", retry=True) from None
        raise FetchError("fetch_http_error", f"リダイレクトが {MAX_REDIRECTS} 回を超えました")
