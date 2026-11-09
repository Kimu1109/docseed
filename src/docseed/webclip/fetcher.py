"""fetcher の本体: fetch_url ジョブだけを 1 件ずつ処理する。docs/design/extensions.md 6.2。

api と同じイメージで `docseed fetcher` として動かす（compose の profile `webclip`）。worker と同じジョブキューを
使うが、worker は fetch_url を取らない（worker は外部と通信しないまま）。
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
import traceback

from .. import db
from ..config import Settings
from ..db import Tx, now
from ..jobs import queue
from ..storage import BlobStore
from . import service
from .safefetch import FetchError, SafeFetcher

log = logging.getLogger("docseed.fetcher")

IDLE_SLEEP = 2.0
HEARTBEAT_EVERY = 30.0


class Fetcher:
    def __init__(self, settings: Settings, safe: SafeFetcher | None = None):
        self.settings = settings
        self.conn = db.connect(settings.db_path)
        self.store = BlobStore(settings.files_dir, settings.tmp_dir)
        self.safe = safe or SafeFetcher(proxy=settings.webclip_proxy or None)
        self.stopping = False
        self._last_beat = 0.0

    def heartbeat(self) -> None:
        t = time.monotonic()
        if t - self._last_beat < HEARTBEAT_EVERY:
            return
        self._last_beat = t
        with Tx(self.conn):
            self.conn.execute("INSERT INTO settings(key, value) VALUES ('fetcher.heartbeat', ?)"
                              " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now(),))

    def run_once(self) -> bool:
        job = queue.claim(self.conn, only=queue.FETCH_TYPES)
        if job is None:
            return False
        clip_id = json.loads(job["payload"] or "{}").get("clip_id")
        log.info("job %s fetch_url clip=%s (attempt %s)", job["id"], clip_id, job["attempts"])
        try:
            result = service.process(self.conn, self.settings, self.store, self.safe, int(clip_id))
            queue.complete(self.conn, job["id"])
            log.info("job %s → %s", job["id"], result)
        except FetchError as e:
            retrying = queue.fail(self.conn, job, f"{e.code}: {e}", retry=e.retry, delays=service.RETRY_DELAYS)
            service.mark_failed(self.conn, int(clip_id), e, final=not retrying)
            log.warning("job %s clip=%s %s: %s%s", job["id"], clip_id, e.code, e,
                        "（やり直します）" if retrying else "")
        except Exception as e:  # noqa: BLE001 - どんな例外でもジョブの失敗として記録する
            log.error("job %s failed: %s\n%s", job["id"], e, traceback.format_exc())
            retrying = queue.fail(self.conn, job, f"{type(e).__name__}: {e}", delays=service.RETRY_DELAYS)
            service.mark_failed(self.conn, int(clip_id), FetchError(
                "internal_error", "取り込み中にエラーが起きました。もう一度お試しください"), final=not retrying)
        return True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        latest = max(v for v, _, _ in db._migrations())
        while db.schema_version(self.conn) < latest and not self.stopping:
            log.info("waiting for migrations")
            time.sleep(2)
        with Tx(self.conn):
            # 自分は 1 つしか動かないので、running の fetch_url は前回の中断分
            self.conn.execute("UPDATE jobs SET status = 'pending', locked_at = NULL"
                              " WHERE status = 'running' AND type = 'fetch_url'")
        log.info("fetcher started (proxy=%s)", "yes" if self.settings.webclip_proxy else "no")
        while not self.stopping:
            try:
                self.heartbeat()
                if not self.run_once():
                    time.sleep(IDLE_SLEEP)
            except sqlite3.OperationalError as e:
                log.warning("db: %s", e)
                time.sleep(IDLE_SLEEP)
        log.info("fetcher stopped")

    def _stop(self, *_):
        self.stopping = True
