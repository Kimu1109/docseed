"""LLM の比較（scripts/bench_llm.sh・scripts/bench_llm.py・docseed export-texts）。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from docseed import llm
from docseed.cli import main as cli_main
from tests.test_suggest import FakeLlm, fake_llm, library, worker  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _bench():
    spec = importlib.util.spec_from_file_location("bench_llm", ROOT / "scripts" / "bench_llm.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bench = _bench()


def test_export_texts(library, worker, settings, conn, tmp_path):  # noqa: F811
    from tests.test_pipeline import run_all
    from tests.test_suggest import put
    a, inbox = library["a"], library["inbox"]
    # ライブラリの文書は本文が 100 字未満なので書き出さない。短すぎる文書・ゴミ箱の文書・同じ内容のファイルも除く
    for i in range(3):
        put(a, inbox, f"long{i}.txt", f"長い文書 {i} の本文です。" * 20)
    gone = put(a, inbox, "gone.txt", "削除する文書の本文です。" * 20)
    put(a, library["travel"], "dup.txt", "長い文書 0 の本文です。" * 20)
    run_all(worker)
    assert a.delete(f"/api/v1/documents/{gone}").status_code in (200, 204)

    files = llm.export_texts(conn, tmp_path / "docs", limit=30)
    names = sorted(f.name for f in files)
    assert [n[6:] for n in names] == ["long1.txt", "long2.txt", "long0.txt"]   # dup.txt は long0.txt と同じ内容
    title, desc, text = llm.read_exported(next(f for f in files if "long1" in f.name))
    assert (title, desc) == ("long1.txt", None) and text.startswith("長い文書 1 の本文です。")
    assert len(llm.export_texts(conn, tmp_path / "two", limit=2)) == 2

    # CLI（ユーザーで絞る。bob の文書は無い）
    os.environ["DOCSEED_DATA_DIR"] = str(settings.data_dir)
    os.environ["DOCSEED_SECRET_KEY"] = "x" * 40
    try:
        cli_main(["export-texts", "--out", str(tmp_path / "cli"), "--username", "alice", "--limit", "2"])
        assert len(list((tmp_path / "cli").glob("*.txt"))) == 2
        with pytest.raises(SystemExit):
            cli_main(["export-texts", "--out", str(tmp_path / "bob"), "--username", "bob"])
    finally:
        del os.environ["DOCSEED_DATA_DIR"], os.environ["DOCSEED_SECRET_KEY"]


def test_read_exported_plain_text(tmp_path):
    f = tmp_path / "memo.txt"
    f.write_text("見出しのない本文", encoding="utf-8")
    assert llm.read_exported(f) == ("memo", None, "見出しのない本文")
    f.write_text("# 記事\n# 説明文\n本文\n# 本文の中の見出し", encoding="utf-8")
    assert llm.read_exported(f) == ("記事", "説明文", "本文\n# 本文の中の見出し")


def test_parse_mem():
    assert bench.parse_mem("812.5MiB / 2GiB") == int(812.5 * 1024 ** 2)
    assert bench.parse_mem("1.5GiB / 2GiB") == int(1.5 * 1024 ** 3)
    assert bench.parse_mem("0B / 0B") == 0
    assert bench.parse_mem("--") is None


def test_run_and_report(tmp_path, monkeypatch, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("# 請求書 A\n" + "今月の請求の内容です。" * 20, encoding="utf-8")
    (docs / "b.txt").write_text("# 短い\n短い本文", encoding="utf-8")
    out = tmp_path / "out"

    fake = FakeLlm()
    transport = httpx.MockTransport(lambda r: httpx.Response(200) if r.url.path == "/health" else fake.handler(r))
    real = llm.LlmClient.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw, transport=transport)
    monkeypatch.setattr(llm.LlmClient, "__init__", init)

    bench.main(["run", str(docs), "--label", "model-a", "--model", "a.gguf", "--out", str(out), "--wait", "1"])
    fake.outputs = [{"要約": "別のモデルの要約です。", "タグ": ["経理"]}]
    bench.main(["run", str(docs / "a.txt"), "--label", "model-b", "--out", str(out), "--wait", "1"])
    (out / "model-a.mem").write_text("700MiB / 2GiB\n1.2GiB / 2GiB\n", encoding="utf-8")
    (out / "model-b.mem").write_text("1.9GiB / 2GiB\nOOMKilled\n", encoding="utf-8")

    recs = [json.loads(x) for x in (out / "model-a.jsonl").read_text().splitlines()]
    assert recs[0]["type"] == "meta" and recs[0]["model"] == "a.gguf"
    assert recs[1]["title"] == "請求書 A" and recs[1]["summary"] == "請求書の内容をまとめた要約です。"
    assert "skipped" in recs[2]
    # 送ったのはタイトルと本文（1 行目の「# 」は本文に入れない）
    assert "この文書の題名は「請求書 A」です。" in fake.templates[0]["messages"][1]["content"]

    capsys.readouterr()
    bench.main(["report", str(out), "--display-dir", "/srv/docseed/bench/x"])
    printed = capsys.readouterr().out
    assert "/srv/docseed/bench/x/report.md" in printed and "## 文書ごと" not in printed
    md = (out / "report.md").read_text()
    row_a = next(x for x in md.splitlines() if x.startswith("| model-a | 1 |"))
    assert "1.20 GB" in row_a and "✓" in row_a
    row_b = next(x for x in md.splitlines() if x.startswith("| model-b | 1 |"))
    assert "メモリ不足で停止" in row_b
    assert "### 請求書 A" in md and "別のモデルの要約です。" in md and "経理" in md
    assert "| model-b | — | （未計測） |" in md      # b.txt は model-b では測っていない


def test_report_without_runs(tmp_path):
    with pytest.raises(SystemExit):
        bench.main(["report", str(tmp_path)])


# ---------------------------------------------------------------------------
# scripts/bench_llm.sh（docker を偽物に差し替えて、流れだけを確かめる）
# ---------------------------------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "image inspect") exit 0 ;;
  "compose --project-directory") shift 3 ;;
  "stats --no-stream") echo "1.1GiB / 2GiB"; exit 0 ;;
  "inspect -f") echo false; exit 0 ;;
  "rm -f") exit 0 ;;
esac
case "$1" in
  ps) printf 'api\nworker\nllm\n'; exit 0 ;;
  stop|start) exit 0 ;;
  exec)   # exec -T api docseed export-texts --out /srv/docseed/<rel> ...
    if [ "$5" = export-texts ]; then
      d="$FAKE_DATA/${7#/srv/docseed/}"; mkdir -p "$d"
      printf '# 文書 1\n%s' "$(printf '本文%.0s' $(seq 80))" > "$d/00001-doc1.txt"
    fi
    exit 0 ;;
  run)
    out=""; for a in "$@"; do case "$a" in *:/out) out=${a%:/out} ;; esac; done
    if [ "$2" = -d ]; then exit 0; fi
    if printf '%s\n' "$@" | grep -qx report; then
      exec "$FAKE_PY" "$FAKE_REPO/scripts/bench_llm.py" report "$out" --display-dir "$out"
    fi
    label=$(printf '%s\n' "$@" | grep -A1 -x -- --label | tail -n 1)
    doc='{"type":"doc","file":"00001-doc1.txt","title":"文書 1","seconds":12.5,"summary":"要約です。","tags":["経理"]}'
    printf '{"type":"meta","label":"%s","model":"%s.gguf"}\n%s\n' "$label" "$label" "$doc" > "$out/$label.jsonl"
    exit 0 ;;
esac
exit 0
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash が必要")
def test_bench_script_flow(tmp_path):
    data = tmp_path / "data"
    (data / "models" / "llm").mkdir(parents=True)
    for n in ("alpha.gguf", "beta.gguf"):
        (data / "models" / "llm" / n).write_bytes(b"x")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    env = os.environ | {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(log), "FAKE_DATA": str(data),
                        "FAKE_REPO": str(ROOT), "FAKE_PY": sys.executable,
                        "DOCSEED_DATA_DIR": str(data)}
    # .env は読むだけ（テストでは DOCSEED_DATA_DIR を .env から取れないので、偽の .env を使う作業コピーを作る）
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "bench_llm.sh", repo / "scripts")
    (repo / ".env").write_text(f"DOCSEED_DATA_DIR={data}\nDOCSEED_TRUSTED_NETS=private_ranges 100.64.0.0/10\n"
                               "DOCSEED_LLM_THREADS=2\n")
    out = tmp_path / "result"
    r = subprocess.run(["bash", str(repo / "scripts" / "bench_llm.sh"), "--out", str(out)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    # 文書の書き出し → worker・llm を止める → モデルごとに起動（.env のスレッド数）→ 起動し直す
    assert any("export-texts --out /srv/docseed/bench/docs --limit 30" in c for c in calls)
    assert any(c.endswith("stop worker llm") for c in calls)
    started = [c for c in calls if c.startswith("run -d")]
    assert len(started) == 2 and "/models/llm/alpha.gguf" in started[0] and "-t 2 " in started[0]
    assert "--memory 2g" in started[0] and "127.0.0.1:8081:8080" in started[0]
    assert calls.index(next(c for c in calls if c.endswith("start worker llm"))) > \
        calls.index(next(c for c in calls if "report" in c))
    md = (out / "report.md").read_text()
    assert "| alpha | 1 | 12.5 |" in md and "| beta | 1 |" in md and "1.10 GB" in md
    assert (out / "alpha.mem").read_text().startswith("1.1GiB")
    assert "report.md" in r.stdout

    # 2 回目は文書を書き出し直さない。--keep-services なら止めない。名前で 1 つだけ選べる
    log.write_text("")
    r = subprocess.run(["bash", str(repo / "scripts" / "bench_llm.sh"), "--keep-services", "--out",
                        str(tmp_path / "r2"), "beta"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    assert not any("export-texts" in c or c.endswith("stop worker llm") for c in calls)
    assert [c for c in calls if c.startswith("run -d")][0].count("/models/llm/beta.gguf") == 1
