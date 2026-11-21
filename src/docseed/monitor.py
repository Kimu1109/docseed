"""システムの状態と警告（管理画面・ヘッダの警告・/api/v1/admin/status）。docs/design/deployment.md 6 章。"""

from __future__ import annotations

import datetime as dt
import os
import platform
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .backup import latest_snapshot, restic_status, restore_drill_status
from .config import Settings
from .i18n import N_

WORKER_STALE_SECS = 120          # worker の応答がこれ以上無ければ警告
DISK_WARN_RATIO = 0.10           # 空きが 10% を切ったら警告（5% でアップロードを拒否: storage.MIN_FREE_RATIO）
DISK_ERROR_RATIO = 0.05
BACKUP_STALE_HOURS = 36          # 日次のバックアップが 1 回抜けたら気づけるように
DRILL_STALE_DAYS = 40            # 月 1 回の復旧の練習が 1 回抜けたら気づけるように
THERMAL_PATHS = [Path("/host/thermal/thermal_zone0/temp"), Path("/sys/class/thermal/thermal_zone0/temp")]


@dataclass
class Alert:
    """確認が必要な項目。template は訳のキー（日本語の文の型）、message は値を差し込んだ日本語の文（通知・CLI 用）。"""

    level: str      # error / warning / info
    code: str
    template: str
    params: dict = field(default_factory=dict)
    message: str = ""

    def __post_init__(self):
        self.message = self.template.format(**self.params) if self.params else self.template


def cpu_temp() -> float | None:
    for p in THERMAL_PATHS:
        try:
            return int(p.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def memory() -> dict | None:
    """機械全体のメモリ（/proc/meminfo。コンテナの中でも Pi 全体の値になる）。バイト。"""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            parts = v.split()
            if parts and parts[0].isdigit():
                info[k] = int(parts[0]) * 1024
    except OSError:
        return None
    if "MemTotal" not in info:
        return None
    return {"total": info["MemTotal"], "available": info.get("MemAvailable", info.get("MemFree", 0)),
            "swap_total": info.get("SwapTotal", 0), "swap_free": info.get("SwapFree", 0)}


def load_average() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _age(t: dt.datetime | None, now: dt.datetime) -> float | None:
    return None if t is None else (now - t).total_seconds()


def status(conn: sqlite3.Connection, settings: Settings, *, now: dt.datetime | None = None) -> dict:
    """状態の一覧と警告。管理画面と API で同じものを使う。"""
    now = now or dt.datetime.now(dt.UTC)
    alerts: list[Alert] = []

    hb = conn.execute("SELECT value FROM settings WHERE key = 'worker.heartbeat'").fetchone()
    worker_seen = _parse(hb[0]) if hb else None
    worker_age = _age(worker_seen, now)
    worker_alive = worker_age is not None and worker_age < WORKER_STALE_SECS
    if worker_seen is None:
        alerts.append(Alert("warning", "worker_never", N_("文字の読み取り（worker）がまだ一度も動いていません")))
    elif not worker_alive:
        alerts.append(Alert("error", "worker_stale", N_("文字の読み取り（worker）から 2 分以上応答がありません")))

    jobs = dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
    if jobs.get("failed"):
        alerts.append(Alert("warning", "jobs_failed", N_("失敗したジョブが {n} 件あります"), {"n": jobs["failed"]}))

    disk = shutil.disk_usage(settings.data_dir)
    ratio = disk.free / disk.total if disk.total else 1.0
    if ratio < DISK_ERROR_RATIO:
        alerts.append(Alert("error", "disk_full",
                            N_("SSD の空きが 5% を下回っています。アップロードを受け付けていません")))
    elif ratio < DISK_WARN_RATIO:
        alerts.append(Alert("warning", "disk_low", N_("SSD の空きが 10% を下回っています")))

    backup_dir = settings.data_dir / "backup"
    snap = latest_snapshot(backup_dir)
    snap_time = dt.datetime.fromtimestamp(snap.stat().st_mtime, dt.UTC) if snap else None
    snap_age = _age(snap_time, now)
    if snap is None:
        alerts.append(Alert("warning", "db_backup_none", N_("データベースのスナップショットがまだありません")))
    elif snap_age is not None and snap_age > BACKUP_STALE_HOURS * 3600:
        alerts.append(Alert("warning", "db_backup_stale",
                            N_("データベースのスナップショットが 36 時間以上作られていません")))

    restic = restic_status(backup_dir)
    restic_time = _parse(restic.get("time")) if restic else None
    restic_age = _age(restic_time, now)
    if restic is None:
        alerts.append(Alert("info", "restic_none", N_("別の機器へのバックアップ（restic）の記録がありません")))
    elif not restic.get("ok"):
        alerts.append(Alert("error", "restic_failed", N_("前回のバックアップ（restic）が失敗しました: {detail}"),
                            {"detail": restic["message"]}) if restic.get("message") else
                      Alert("error", "restic_failed", N_("前回のバックアップ（restic）が失敗しました: "
                                                         "詳細はホストの journalctl -u docseed-backup")))
    elif restic_age is not None and restic_age > BACKUP_STALE_HOURS * 3600:
        alerts.append(Alert("warning", "restic_stale",
                            N_("別の機器へのバックアップ（restic）が 36 時間以上成功していません")))

    drill = restore_drill_status(backup_dir)
    drill_ok_at = _parse(drill.get("last_success")) if drill else None
    drill_age = _age(drill_ok_at, now)
    if drill is None:
        alerts.append(Alert("info", "drill_none",
                            N_("復旧の練習の記録がありません（sudo scripts/ops/install-backup.sh で"
                               "毎月の練習を入れるか、sudo scripts/ops/restore-drill.sh を手で実行してください）")))
    elif drill.get("result") == "failed":
        alerts.append(Alert("error", "drill_failed", N_("前回の復旧の練習が失敗しました: {detail}"),
                            {"detail": drill["message"]}) if drill.get("message") else
                      Alert("error", "drill_failed", N_("前回の復旧の練習が失敗しました: "
                                                        "詳細はホストの journalctl -u docseed-restore-drill")))
    elif drill.get("result") == "skipped":
        alerts.append(Alert("warning", "drill_skipped", N_("空き容量が足りず、復旧の練習を飛ばしました: {detail}"),
                            {"detail": str(drill.get("message") or "")}))
    elif drill_age is not None and drill_age > DRILL_STALE_DAYS * 86400:
        alerts.append(Alert("warning", "drill_stale", N_("復旧の練習が {days} 日以上成功していません"),
                            {"days": DRILL_STALE_DAYS}))

    rp_saved = conn.execute("SELECT value FROM settings WHERE key = 'auth.rp_id'").fetchone()
    if rp_saved and rp_saved[0] != settings.rp_id \
            and conn.execute("SELECT 1 FROM webauthn_credentials LIMIT 1").fetchone():
        alerts.append(Alert("warning", "rp_id_changed",
                            N_("パスキーの RP ID が {old} から {new} に変わったため、"
                               "登録済みのパスキーは使えません。DOCSEED_HOST / DOCSEED_RP_ID を戻すか、各ユーザーが"
                               "パスキーを登録し直してください"),
                            {"old": rp_saved[0], "new": settings.rp_id or "-"}))

    if settings.webclip_enabled:
        fb = conn.execute("SELECT value FROM settings WHERE key = 'fetcher.heartbeat'").fetchone()
        fetcher_age = _age(_parse(fb[0]) if fb else None, now)
        waiting = conn.execute("SELECT min(created_at) FROM jobs WHERE type = 'fetch_url' AND status = 'pending'"
                               ).fetchone()[0]
        waiting_age = _age(_parse(waiting), now)
        if waiting_age is not None and waiting_age > 600 and (fetcher_age is None or fetcher_age > WORKER_STALE_SECS):
            alerts.append(Alert("warning", "fetcher_stale", N_("URL の取り込み（fetcher）が動いていません。.env の"
                                                               " COMPOSE_PROFILES=webclip を確認し、docker compose"
                                                               " up -d してください")))

    from .tlscheck import alerts as tls_alerts
    from .tlscheck import stored_status as tls_status
    tls = tls_status(conn)
    alerts.extend(Alert(level, code, msg) for level, code, msg in tls_alerts(tls))

    temp = cpu_temp()
    if temp is not None and temp >= settings.thermal_pause_c:
        alerts.append(Alert("warning", "thermal", N_("CPU の温度が {temp}℃ です。文字の読み取りを一時停止しています"),
                            {"temp": f"{temp:.0f}"}))
    elif temp is not None and temp >= settings.thermal_warn_c:
        alerts.append(Alert("warning", "thermal_high",
                            N_("CPU の温度が {temp}℃ と高めです（{pause}℃ で文字の読み取りを"
                               "一時停止します）。冷却ファンと設置場所を確認してください"),
                            {"temp": f"{temp:.0f}", "pause": f"{settings.thermal_pause_c:.0f}"}))

    mem = memory()
    if mem and mem["total"] and mem["available"] / mem["total"] * 100 < settings.memory_warn_pct:
        alerts.append(Alert("warning", "memory_low", N_("使えるメモリが {pct}%（{mb} MB）しかありません。"
                                                       "処理が遅くなったり、止まったりすることがあります"),
                            {"pct": f"{mem['available'] / mem['total'] * 100:.0f}",
                             "mb": mem["available"] // (1024 * 1024)}))

    def iso(t: dt.datetime | None) -> str | None:
        return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None

    return {
        "alerts": [asdict(a) for a in alerts],
        "worker": {"alive": worker_alive, "last_seen": iso(worker_seen)},
        "jobs": {k: jobs.get(k, 0) for k in ("pending", "running", "failed", "done")},
        "disk": {"total": disk.total, "free": disk.free, "free_ratio": round(ratio, 4)},
        "cpu_temp_c": temp,
        "tls": tls,
        "backup": {"db_snapshot": snap.name if snap else None, "db_snapshot_at": iso(snap_time),
                   "restic": restic, "restic_at": iso(restic_time),
                   "drill": drill, "drill_ok_at": iso(drill_ok_at)},
    }


def system_info(conn: sqlite3.Connection, settings: Settings) -> dict:
    """機械と docseed の状態（GET /api/v1/admin/system と管理画面の「システム」）。"""
    from . import __version__
    from .db import schema_version
    disk = shutil.disk_usage(settings.data_dir)
    load = load_average()
    c = conn.execute(
        "SELECT (SELECT count(*) FROM documents WHERE deleted_at IS NULL) AS documents,"
        " (SELECT count(*) FROM documents WHERE deleted_at IS NOT NULL) AS documents_trash,"
        " (SELECT count(*) FROM document_versions) AS versions,"
        " (SELECT count(*) FROM folders WHERE deleted_at IS NULL AND parent_id IS NOT NULL) AS folders,"
        " (SELECT count(*) FROM users WHERE is_active = 1) AS users,"
        " (SELECT count(*) FROM blobs) AS blobs,"
        " (SELECT coalesce(sum(size_bytes), 0) FROM blobs) AS blob_bytes,"
        " (SELECT coalesce(sum(b.size_bytes), 0) FROM blobs b WHERE b.id IN (SELECT blob_id FROM document_versions)"
        "   AND b.id NOT IN (SELECT blob_id FROM documents)) AS version_only_bytes,"
        " (SELECT count(*) FROM pages) AS pages,"
        " (SELECT count(*) FROM chunks) AS chunks").fetchone()
    db_bytes = sum(p.stat().st_size for p in settings.db_path.parent.glob(settings.db_path.name + "*") if p.is_file())
    return {
        "version": __version__,
        "schema_version": schema_version(conn),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "uptime_s": uptime_s(),
        "cpu": {"count": os.cpu_count(), "load": list(load) if load else None, "temp_c": cpu_temp(),
                "temp_warn_c": settings.thermal_warn_c, "temp_pause_c": settings.thermal_pause_c},
        "memory": memory(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                 "free_ratio": round(disk.free / disk.total, 4) if disk.total else None},
        "db_bytes": db_bytes,
        "counts": {k: c[k] for k in ("documents", "documents_trash", "versions", "folders", "users", "blobs",
                                     "pages", "chunks")},
        "storage": {"blob_bytes": c["blob_bytes"], "version_only_bytes": c["version_only_bytes"]},
        "models": {"ocr": settings.ocr_engine, "embedding": settings.embed_model,
                   "llm": settings.llm_model if settings.llm_enabled else None},
    }


def alert_count(conn: sqlite3.Connection, settings: Settings) -> int:
    """ヘッダの警告アイコンに出す数（info は数えない）。"""
    return sum(1 for a in status(conn, settings)["alerts"] if a["level"] != "info")
