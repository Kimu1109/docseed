"""Phase 0 計測スクリプトの共通処理。

- 実行環境の情報（機種・RAM・CPU 温度・スロットリング）
- 時間計測、ピークメモリ（RSS）
- 結果の JSONL 書き出し、Markdown の表の出力
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

RESULTS_DIR = Path(__file__).resolve().parent / "results"
THERMAL_PATHS = [
    Path("/sys/class/thermal/thermal_zone0/temp"),
    Path("/host/thermal/thermal_zone0/temp"),
]


# ---------------------------------------------------------------------------
# 実行環境
# ---------------------------------------------------------------------------

def cpu_temp_c() -> float | None:
    """SoC の温度（℃）。取れなければ None。"""
    for p in THERMAL_PATHS:
        try:
            return int(p.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def throttled() -> str | None:
    """`vcgencmd get_throttled` の値（例: '0x0'）。Raspberry Pi 以外では None。

    ビットの意味: 0=低電圧, 1=周波数上限, 2=スロットリング中, 3=温度による制限
    （16 以降は「起動後に一度でも発生した」）。
    """
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return out.split("=", 1)[1] if "=" in out else out
    except (OSError, subprocess.SubprocessError):
        return None


def cpu_freq_mhz() -> float | None:
    p = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    try:
        return int(p.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def peak_rss_mb() -> float:
    """このプロセスのピーク RSS（MB）。Linux の ru_maxrss は KB 単位。"""
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1024.0 if sys.platform.startswith("linux") else kb / 1024.0 / 1024.0


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    model = Path("/proc/device-tree/model")
    if model.exists():
        info["model"] = model.read_text(errors="ignore").strip("\x00\n ")
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                info["mem_total_mb"] = int(line.split()[1]) // 1024
                break
    except OSError:
        pass
    info["cpu_temp_c"] = cpu_temp_c()
    info["throttled"] = throttled()
    return info


def wait_for_cool(max_temp_c: float, timeout_s: float = 600) -> None:
    """CPU 温度が max_temp_c 以下になるまで待つ（計測条件をそろえるため）。"""
    t = cpu_temp_c()
    if t is None or t <= max_temp_c:
        return
    print(f"  CPU {t:.1f}℃ > {max_temp_c}℃ のため冷えるまで待機します…", file=sys.stderr)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(5)
        t = cpu_temp_c()
        if t is None or t <= max_temp_c:
            return
    print("  待機がタイムアウトしました。そのまま続行します。", file=sys.stderr)


# ---------------------------------------------------------------------------
# 計測
# ---------------------------------------------------------------------------

class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self.start


def summarize(values: Iterable[float]) -> dict[str, float]:
    v = sorted(values)
    if not v:
        return {}
    def pct(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)
    return {
        "n": len(v),
        "mean": statistics.fmean(v),
        "p50": pct(0.5),
        "p95": pct(0.95),
        "min": v[0],
        "max": v[-1],
    }


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def results_path(name: str, out_dir: Path | None = None) -> Path:
    d = out_dir or RESULTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return d / f"{stamp}-{name}.jsonl"


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def md_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """rows を Markdown の表にする。columns は (キー, 見出し) のリスト。"""
    def fmt(x: Any) -> str:
        if x is None:
            return "-"
        if isinstance(x, float):
            return f"{x:.3f}" if abs(x) < 10 else f"{x:.1f}"
        return str(x)
    head = "| " + " | ".join(h for _, h in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(fmt(r.get(k)) for k, _ in columns) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def run_isolated(argv: list[str]) -> list[dict[str, Any]]:
    """自分自身を別プロセスで起動し、標準出力の JSON 行を集める。

    モデルごとのピークメモリを正しく測るため、設定ごとにプロセスを分ける。
    """
    proc = subprocess.run(
        [sys.executable, *argv], stdout=subprocess.PIPE, text=True
    )
    if proc.returncode != 0:
        print(f"  ! 失敗 (exit {proc.returncode}): {' '.join(argv)}", file=sys.stderr)
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out
