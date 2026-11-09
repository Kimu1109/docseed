"""HTTPS 証明書の監視。docs/design/extensions.md 2.4。

api が 6 時間ごとに Caddy（DOCSEED_TLS_CHECK_ADDR、compose では caddy:<DOCSEED_HTTPS_PORT>）へ
DOCSEED_HOST の名前（SNI）で TLS 接続し、証明書を検証して有効期限を読む。結果は settings の
'tls.status' に JSON で置き、管理画面の「確認が必要な項目」（monitor.py）が読む。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
import ssl
import threading
import time
from pathlib import Path

from .config import Settings, _first_host
from .db import Tx, connect

log = logging.getLogger("docseed.tlscheck")

INTERVAL_S = 6 * 3600
FIRST_DELAY_S = 120          # 起動直後は Caddy が証明書を取り終えていないことがある
WARN_DAYS = 7
TIMEOUT_S = 10


def internal_ca(settings: Settings) -> Path:
    """Caddy の内部 CA のルート証明書（compose で data_dir/caddy を Caddy の /data にマウントしている）。"""
    return settings.data_dir / "caddy" / "caddy" / "pki" / "authorities" / "local" / "root.crt"


def _not_after(der: bytes) -> dt.datetime:
    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    return cert.not_valid_after_utc


def check(addr: str, server_name: str, *, cafile: Path | None = None, now_: dt.datetime | None = None) -> dict:
    """addr（host:port）へ server_name で接続して証明書を確かめる。

    返り値: {ok, error, not_after, days_left, issuer, checked_at}。検証に失敗しても、読めれば期限は入れる。
    """
    t = now_ or dt.datetime.now(dt.UTC)
    host, _, port = addr.rpartition(":")
    out: dict = {"ok": False, "error": None, "not_after": None, "days_left": None, "issuer": None,
                 "server_name": server_name, "checked_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}
    ctx = ssl.create_default_context(cafile=str(cafile) if cafile else None)
    der = None
    try:
        with socket.create_connection((host, int(port)), timeout=TIMEOUT_S) as sock, \
                ctx.wrap_socket(sock, server_hostname=server_name) as tls:
            der = tls.getpeercert(binary_form=True)
            issuer = dict(x[0] for x in tls.getpeercert().get("issuer", ()))
            out["issuer"] = issuer.get("organizationName") or issuer.get("commonName")
        out["ok"] = True
    except ssl.SSLCertVerificationError as e:
        out["error"] = e.verify_message or str(e)
        der = _fetch_unverified(host, int(port), server_name)
    except (OSError, ssl.SSLError, ValueError) as e:
        out["error"] = f"接続できません: {e}"
    if der:
        na = _not_after(der)
        out["not_after"] = na.strftime("%Y-%m-%dT%H:%M:%SZ")
        out["days_left"] = round((na - t).total_seconds() / 86400, 1)
        if out["ok"] and na <= t:
            out["ok"], out["error"] = False, "証明書の期限が切れています"
    return out


def _fetch_unverified(host: str, port: int, server_name: str) -> bytes | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as sock, \
                ctx.wrap_socket(sock, server_hostname=server_name) as tls:
            return tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None


def check_and_store(conn: sqlite3.Connection, settings: Settings) -> dict | None:
    name = _first_host(settings.host)
    if not settings.tls_check_addr or not name:
        return None
    cafile = None
    if settings.tls_mode == "internal":
        cafile = internal_ca(settings)
        if not cafile.exists():
            cafile = None
    res = check(settings.tls_check_addr, name, cafile=cafile)
    res["mode"] = settings.tls_mode
    with Tx(conn):
        conn.execute("INSERT INTO settings(key, value) VALUES ('tls.status', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(res, ensure_ascii=False),))
    log.info("tls check %s: ok=%s days_left=%s error=%s", name, res["ok"], res["days_left"], res["error"])
    return res


def stored_status(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT value FROM settings WHERE key = 'tls.status'").fetchone()
    try:
        return json.loads(r[0]) if r else None
    except ValueError:
        return None


def start_background(settings: Settings) -> threading.Thread | None:
    """api の中で 6 時間ごとに確かめるスレッドを起動する（DOCSEED_TLS_CHECK_ADDR が無ければ何もしない）。"""
    if not settings.tls_check_addr or not _first_host(settings.host):
        return None

    def loop():
        time.sleep(FIRST_DELAY_S)
        while True:
            try:
                conn = connect(settings.db_path)
                try:
                    check_and_store(conn, settings)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 - 監視の失敗で api を止めない
                log.exception("tls check failed")
            time.sleep(INTERVAL_S)

    th = threading.Thread(target=loop, name="tls-check", daemon=True)
    th.start()
    return th


def alerts(status: dict | None) -> list[tuple[str, str, str]]:
    """(level, code, message) の一覧。"""
    if not status:
        return []
    if not status.get("ok"):
        return [("error", "tls_invalid", f"HTTPS の証明書を確認できません（{status.get('server_name')}）: "
                 f"{status.get('error')}。docker compose logs caddy で証明書の取得の状況を確認してください")]
    days = status.get("days_left")
    if days is not None and days < WARN_DAYS:
        return [("warning", "tls_expiring", f"HTTPS の証明書の期限まで {days:.0f} 日です。自動の更新が失敗している"
                 "可能性があります（docker compose logs caddy）")]
    return []

