"""要約に使う LLM の計測と比較（docs/design/extensions.md 7.4）。

ふつうは scripts/bench_llm.sh から使う（モデルごとの llama-server の起動・メモリの記録・比較表までを 1 回で行う）。
このスクリプトには 2 つのコマンドがある:

    run    動いている llama-server に文書を 1 件ずつ送り、時間と要約・タグを <out>/<label>.jsonl に記録する
    report <dir> の *.jsonl（と bench_llm.sh が記録したメモリ <label>.mem）から、比較表 report.md を作る

手で使う例（llama-server を自分で起動した場合）:
    python scripts/bench_llm.py run --url http://localhost:8081 --label sarashina-q4 --out out/ docs/
    python scripts/bench_llm.py report out/

文書は .txt のファイルか、それを入れたディレクトリ。docseed export-texts の書き出したもの（1 行目が「# タイトル」）
でも、手で用意したテキストでもよい。1 件でエラーになっても止めずに次の文書へ進み、理由を残す。
決める条件: 1 件 90 秒以内、llm コンテナのメモリ 2GB 以内、要約の誤りが 20 件中 2 件以下（目視）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docseed.llm import (  # noqa: E402
    MIN_INPUT_CHARS,
    LlmClient,
    LlmError,
    LlmTimeout,
    LlmUnavailable,
    read_exported,
    too_short,
)

LIMIT_SECONDS = 90
LIMIT_MEM = 2 * 1024 ** 3


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def collect_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.glob("*.txt")) if p.is_dir() else [p]
    return out


def wait_ready(client: LlmClient, seconds: int) -> bool:
    """モデルの読み込みが終わる（/health が 200 になる）まで待つ。"""
    end = time.monotonic() + seconds
    while True:
        if client.health():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(2)


def bench_one(client: LlmClient, f: Path) -> dict:
    title, desc, text = read_exported(f)
    rec: dict = {"type": "doc", "file": f.name, "title": title}
    if too_short(text):
        # docseed も、本文がこれより短い文書は要約しない
        return rec | {"skipped": f"本文が {MIN_INPUT_CHARS} 字未満"}
    try:
        s = client.summarize(title, desc, text)
    except LlmTimeout:
        return rec | {"timeout": True}
    except (LlmError, LlmUnavailable) as e:
        return rec | {"error": str(e)}
    return rec | {"seconds": s.elapsed_ms / 1000, "summary": s.summary, "tags": s.tags, "attempts": s.attempts}


def cmd_run(a: argparse.Namespace) -> None:
    files = collect_files(a.files)
    if not files:
        sys.exit("文書（.txt）がありません")
    client = LlmClient(a.url, a.model or a.label, timeout_s=a.timeout, max_input_chars=a.max_chars)
    print(f"[{a.label}] llama-server（{a.url}）の準備を待っています")
    if not wait_ready(client, a.wait):
        sys.exit(f"llama-server（{a.url}）に接続できません")
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"{a.label}.jsonl"
    with path.open("w", encoding="utf-8") as jl:
        meta = {"type": "meta", "label": a.label, "model": a.model or a.label, "url": a.url,
                "timeout": a.timeout, "max_chars": a.max_chars,
                "started_at": dt.datetime.now().isoformat(timespec="seconds")}
        jl.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for i, f in enumerate(files, 1):
            rec = bench_one(client, f)
            jl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jl.flush()
            print(f"[{a.label}] {i}/{len(files)} {f.name}: {describe(rec, a.timeout)}")
    print(f"→ {path}")


def describe(rec: dict, timeout: int) -> str:
    if "skipped" in rec:
        return f"飛ばした（{rec['skipped']}）"
    if rec.get("timeout"):
        return f"時間切れ（{timeout} 秒）"
    if "error" in rec:
        return f"エラー: {rec['error']}"
    return f"{rec['seconds']:.1f} 秒" + ("（日本語でなかったので頼み直し）" if rec.get("attempts", 1) > 1 else "")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3, "gib": 1024 ** 3}


def parse_mem(s: str) -> int | None:
    """docker stats の MemUsage（例「812.3MiB / 2GiB」）の使用量をバイトにする。"""
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", s.split("/")[0])
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def read_mem(path: Path) -> tuple[int | None, bool]:
    """(最大のメモリ, メモリ不足で止まったか)。"""
    if not path.exists():
        return None, False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    vals = [v for v in (parse_mem(x) for x in lines) if v is not None]
    return (max(vals) if vals else None), any(x.strip() == "OOMKilled" for x in lines)


def fmt_mem(b: int | None) -> str:
    return "—" if b is None else f"{b / 1024 ** 3:.2f} GB"


def load_runs(d: Path) -> list[dict]:
    runs = []
    for p in sorted(d.glob("*.jsonl")):
        meta: dict = {"label": p.stem, "model": p.stem}
        docs: dict[str, dict] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("type") == "meta":
                meta |= r
            else:
                docs[r["file"]] = r
        peak, oom = read_mem(d / f"{p.stem}.mem")
        times = [r["seconds"] for r in docs.values() if "seconds" in r]
        runs.append(meta | {
            "docs": docs, "times": times, "peak_mem": peak, "oom": oom,
            "failed": sum(1 for r in docs.values() if r.get("timeout") or "error" in r),
            "retried": sum(1 for r in docs.values() if r.get("attempts", 1) > 1),
        })
    return runs


def verdict(run: dict) -> str:
    bad = []
    if not run["times"]:
        return "✗ 要約できた文書なし"
    if max(run["times"]) > LIMIT_SECONDS:
        bad.append(f"{LIMIT_SECONDS} 秒超")
    if run["failed"]:
        bad.append("失敗あり")
    if run["oom"]:
        bad.append("メモリ不足で停止")
    elif run["peak_mem"] is not None and run["peak_mem"] > LIMIT_MEM:
        bad.append("2GB 超")
    return "✓" if not bad else "✗ " + "・".join(bad)


def cell(s: str) -> str:
    return s.replace("|", "／").replace("\n", " ")


def doc_cell(r: dict | None) -> tuple[str, str, str]:
    """(秒, 要約, タグ)"""
    if r is None:
        return "—", "（未計測）", ""
    if "skipped" in r:
        return "飛ばした", cell(r["skipped"]), ""
    if r.get("timeout"):
        return "時間切れ", "", ""
    if "error" in r:
        return "エラー", cell(r["error"])[:200], ""
    sec = f"{r['seconds']:.1f}" + ("（頼み直し）" if r.get("attempts", 1) > 1 else "")
    return sec, cell(r["summary"]), cell(", ".join(r["tags"]))


def build_report(d: Path) -> str:
    runs = load_runs(d)
    if not runs:
        raise SystemExit(f"{d} に計測の記録（*.jsonl）がありません")
    md = [f"# 要約に使う LLM の比較（{d.name}）", "",
          f"条件: 1 件 {LIMIT_SECONDS} 秒以内・メモリ 2GB 以内・要約の誤り（本文に無いことを書く）が 20 件中 2 件以下。"
          "誤りは下の文書ごとの表に目視で印を付けて数え、この表の「誤り」に書き込んでください。", "",
          "| モデル | 件数 | 平均 秒 | 中央値 秒 | 最大 秒 | 失敗 | 頼み直し | 最大メモリ | 速さ・メモリ"
          " | 誤り（目視） |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        t = r["times"]
        avg, med, mx = (f"{statistics.mean(t):.1f}", f"{statistics.median(t):.1f}", f"{max(t):.1f}") if t \
            else ("—", "—", "—")
        md.append(f"| {cell(r['label'])} | {len(t)} | {avg} | {med} | {mx} | {r['failed']} | {r['retried']}"
                  f" | {fmt_mem(r['peak_mem'])}{'（停止）' if r['oom'] else ''} | {verdict(r)} | |")
    models = "・".join(f"`{r['model']}`" for r in runs)
    md += ["", f"モデル: {models}", "", "## 文書ごとの要約", ""]
    files = sorted({f for r in runs for f in r["docs"]})
    for f in files:
        title = next((r["docs"][f].get("title") for r in runs if f in r["docs"]), f)
        md += [f"### {cell(title)}", "", f"`{f}`", "",
               "| モデル | 秒 | 要約 | タグ | 誤り |", "|---|---|---|---|---|"]
        for r in runs:
            sec, summary, tags = doc_cell(r["docs"].get(f))
            md.append(f"| {cell(r['label'])} | {sec} | {summary} | {tags} | |")
        md.append("")
    return "\n".join(md)


def cmd_report(a: argparse.Namespace) -> None:
    text = build_report(a.dir)
    out = a.dir / "report.md"
    out.write_text(text, encoding="utf-8")
    # 画面には全体の表だけを出す
    head = text.split("\n## ", 1)[0]
    print(head)
    shown = (a.display_dir or a.dir) / "report.md"
    print(f"\n→ {shown}（文書ごとの要約の比較もここに）")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="1 つのモデルで文書を要約して記録する")
    r.add_argument("files", nargs="+", type=Path, help=".txt のファイルか、それを入れたディレクトリ")
    r.add_argument("--url", default="http://localhost:8081")
    r.add_argument("--label", required=True, help="比較表に出す名前（モデルと量子化）")
    r.add_argument("--model", help="GGUF のファイル名（記録用）")
    r.add_argument("--out", type=Path, required=True, help="記録を置くディレクトリ")
    r.add_argument("--timeout", type=int, default=180, help="1 件の上限（秒）")
    r.add_argument("--max-chars", type=int, default=3000)
    r.add_argument("--wait", type=int, default=300, help="llama-server の準備を待つ上限（秒）")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report", help="記録から比較表（report.md）を作る")
    p.add_argument("dir", type=Path)
    p.add_argument("--display-dir", type=Path, help="表示に使うパス（コンテナの外から見た dir）")
    p.set_defaults(func=cmd_report)
    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
