"""要約に使う LLM の計測（docs/design/extensions.md 7.4）。Pi 5 でモデルを決めるために使う。

動いている llama-server に、手元の文書（テキスト）を 1 件ずつ送り、時間と要約・タグを記録する。
メモリは別の端末で `docker stats` か `free -m` を見る。

使い方（Pi 5 の上で。モデルは docseed fetch-models --llm <名前> で取得済みとする）:
    docker run --rm -p 8080:8080 -v /srv/docseed/models:/models:ro ghcr.io/ggml-org/llama.cpp:server \\
        -m /models/llm/sarashina2.2-1b-instruct-v0.1-Q4_K_M.gguf --host 0.0.0.0 -t 3 -c 4096 --parallel 1
    python scripts/poc/bench_llm.py --url http://localhost:8080 --label sarashina-q4 docs/*.txt

文書は「取り出したテキスト」を保存したもの（docseed の /api/v1/documents/{id}/text の pages[].text）でよい。
結果は results/<日時>-llm-<label>.md（目視で要約の誤りを数える）と .jsonl に出る。
決める条件: 1 件 90 秒以内、llm コンテナのメモリ 2GB 以内、要約の誤りが 20 件中 2 件以下。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from docseed.llm import LlmClient, LlmTimeout  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--label", required=True, help="結果のファイル名に付ける名前（モデルと量子化）")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--max-chars", type=int, default=3000)
    a = ap.parse_args()
    client = LlmClient(a.url, a.label, timeout_s=a.timeout, max_input_chars=a.max_chars)
    if not client.health():
        sys.exit(f"llama-server（{a.url}）に接続できません")
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md = [f"# LLM の計測: {a.label}（{stamp}）", "", "| 文書 | 秒 | 要約 | タグ | 誤り（目視で記入） |", "|---|---|---|---|---|"]
    times = []
    with (out_dir / f"{stamp}-llm-{a.label}.jsonl").open("w") as jl:
        for f in a.files:
            text = f.read_text(encoding="utf-8", errors="replace")
            try:
                s = client.summarize(f.stem, None, text)
            except LlmTimeout:
                md.append(f"| {f.name} | 時間切れ | | | |")
                jl.write(json.dumps({"file": f.name, "timeout": True}, ensure_ascii=False) + "\n")
                continue
            times.append(s.elapsed_ms / 1000)
            md.append(f"| {f.name} | {s.elapsed_ms / 1000:.1f} | {s.summary.replace('|', '／')} | {', '.join(s.tags)} | |")
            jl.write(json.dumps({"file": f.name, "seconds": s.elapsed_ms / 1000, "summary": s.summary,
                                 "tags": s.tags}, ensure_ascii=False) + "\n")
            print(f"{f.name}: {s.elapsed_ms / 1000:.1f} 秒")
    if times:
        md += ["", f"件数 {len(times)}、平均 {statistics.mean(times):.1f} 秒、最大 {max(times):.1f} 秒"]
    (out_dir / f"{stamp}-llm-{a.label}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"→ {out_dir}/{stamp}-llm-{a.label}.md")


if __name__ == "__main__":
    main()
