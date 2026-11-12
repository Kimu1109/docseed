"""worker の本体: ジョブを 1 つずつ取り出して処理する。docs/design/pipeline.md 2 章、deployment.md 6 章。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import signal
import sqlite3
import time
import traceback

from .. import db
from ..config import Settings
from ..db import Tx, now
from ..documents import purge_expired
from ..monitor import cpu_temp
from ..storage import BlobStore
from . import queue
from .process import Context, derive_blob, process_blob, reembed_blob, reset_blob, summarize_blob

log = logging.getLogger("docseed.worker")

IDLE_SLEEP = 2.0
HEARTBEAT_EVERY = 30.0
INBOX_EVERY = 30.0


class Interrupted(Exception):
    """停止の合図（SIGTERM）を受けた。処理中のジョブはやり直しに回す。"""


class Worker:
    def __init__(self, settings: Settings, ctx: Context | None = None):
        self.settings = settings
        self.conn = db.connect(settings.db_path)
        self.store = BlobStore(settings.files_dir, settings.tmp_dir)
        self.ctx = ctx or default_context(settings, self._between_pages)
        if ctx is not None:
            ctx.between_pages = self._between_pages
        self.stopping = False
        self._last_beat = 0.0
        self._last_inbox = 0.0
        self.inbox = None
        if settings.inbox_enabled:
            from ..inbox import InboxScanner
            self.inbox = InboxScanner(settings.inbox_dir, self.store, max_bytes=settings.max_upload_bytes,
                                      keep=settings.inbox_keep)

    # ---- 合間の処理（ページごと・埋め込みのバッチごと） -------------------
    def _between_pages(self) -> None:
        self.heartbeat()
        self.thermal_wait()
        if self.stopping:
            raise Interrupted()

    def heartbeat(self, force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - self._last_beat < HEARTBEAT_EVERY:
            return
        self._last_beat = t
        with Tx(self.conn):
            self.conn.execute("INSERT INTO settings(key, value) VALUES ('worker.heartbeat', ?)"
                              " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now(),))

    def thermal_wait(self) -> None:
        temp = cpu_temp()
        if temp is None or temp < self.settings.thermal_pause_c:
            return
        log.warning("CPU %.1f℃ のため処理を一時停止します", temp)
        while not self.stopping:
            time.sleep(5)
            self.heartbeat()
            temp = cpu_temp()
            if temp is None or temp < self.settings.thermal_resume_c:
                log.info("CPU %.1f℃ に下がったので再開します", temp or 0)
                return

    # ---- 定期処理 ----------------------------------------------------------
    def daily(self) -> None:
        last = self.conn.execute("SELECT value FROM settings WHERE key = 'worker.daily'").fetchone()
        today = dt.date.today().isoformat()
        if last and last[0] == today:
            return
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=self.settings.trash_retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        n = purge_expired(self.conn, self.store, cutoff)
        queue.cleanup(self.conn)
        with Tx(self.conn):
            self.conn.execute("INSERT INTO settings(key, value) VALUES ('worker.daily', ?)"
                              " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (today,))
        log.info("daily: ゴミ箱から %d 件を完全に削除しました", n)

    def scan_inbox(self, force: bool = False) -> int:
        if self.inbox is None:
            return 0
        t = time.monotonic()
        if not force and t - self._last_inbox < INBOX_EVERY:
            return 0
        self._last_inbox = t
        try:
            return self.inbox.scan(self.conn)
        except OSError as e:
            log.warning("inbox: %s", e)
            return 0

    # ---- ジョブ ------------------------------------------------------------
    def run_once(self) -> bool:
        """ジョブを 1 つ処理する。処理したら True。"""
        job = queue.claim(self.conn)
        if job is None:
            return False
        log.info("job %s %s blob=%s (attempt %s)", job["id"], job["type"], job["blob_id"], job["attempts"])
        try:
            if job["type"] == "process_blob":
                payload = json.loads(job["payload"]) if job["payload"] else {}
                if payload.get("reset"):
                    reset_blob(self.conn, job["blob_id"])
                result = process_blob(self.conn, self.ctx, job["blob_id"], force_ocr=bool(payload.get("force_ocr")))
            elif job["type"] == "reembed_blob":
                payload = json.loads(job["payload"]) if job["payload"] else {}
                if payload.get("missing_only"):   # 手で直した要約など、ベクトルの無いチャンクだけ
                    from .process import embed_chunks
                    result = f"embedded {embed_chunks(self.conn, self.ctx, job['blob_id'])}"
                else:
                    result = reembed_blob(self.conn, self.ctx, job["blob_id"])
            elif job["type"] == "suggest_document":
                from ..suggest import run_job
                doc_id = json.loads(job["payload"] or "{}").get("document_id")
                result = run_job(self.conn, int(doc_id), self.ctx.embedder) if self.ctx.suggest_enabled else "off"
            elif job["type"] == "summarize_blob":
                result = summarize_blob(self.conn, self.ctx, job["blob_id"])
            elif job["type"] == "derive_blob":
                result = derive_blob(self.conn, self.ctx, job["blob_id"])
            elif job["type"] == "purge_trash":
                self.daily()
                result = "done"
            else:
                raise ValueError(f"unknown job type: {job['type']}")
            queue.complete(self.conn, job["id"])
            log.info("job %s → %s", job["id"], result)
        except Interrupted:
            with Tx(self.conn):
                self.conn.execute("UPDATE jobs SET status = 'pending', locked_at = NULL, attempts = attempts - 1"
                                  " WHERE id = ?", (job["id"],))
            raise
        except Exception as e:  # noqa: BLE001 - どんな例外でもジョブの失敗として記録する
            msg = f"{type(e).__name__}: {e}"
            log.error("job %s failed: %s\n%s", job["id"], msg, traceback.format_exc())
            retrying = queue.fail(self.conn, job, msg)
            if not retrying and job["blob_id"] is not None:
                code = "out_of_memory" if isinstance(e, MemoryError) else "internal_error"
                with Tx(self.conn):
                    self.conn.execute(
                        "UPDATE blobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                        (json.dumps({"code": code, "message": "処理中にエラーが起きました。管理画面から再処理できます"},
                                    ensure_ascii=False), now(), job["blob_id"]))
        return True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.wait_for_schema()
        n = queue.recover_all_running(self.conn)
        if n:
            log.info("前回中断したジョブ %d 件をやり直します", n)
        log.info("worker started (ocr=%s, embed=%s)", self.settings.ocr_engine, self.settings.embed_model)
        while not self.stopping:
            try:
                self.heartbeat()
                self.daily()
                self.scan_inbox()
                if not self.run_once():
                    time.sleep(IDLE_SLEEP)
                else:
                    self.thermal_wait()
            except Interrupted:
                break
            except sqlite3.OperationalError as e:  # DB のロック待ちのタイムアウトなど
                log.warning("db: %s", e)
                time.sleep(IDLE_SLEEP)
        log.info("worker stopped")

    def wait_for_schema(self) -> None:
        """api がマイグレーションを適用し終えるまで待つ（worker 自身は適用しない）。"""
        latest = max(v for v, _, _ in db._migrations())
        while db.schema_version(self.conn) < latest and not self.stopping:
            log.info("waiting for migrations (%s < %s)", db.schema_version(self.conn), latest)
            time.sleep(2)

    def _stop(self, *_):
        self.stopping = True


def default_context(settings: Settings, between_pages) -> Context:
    from ..llm import LlmClient
    from ..pipeline.embed import OnnxEmbedder
    from ..pipeline.ocr import RapidOcrEngine

    model_type = settings.ocr_engine.removeprefix("ppocrv6_")
    return Context(
        settings.files_dir,
        ocr_factory=lambda: RapidOcrEngine(settings.models_dir, model_type=model_type,
                                           det_side=settings.ocr_det_side, threads=settings.ocr_threads),
        embedder_factory=lambda: OnnxEmbedder(settings.models_dir, settings.embed_model,
                                              threads=settings.embed_threads),
        dpi=settings.ocr_dpi, min_score=settings.ocr_min_score, between_pages=between_pages,
        data_dir=settings.data_dir, suggest_enabled=settings.suggest_enabled, pdf_images=settings.ocr_pdf_images,
        llm=LlmClient(settings.llm_url, settings.llm_model, timeout_s=settings.llm_timeout_s,
                      max_input_chars=settings.llm_max_input_chars) if settings.llm_enabled else None,
    )
