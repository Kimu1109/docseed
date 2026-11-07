"""SQLite（sqlite-vec のベクトル検索 / FTS5 trigram の全文検索）の規模ごとの速度を計測する（Phase 0）。

設計書 search.md 8 章の見積もり（10 万チャンクで検索 p95 1 秒以内）を確かめる。
ベクトルは乱数、本文は合成テキストを使うため、検索の「質」ではなく「速度と容量」だけを測る。

使い方の例:
    python bench_vec.py                       # 1 万・10 万件、float32 と int8
    python bench_vec.py --sizes 1000000       # 100 万件（DB が数 GB になる。SSD 上で実行すること）
    python bench_vec.py --skip-fts            # ベクトル検索だけ

出力: results/<日時>-vec.jsonl と標準出力の Markdown 表
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DIM = 384
PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -65536;
PRAGMA mmap_size = 268435456;
PRAGMA temp_store = MEMORY;
"""
FTS_QUERIES = ["請求書番号", "振込先", "秘密保持", "有効期間", "議事録", "問い合わせ",
               "健康診断", "取扱説明書", "INV-2026", "グラントウキョウ"]
SHORT_QUERIES = ["契約", "税", "印鑑"]


def connect(path: Path) -> sqlite3.Connection:
    import sqlite_vec
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(PRAGMAS)
    return conn


def db_size_mb(path: Path) -> float:
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*")) / 1024 / 1024


def random_unit_vectors(rng: np.random.Generator, n: int) -> np.ndarray:
    v = rng.standard_normal((n, DIM)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def to_blob(v: np.ndarray, vtype: str) -> bytes:
    if vtype == "int8":
        return np.clip(np.round(v * 127), -127, 127).astype(np.int8).tobytes()
    return v.astype(np.float32).tobytes()


# ---------------------------------------------------------------------------
# ベクトル検索
# ---------------------------------------------------------------------------

def bench_vectors(db: Path, n: int, vtype: str, k: int, queries: int, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    conn = connect(db)
    conn.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding {vtype}[{DIM}] distance_metric=cosine)")
    wrap = "vec_int8(?)" if vtype == "int8" else "?"

    batch = 10_000
    t0 = time.perf_counter()
    for start in range(0, n, batch):
        m = min(batch, n - start)
        vecs = random_unit_vectors(rng, m)
        conn.executemany(f"INSERT INTO v(rowid, embedding) VALUES (?, {wrap})",
                         [(start + i + 1, to_blob(vecs[i], vtype)) for i in range(m)])
        conn.commit()
    insert_s = time.perf_counter() - t0

    secs = []
    for i in range(queries + 2):
        q = to_blob(random_unit_vectors(rng, 1)[0], vtype)
        t = time.perf_counter()
        rows = conn.execute(
            f"SELECT rowid, distance FROM v WHERE embedding MATCH {wrap} AND k = ?", (q, k)
        ).fetchall()
        if i >= 2:  # 最初の 2 回はキャッシュ温め
            secs.append(time.perf_counter() - t)
        assert len(rows) == min(k, n)
    conn.close()
    s = common.summarize(secs)
    return {
        "type": "vector", "n": n, "vtype": vtype, "k": k,
        "insert_s": insert_s, "insert_per_s": n / insert_s,
        "query_ms_p50": s["p50"] * 1000, "query_ms_p95": s["p95"] * 1000,
        "db_mb": db_size_mb(db), "peak_rss_mb": common.peak_rss_mb(),
        "temp_c": common.cpu_temp_c(),
    }


# ---------------------------------------------------------------------------
# 全文検索
# ---------------------------------------------------------------------------

def synthetic_chunk(rng: np.random.Generator, sentences: list[str]) -> str:
    target = int(rng.integers(300, 800))
    s = ""
    while len(s) < target:
        sent = sentences[int(rng.integers(len(sentences)))]
        # 数字を変えて索引の語彙を増やす
        s += sent.replace("2026", str(int(rng.integers(1990, 2040)))).replace(
            "1234567", str(int(rng.integers(1_000_000, 9_999_999))))
    return s[:target]


def bench_fts(db: Path, n: int, k: int, seed: int) -> dict[str, Any]:
    from make_samples import SENTENCES
    rng = np.random.default_rng(seed)
    conn = connect(db)
    conn.executescript("""
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize='trigram');
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
        END;
    """)
    batch = 5_000
    t0 = time.perf_counter()
    for start in range(0, n, batch):
        m = min(batch, n - start)
        conn.executemany("INSERT INTO chunks(id, text) VALUES (?, ?)",
                         [(start + i + 1, synthetic_chunk(rng, SENTENCES)) for i in range(m)])
        conn.commit()
    insert_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
    conn.commit()
    optimize_s = time.perf_counter() - t0

    match_secs = []
    for q in FTS_QUERIES * 2:
        t = time.perf_counter()
        conn.execute(
            "SELECT rowid, bm25(chunks_fts) AS s, highlight(chunks_fts, 0, char(2), char(3)) "
            "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
            (f'"{q}"', k),
        ).fetchall()
        match_secs.append(time.perf_counter() - t)

    # 2 文字以下の語: 候補（k 件）の中だけを instr で照合する方式（search.md 4.3）
    cand = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY random() LIMIT ?", (k,))]
    ph = ",".join("?" * len(cand))
    short_secs = []
    for q in SHORT_QUERIES * 3:
        t = time.perf_counter()
        conn.execute(f"SELECT id FROM chunks WHERE id IN ({ph}) AND instr(text, ?) > 0",
                     (*cand, q)).fetchall()
        short_secs.append(time.perf_counter() - t)

    # 参考: 全件 LIKE 走査（採用しない方式。どれだけ遅いかの確認）
    t = time.perf_counter()
    conn.execute("SELECT count(*) FROM chunks WHERE text LIKE ?", ("%契約%",)).fetchone()
    like_full_s = time.perf_counter() - t
    conn.close()

    ms, ss = common.summarize(match_secs), common.summarize(short_secs)
    return {
        "type": "fts", "n": n, "k": k,
        "insert_s": insert_s, "insert_per_s": n / insert_s, "optimize_s": optimize_s,
        "match_ms_p50": ms["p50"] * 1000, "match_ms_p95": ms["p95"] * 1000,
        "short_in_candidates_ms_p95": ss["p95"] * 1000,
        "like_full_scan_ms": like_full_s * 1000,
        "db_mb": db_size_mb(db), "peak_rss_mb": common.peak_rss_mb(),
        "temp_c": common.cpu_temp_c(),
    }


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="10000,100000", help="件数（カンマ区切り）")
    ap.add_argument("--vtypes", default="float32,int8", help="ベクトルの型（float32, int8）")
    ap.add_argument("--k", type=int, default=200, help="検索で取得する件数（search.md の K_vec / K_kw）")
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--skip-fts", action="store_true")
    ap.add_argument("--skip-vec", action="store_true")
    ap.add_argument("--keep-db", action="store_true", help="計測に使った DB を消さずに残す")
    ap.add_argument("--db-dir", type=Path, default=Path(__file__).resolve().parent / "results" / "db",
                    help="計測用 DB の作成先（実運用と同じ SSD 上を指定すること）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, help="結果の保存先ディレクトリ（既定: scripts/poc/results）")
    args = ap.parse_args()

    sizes = [int(x) for x in args.sizes.split(",")]
    vtypes = [x.strip() for x in args.vtypes.split(",")]
    args.db_dir.mkdir(parents=True, exist_ok=True)
    out_path = common.results_path("vec", args.out)
    common.write_jsonl(out_path, {"type": "system", **common.system_info(),
                                  "sqlite": sqlite3.sqlite_version, "argv": sys.argv[1:]})

    def fresh_db(name: str) -> Path:
        p = args.db_dir / name
        for f in args.db_dir.glob(name + "*"):
            f.unlink()
        return p

    vec_rows, fts_rows = [], []
    for n in sizes:
        if not args.skip_vec:
            for vt in vtypes:
                print(f"▶ vector n={n} {vt}", file=sys.stderr)
                db = fresh_db(f"vec-{n}-{vt}.sqlite")
                rec = bench_vectors(db, n, vt, args.k, args.queries, args.seed)
                common.write_jsonl(out_path, rec)
                vec_rows.append(rec)
                print("  " + json.dumps({k: round(v, 1) if isinstance(v, float) else v
                                         for k, v in rec.items()}, ensure_ascii=False), file=sys.stderr)
                if not args.keep_db:
                    fresh_db(db.name)
        if not args.skip_fts:
            print(f"▶ fts n={n}", file=sys.stderr)
            db = fresh_db(f"fts-{n}.sqlite")
            rec = bench_fts(db, n, args.k, args.seed)
            common.write_jsonl(out_path, rec)
            fts_rows.append(rec)
            if not args.keep_db:
                fresh_db(db.name)

    if vec_rows:
        print("\n### ベクトル検索（sqlite-vec, 総当たり）\n")
        print(common.md_table(vec_rows, [
            ("n", "件数"), ("vtype", "型"), ("k", "k"), ("insert_per_s", "登録(件/s)"),
            ("query_ms_p50", "検索p50(ms)"), ("query_ms_p95", "検索p95(ms)"), ("db_mb", "DB(MB)"),
        ]))
    if fts_rows:
        print("\n### 全文検索（FTS5 trigram）\n")
        print(common.md_table(fts_rows, [
            ("n", "件数"), ("insert_per_s", "登録(件/s)"), ("match_ms_p50", "MATCH p50(ms)"),
            ("match_ms_p95", "MATCH p95(ms)"), ("short_in_candidates_ms_p95", "2文字語 p95(ms)"),
            ("like_full_scan_ms", "全件LIKE(ms)"), ("db_mb", "DB(MB)"),
        ]))
    print(f"\n結果: {out_path}")


if __name__ == "__main__":
    main()
