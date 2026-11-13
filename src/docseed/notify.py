"""通知（docs/design/deployment.md 6.1）。

- 監視の警告（monitor.status）が出た・重くなった・解消したとき、共有リンクからファイルを受け取ったとき、
  受け取り箱（inbox）からファイルを取り込んだときに、Webhook（ntfy / Discord / Slack / JSON）とメールで知らせる。
- 知らせることは notifications 表に積み、api の中のスレッドがまとめて送る（worker は外部と通信しないため）。
- 宛先（DOCSEED_NOTIFY_URLS / DOCSEED_NOTIFY_EMAIL）が無ければ何も積まない。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import smtplib
import sqlite3
import threading
import time
from collections.abc import Callable
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .db import Tx, connect, now

log = logging.getLogger(__name__)

LEVELS = {"info": 0, "warning": 1, "error": 2}
LEVEL_LABEL = {"info": "お知らせ", "warning": "注意", "error": "異常"}
EVENTS = ("alert", "share_upload", "inbox")
EVENT_LABEL = {"alert": "監視の警告", "share_upload": "共有リンクでの受け取り", "inbox": "受け取り箱の取り込み",
               "test": "テスト"}
SEND_EVERY_S = 60             # 送信待ちを送る間隔
CHECK_EVERY_S = 300           # 監視の警告を確かめる間隔
FIRST_DELAY_S = 90            # 起動直後（worker の再起動中など）は少し待ってから確かめる
REMIND_S = 24 * 3600          # 続いている警告は 1 日ごとに知らせ直す
MAX_ATTEMPTS = 5
KEEP_DAYS = 30                # 送った記録を残す日数
MAX_LINES = 20                # まとめて送るときの行数の上限
HTTP_TIMEOUT_S = 10.0
FORMATS = ("ntfy", "discord", "slack", "json")


# ---------------------------------------------------------------------------
# 積む
# ---------------------------------------------------------------------------

def enqueue(conn: sqlite3.Connection, settings: Settings, kind: str, title: str, body: str, *,
            level: str = "info", group_key: str | None = None) -> int | None:
    """通知を積む。宛先が無い・その種類を知らせない設定なら何もしない（None）。"""
    if not settings.notify_enabled or (kind != "test" and kind not in settings.notify_events):
        return None
    with Tx(conn):
        cur = conn.execute("INSERT INTO notifications(kind, level, title, body, group_key, created_at)"
                           " VALUES (?,?,?,?,?,?)", (kind, level, title, body, group_key, now()))
    return int(cur.lastrowid)


def _link(settings: Settings, path: str) -> str:
    """本文の最後に付けるリンク（"\nhttps://..."）。外から開く URL が分からなければ付けない。"""
    base = settings.public_url
    return "\n" + base.rstrip("/") + path if base else ""


def share_upload(conn: sqlite3.Connection, settings: Settings, share: sqlite3.Row, owner_name: str,
                 folder_id: int, folder_name: str, names: list[str]) -> None:
    """共有リンク（受け取り用）からファイルを受け取った。"""
    if not names:
        return
    lines = [f"・{n}" for n in names]
    enqueue(conn, settings, "share_upload", "共有リンクでファイルを受け取りました",
            f"{owner_name} さんの「{folder_name or 'マイファイル'}」に {len(names)} 件\n" + "\n".join(lines)
            + _link(settings, f"/f/{folder_id}"), group_key=f"share:{share['id']}")


def inbox_imported(conn: sqlite3.Connection, settings: Settings, username: str, path: str, *,
                   ok: bool, message: str = "") -> None:
    """受け取り箱（inbox）からファイルを取り込んだ（または取り込めなかった）。"""
    if ok:
        enqueue(conn, settings, "inbox", "受け取り箱からファイルを取り込みました", f"{username}: {path}",
                group_key=f"inbox:{username}")
    else:
        enqueue(conn, settings, "inbox", "受け取り箱のファイルを取り込めませんでした",
                f"{username}: {path}（{message}）", level="warning", group_key=f"inbox-failed:{username}")


# ---------------------------------------------------------------------------
# 監視の警告
# ---------------------------------------------------------------------------

def _state(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT value FROM settings WHERE key = 'notify.alerts'").fetchone()
    try:
        return json.loads(r[0]) if r else {}
    except ValueError:
        return {}


def _age_s(ts: str | None, t: dt.datetime) -> float:
    if not ts:
        return float("inf")
    return (t - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def check_alerts(conn: sqlite3.Connection, settings: Settings, *, now_: dt.datetime | None = None) -> int:
    """警告（warning 以上）を前回の状態と比べて、知らせるものを積む。積んだ数を返す。

    一時的なもの（worker の再起動中など）で知らせないよう、2 回続けて出たときに知らせる。
    知らせた警告がもっと重くなったら知らせ直し、続いていれば 1 日ごとに知らせ直す。消えたら「解消」を知らせる。
    """
    from .monitor import status
    t = now_ or dt.datetime.now(dt.UTC)
    ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
    current = {a["code"]: a for a in status(conn, settings)["alerts"] if LEVELS[a["level"]] >= LEVELS["warning"]}
    prev = _state(conn)
    state: dict[str, dict] = {}
    n = 0
    admin = _link(settings, "/admin")
    for code, a in current.items():
        p = prev.get(code)
        p = p or {}
        s = {"level": a["level"], "message": a["message"], "first_seen": p.get("first_seen", ts),
             "notified_at": p.get("notified_at"), "notified_level": p.get("notified_level")}
        due = bool(p) and (
            s["notified_at"] is None
            or LEVELS[a["level"]] > LEVELS.get(s["notified_level"] or "info", 0)
            or _age_s(s["notified_at"], t) >= REMIND_S)
        if due and enqueue(conn, settings, "alert", f"[{LEVEL_LABEL[a['level']]}] docseed の警告",
                           a["message"] + admin, level=a["level"]):
            s["notified_at"], s["notified_level"] = ts, a["level"]
            n += 1
        state[code] = s
    for code, p in prev.items():
        if code not in current and p.get("notified_at"):
            if enqueue(conn, settings, "alert", "[解消] docseed の警告", f"解消しました: {p.get('message', code)}"):
                n += 1
    with Tx(conn):
        conn.execute("INSERT INTO settings(key, value) VALUES ('notify.alerts', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (json.dumps(state, ensure_ascii=False),))
    return n


# ---------------------------------------------------------------------------
# 送る
# ---------------------------------------------------------------------------

def parse_destination(url: str) -> tuple[str, str]:
    """宛先の URL を (形式, URL) にする。「ntfy+https://...」のように形式を前に付けられる。

    付けなければ、Discord・Slack の Webhook と ntfy.sh はホスト名で見分け、それ以外は JSON にする。
    """
    head, sep, rest = url.partition("+")
    if sep and head in FORMATS and rest.startswith(("http://", "https://")):
        return head, rest
    host = (urlsplit(url).hostname or "").lower()
    if host in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"):
        return "discord", url
    if host == "hooks.slack.com":
        return "slack", url
    if host == "ntfy.sh":
        return "ntfy", url
    return "json", url


def describe_destination(url: str) -> str:
    """画面に出す宛先（Webhook の URL は秘密を含むので、形式とホスト名だけにする）。"""
    fmt, u = parse_destination(url)
    return f"{fmt}（{urlsplit(u).hostname or '?'}）"


def _webhook_request(fmt: str, url: str, title: str, body: str, level: str) -> tuple[str, dict, dict]:
    """(URL, JSON, ヘッダ)。"""
    if fmt == "ntfy":
        # JSON で送る（ヘッダの Title は日本語を入れにくいため）。https://host/topic → https://host/ に topic を付ける
        parts = urlsplit(url)
        topic = parts.path.strip("/")
        root = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        return root, {"topic": topic, "title": title, "message": body,
                      "priority": {"error": 5, "warning": 4, "info": 3}[level],
                      "tags": [{"error": "rotating_light", "warning": "warning", "info": "inbox_tray"}[level]]}, {}
    if fmt == "discord":
        return url, {"content": f"**{title}**\n{body}"[:2000]}, {}
    if fmt == "slack":
        return url, {"text": f"*{title}*\n{body}"}, {}
    return url, {"source": "docseed", "level": level, "title": title, "message": body, "text": f"{title}\n{body}"}, {}


def _send_email(settings: Settings, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(settings.notify_email)
    msg.set_content(body + "\n\n-- \ndocseed")
    cls = smtplib.SMTP_SSL if settings.smtp_security == "ssl" else smtplib.SMTP
    with cls(settings.smtp_host, settings.smtp_port, timeout=HTTP_TIMEOUT_S) as s:
        if settings.smtp_security == "starttls":
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def deliver(settings: Settings, title: str, body: str, level: str = "info", *,
            http: httpx.Client | None = None, email: Callable[[Settings, str, str], None] | None = None) -> list[str]:
    """すべての宛先へ送る。失敗した宛先の説明の一覧を返す（すべて失敗したら例外）。"""
    errors: list[str] = []
    sent = 0
    client = http or httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=False)
    try:
        for u in settings.notify_urls:
            fmt, url = parse_destination(u)
            try:
                target, payload, headers = _webhook_request(fmt, url, title, body, level)
                r = client.post(target, json=payload, headers=headers)
                r.raise_for_status()
                sent += 1
            except Exception as e:  # noqa: BLE001 - 宛先ごとに記録して、ほかの宛先へは送る
                errors.append(f"{describe_destination(u)}: {type(e).__name__}: {str(e)[:200]}")
    finally:
        if http is None:
            client.close()
    if settings.smtp_host and settings.notify_email:
        try:
            (email or _send_email)(settings, title, body)
            sent += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"メール（{settings.smtp_host}）: {type(e).__name__}: {str(e)[:200]}")
    if errors and not sent:
        raise RuntimeError("; ".join(errors))
    return errors


def _merge(rows: list[sqlite3.Row]) -> tuple[str, str, str]:
    """同じ group_key の送信待ちを 1 通にする。"""
    level = max((r["level"] for r in rows), key=lambda x: LEVELS[x])
    if len(rows) == 1:
        return rows[0]["title"], rows[0]["body"], level
    lines: list[str] = []
    for r in rows:
        lines += r["body"].splitlines()
    # 同じリンク（最後の行の URL）は 1 つにまとめる
    urls = list(dict.fromkeys(x for x in lines if x.startswith(("http://", "https://"))))
    text = [x for x in lines if x not in urls]
    if len(text) > MAX_LINES:
        text = text[:MAX_LINES] + [f"ほか {len(text) - MAX_LINES} 行"]
    return f"{rows[0]['title']}（{len(rows)} 回分）", "\n".join(text + urls), level


def send_pending(conn: sqlite3.Connection, settings: Settings, **kw) -> int:
    """送信待ちを送る。送った通数を返す。kw は deliver へ（テスト用）。"""
    rows = conn.execute("SELECT * FROM notifications WHERE status = 'pending' ORDER BY id").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["group_key"] or f"id:{r['id']}", []).append(r)
    n = 0
    for grp in groups.values():
        ids = [r["id"] for r in grp]
        q = ",".join("?" * len(ids))
        title, body, level = _merge(grp)
        try:
            errors = deliver(settings, title, body, level, **kw)
        except Exception as e:  # noqa: BLE001
            attempts = max(r["attempts"] for r in grp) + 1
            with Tx(conn):
                conn.execute(f"UPDATE notifications SET attempts = ?, error = ?, status = ? WHERE id IN ({q})",
                             (attempts, str(e)[:1000], "failed" if attempts >= MAX_ATTEMPTS else "pending", *ids))
            log.warning("通知を送れませんでした（%d 回目）: %s", attempts, e)
            continue
        with Tx(conn):
            conn.execute(f"UPDATE notifications SET status = 'sent', sent_at = ?, error = ?,"
                         f" attempts = attempts + 1 WHERE id IN ({q})", (now(), "; ".join(errors) or None, *ids))
        n += 1
    return n


def cleanup(conn: sqlite3.Connection, *, now_: dt.datetime | None = None) -> int:
    cutoff = ((now_ or dt.datetime.now(dt.UTC)) - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with Tx(conn):
        return conn.execute("DELETE FROM notifications WHERE status <> 'pending' AND created_at < ?",
                            (cutoff,)).rowcount


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def send_test(conn: sqlite3.Connection, settings: Settings, **kw) -> sqlite3.Row:
    """テスト通知を積んで、すぐに送る（管理画面の「テスト通知を送る」）。結果の行を返す。"""
    nid = enqueue(conn, settings, "test", "docseed のテスト通知",
                  "通知の設定ができています。" + _link(settings, "/admin"))
    if nid is None:
        raise ValueError("通知の宛先が設定されていません")
    row = conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()
    title, body, level = _merge([row])
    try:
        errors = deliver(settings, title, body, level, **kw)
        with Tx(conn):
            conn.execute("UPDATE notifications SET status = 'sent', sent_at = ?, attempts = 1, error = ?"
                         " WHERE id = ?", (now(), "; ".join(errors) or None, nid))
    except Exception as e:  # noqa: BLE001 - テストは再送しない
        with Tx(conn):
            conn.execute("UPDATE notifications SET status = 'failed', attempts = 1, error = ? WHERE id = ?",
                         (str(e)[:1000], nid))
    return conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()


# ---------------------------------------------------------------------------
# api の中で動かす
# ---------------------------------------------------------------------------

def start_background(settings: Settings) -> threading.Thread | None:
    """送信待ちを 1 分ごとに送り、警告を 5 分ごとに確かめるスレッド（宛先が無ければ起動しない）。"""
    if not settings.notify_enabled:
        return None

    def loop():
        time.sleep(FIRST_DELAY_S)
        last_check = last_cleanup = 0.0
        while True:
            try:
                conn = connect(settings.db_path)
                try:
                    t = time.monotonic()
                    if "alert" in settings.notify_events and t - last_check >= CHECK_EVERY_S:
                        check_alerts(conn, settings)
                        last_check = t
                    send_pending(conn, settings)
                    if t - last_cleanup >= 24 * 3600:
                        cleanup(conn)
                        last_cleanup = t
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 - 通知の失敗で api を止めない
                log.exception("notify loop failed")
            time.sleep(SEND_EVERY_S)

    th = threading.Thread(target=loop, name="notify", daemon=True)
    th.start()
    return th
