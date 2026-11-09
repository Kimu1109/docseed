"""システムの状態と警告（管理画面・ヘッダの警告・/api/v1/admin/status）。docs/design/deployment.md 6 章。"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .backup import latest_snapshot, restic_status
from .config import Settings

WORKER_STALE_SECS = 120          # worker の応答がこれ以上無ければ警告
DISK_WARN_RATIO = 0.10           # 空きが 10% を切ったら警告（5% でアップロードを拒否: storage.MIN_FREE_RATIO）
DISK_ERROR_RATIO = 0.05
BACKUP_STALE_HOURS = 36          # 日次のバックアップが 1 回抜けたら気づけるように
THERMAL_PATHS = [Path("/host/thermal/thermal_zone0/temp"), Path("/sys/class/thermal/thermal_zone0/temp")]


@dataclass
class Alert:
    level: str      # error / warning / info
    code: str
    message: str


def cpu_temp() -> float | None:
    for p in THERMAL_PATHS:
        try:
            return int(p.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
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
        alerts.append(Alert("warning", "worker_never", "文字の読み取り（worker）がまだ一度も動いていません"))
    elif not worker_alive:
        alerts.append(Alert("error", "worker_stale", "文字の読み取り（worker）から 2 分以上応答がありません"))

    jobs = dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
    if jobs.get("failed"):
        alerts.append(Alert("warning", "jobs_failed", f"失敗したジョブが {jobs['failed']} 件あります"))

    disk = shutil.disk_usage(settings.data_dir)
    ratio = disk.free / disk.total if disk.total else 1.0
    if ratio < DISK_ERROR_RATIO:
        alerts.append(Alert("error", "disk_full", "SSD の空きが 5% を下回っています。アップロードを受け付けていません"))
    elif ratio < DISK_WARN_RATIO:
        alerts.append(Alert("warning", "disk_low", "SSD の空きが 10% を下回っています"))

    backup_dir = settings.data_dir / "backup"
    snap = latest_snapshot(backup_dir)
    snap_time = dt.datetime.fromtimestamp(snap.stat().st_mtime, dt.UTC) if snap else None
    snap_age = _age(snap_time, now)
    if snap is None:
        alerts.append(Alert("warning", "db_backup_none", "データベースのスナップショットがまだありません"))
    elif snap_age is not None and snap_age > BACKUP_STALE_HOURS * 3600:
        alerts.append(Alert("warning", "db_backup_stale",
                            "データベースのスナップショットが 36 時間以上作られていません"))

    restic = restic_status(backup_dir)
    restic_time = _parse(restic.get("time")) if restic else None
    restic_age = _age(restic_time, now)
    if restic is None:
        alerts.append(Alert("info", "restic_none", "別の機器へのバックアップ（restic）の記録がありません"))
    elif not restic.get("ok"):
        alerts.append(Alert("error", "restic_failed", "前回のバックアップ（restic）が失敗しました: "
                            + str(restic.get("message") or "詳細はホストの journalctl -u docseed-backup")))
    elif restic_age is not None and restic_age > BACKUP_STALE_HOURS * 3600:
        alerts.append(Alert("warning", "restic_stale",
                            "別の機器へのバックアップ（restic）が 36 時間以上成功していません"))

    rp_saved = conn.execute("SELECT value FROM settings WHERE key = 'auth.rp_id'").fetchone()
    if rp_saved and rp_saved[0] != settings.rp_id \
            and conn.execute("SELECT 1 FROM webauthn_credentials LIMIT 1").fetchone():
        alerts.append(Alert("warning", "rp_id_changed",
                            f"パスキーの RP ID が {rp_saved[0]} から {settings.rp_id or '（なし）'} に変わったため、"
                            "登録済みのパスキーは使えません。DOCSEED_HOST / DOCSEED_RP_ID を戻すか、各ユーザーが"
                            "パスキーを登録し直してください"))

    from .tlscheck import alerts as tls_alerts
    from .tlscheck import stored_status as tls_status
    tls = tls_status(conn)
    alerts.extend(Alert(level, code, msg) for level, code, msg in tls_alerts(tls))

    temp = cpu_temp()
    if temp is not None and temp >= settings.thermal_pause_c:
        alerts.append(Alert("warning", "thermal", f"CPU の温度が {temp:.0f}℃ です。文字の読み取りを一時停止しています"))

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
                   "restic": restic, "restic_at": iso(restic_time)},
    }


def alert_count(conn: sqlite3.Connection, settings: Settings) -> int:
    """ヘッダの警告アイコンに出す数（info は数えない）。"""
    return sum(1 for a in status(conn, settings)["alerts"] if a["level"] != "info")
