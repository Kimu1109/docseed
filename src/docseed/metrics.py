"""Prometheus 形式の指標（GET /api/v1/admin/metrics。docs/design/deployment.md 6.2）。

依存を増やさないよう、テキスト形式（text/plain; version=0.0.4）を自前で書く。DB の集計・monitor.status・system_info
を読むだけなので、取得のたびに少しの SELECT が走る（Prometheus の取得間隔は 30 秒〜1 分を想定）。
検索の速さだけは api の中で数える（プロセスを再起動すると 0 から数え直す。Prometheus の counter の扱い）。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading

from .config import Settings

SEARCH_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
JOB_WINDOW_H = 24          # ジョブの所要時間は直近 24 時間に終わったもので出す


class _Histogram:
    def __init__(self, buckets: tuple[float, ...]):
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.total = 0
        self.sum = 0.0
        self.lock = threading.Lock()

    def observe(self, v: float) -> None:
        with self.lock:
            self.total += 1
            self.sum += v
            for i, b in enumerate(self.buckets):
                if v <= b:
                    self.counts[i] += 1

    def snapshot(self) -> tuple[list[int], int, float]:
        with self.lock:
            return list(self.counts), self.total, self.sum


search_seconds: dict[str, _Histogram] = {}
_search_lock = threading.Lock()


def observe_search(mode: str, seconds: float) -> None:
    with _search_lock:
        h = search_seconds.setdefault(mode, _Histogram(SEARCH_BUCKETS))
    h.observe(seconds)


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------

def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _num(v: float | int) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


class _Out:
    def __init__(self):
        self.lines: list[str] = []
        self.declared: set[str] = set()

    def metric(self, name: str, kind: str, help_: str, value: float | int | None,
               labels: dict[str, object] | None = None) -> None:
        if name not in self.declared:
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
            self.declared.add(name)
        if value is None:
            return
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in (labels or {}).items())
        self.lines.append(f"{name}{{{lab}}} {_num(value)}" if lab else f"{name} {_num(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.UTC)).timestamp()


def render(conn: sqlite3.Connection, settings: Settings) -> str:
    from . import __version__
    from .monitor import status, system_info
    o = _Out()
    st = status(conn, settings)
    si = system_info(conn, settings)
    now = dt.datetime.now(dt.UTC)

    o.metric("docseed_info", "gauge", "docseed の版（値は常に 1）", 1,
             {"version": __version__, "schema": si["schema_version"]})
    if si["uptime_s"] is not None:
        o.metric("docseed_host_uptime_seconds", "gauge", "機械が起動してからの秒数", si["uptime_s"])

    # ---- 文書・索引 ----
    c = si["counts"]
    for state, key in (("active", "documents"), ("trash", "documents_trash")):
        o.metric("docseed_documents", "gauge", "文書の数（state: active / trash）", c[key], {"state": state})
    o.metric("docseed_document_versions", "gauge", "過去の版の数", c["versions"])
    o.metric("docseed_folders", "gauge", "フォルダの数（ゴミ箱とルートを除く）", c["folders"])
    o.metric("docseed_pages", "gauge", "読み取ったページの数", c["pages"])
    o.metric("docseed_chunks", "gauge", "検索の索引のチャンクの数", c["chunks"])
    o.metric("docseed_blob_bytes", "gauge", "保存している実体の合計の大きさ", si["storage"]["blob_bytes"])
    for s_, n in conn.execute("SELECT status, count(*) FROM blobs GROUP BY status"):
        o.metric("docseed_blobs", "gauge", "保存している実体の数（status: 処理の状態）", n, {"status": s_})

    # ---- ジョブ ----
    for (type_, s_), n in {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT type, status, count(*) FROM jobs GROUP BY type, status")}.items():
        o.metric("docseed_jobs", "gauge", "ジョブの数（type・status ごと）", n, {"type": type_, "status": s_})
    oldest = conn.execute("SELECT min(run_after) FROM jobs WHERE status = 'pending'").fetchone()[0]
    o.metric("docseed_jobs_oldest_pending_seconds", "gauge", "いちばん古い処理待ちのジョブが待っている秒数",
             max(0.0, now.timestamp() - _ts(oldest)) if _ts(oldest) else 0)
    since = (now - dt.timedelta(hours=JOB_WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for type_, n, avg, mx in conn.execute(
            "SELECT type, count(*), avg(julianday(finished_at) - julianday(locked_at)) * 86400,"
            " max(julianday(finished_at) - julianday(locked_at)) * 86400 FROM jobs"
            " WHERE status = 'done' AND finished_at >= ? AND locked_at IS NOT NULL GROUP BY type", (since,)):
        o.metric("docseed_job_duration_seconds_avg", "gauge",
                 f"直近 {JOB_WINDOW_H} 時間に終わったジョブの平均所要時間（OCR・埋め込みは process_blob）",
                 round(avg or 0.0, 3), {"type": type_})
        o.metric("docseed_job_duration_seconds_max", "gauge",
                 f"直近 {JOB_WINDOW_H} 時間に終わったジョブの最大所要時間", round(mx or 0.0, 3), {"type": type_})
        o.metric("docseed_jobs_done_recent", "gauge", f"直近 {JOB_WINDOW_H} 時間に終わったジョブの数", n,
                 {"type": type_})

    # ---- worker・警告 ----
    seen = _ts(st["worker"]["last_seen"])
    o.metric("docseed_worker_up", "gauge", "worker が 2 分以内に応答したか", st["worker"]["alive"])
    if seen:
        o.metric("docseed_worker_last_seen_timestamp_seconds", "gauge", "worker の最終応答（UNIX 時刻）", seen)
    levels = {"error": 0, "warning": 0, "info": 0}
    for a in st["alerts"]:
        levels[a["level"]] = levels.get(a["level"], 0) + 1
    for lv, n in levels.items():
        o.metric("docseed_alerts", "gauge", "管理画面の「確認が必要な項目」の数（level ごと）", n, {"level": lv})
    for a in st["alerts"]:
        o.metric("docseed_alert", "gauge", "出ている警告（code・level。値は常に 1）", 1,
                 {"code": a["code"], "level": a["level"]})

    # ---- 機械 ----
    cpu, mem, disk = si["cpu"], si["memory"], si["disk"]
    if cpu["temp_c"] is not None:
        o.metric("docseed_cpu_temperature_celsius", "gauge", "CPU の温度", cpu["temp_c"])
    if cpu["load"]:
        for period, v in zip(("1m", "5m", "15m"), cpu["load"], strict=True):
            o.metric("docseed_load_average", "gauge", "負荷の平均", round(v, 3), {"period": period})
    if mem:
        o.metric("docseed_memory_total_bytes", "gauge", "メモリの合計", mem["total"])
        o.metric("docseed_memory_available_bytes", "gauge", "使えるメモリ", mem["available"])
    o.metric("docseed_disk_total_bytes", "gauge", "データの SSD の大きさ", disk["total"])
    o.metric("docseed_disk_free_bytes", "gauge", "データの SSD の空き", disk["free"])
    o.metric("docseed_db_bytes", "gauge", "データベースの大きさ（WAL を含む）", si["db_bytes"])

    # ---- バックアップ・証明書 ----
    b = st["backup"]
    for kind, ts_ in (("db_snapshot", b["db_snapshot_at"]),
                      ("restic", b["restic_at"] if b["restic"] and b["restic"].get("ok") else None)):
        if _ts(ts_):
            o.metric("docseed_backup_last_success_timestamp_seconds", "gauge",
                     "最後にバックアップが成功した時刻（UNIX 時刻。kind: db_snapshot / restic）", _ts(ts_),
                     {"kind": kind})
    tls = st.get("tls")
    if tls and tls.get("not_after"):
        o.metric("docseed_tls_cert_expiry_timestamp_seconds", "gauge", "HTTPS の証明書の期限（UNIX 時刻）",
                 _ts(tls["not_after"]))
        o.metric("docseed_tls_cert_valid", "gauge", "HTTPS の証明書を確認できたか", bool(tls.get("ok")))

    # ---- ユーザー・容量 ----
    from .limits import status as quota_status
    for uid, username, active in conn.execute("SELECT id, username, is_active FROM users ORDER BY id"):
        q = quota_status(conn, uid)
        o.metric("docseed_user_usage_bytes", "gauge", "ユーザーの使用量（今のファイル・ゴミ箱・過去の版）",
                 q["total"], {"username": username})
        if q["quota"]:
            o.metric("docseed_user_quota_bytes", "gauge", "ユーザーの容量の上限（上限なしなら出さない）", q["quota"],
                     {"username": username})
        o.metric("docseed_user_active", "gauge", "ユーザーが有効か", bool(active), {"username": username})

    # ---- 通知 ----
    for s_, n in conn.execute("SELECT status, count(*) FROM notifications GROUP BY status"):
        o.metric("docseed_notifications", "gauge", "通知の数（status ごと。送った記録は 30 日で消す）", n,
                 {"status": s_})

    # ---- 検索の速さ（api の中で数える） ----
    o.metric("docseed_search_duration_seconds", "histogram",
             "検索にかかった時間（mode: hybrid / semantic / keyword / filter）", None)
    with _search_lock:
        hists = dict(search_seconds)
    for mode, h in sorted(hists.items()):
        counts, total, sum_ = h.snapshot()
        for b_, n in zip(h.buckets, counts, strict=True):
            o.lines.append(f'docseed_search_duration_seconds_bucket{{mode="{_esc(mode)}",le="{b_}"}} {n}')
        o.lines.append(f'docseed_search_duration_seconds_bucket{{mode="{_esc(mode)}",le="+Inf"}} {total}')
        o.lines.append(f'docseed_search_duration_seconds_sum{{mode="{_esc(mode)}"}} {sum_!r}')
        o.lines.append(f'docseed_search_duration_seconds_count{{mode="{_esc(mode)}"}} {total}')
    return o.text()
