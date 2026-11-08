"""検索品質の評価（docs/design/search.md 9 章）。

自分の文書に対する「質問」と「正解のファイル名」の組を用意し、動いている docseed に対して検索して
Recall@5 / Recall@20 / MRR を検索方法ごとに出す。意味検索の類似度の下限（DOCSEED_SEARCH_SEM_MIN）を
いくつか試して、どの値が良いかを比べることもできる。

質問ファイル（JSON。リポジトリには入れず手元に置く）:
    [
      {"q": "振込先の口座番号", "expect": ["請求書_A社.pdf"]},
      {"q": "契約はいつまで有効か", "expect": ["業務委託契約書.pdf", "契約書_写し.pdf"]}
    ]
    expect のどれか 1 つが見つかれば正解とする（ファイル名で照合）。

使い方:
    # 設定画面で発行した API トークン（閲覧のみで可）を使う
    export DOCSEED_URL=https://docseed.local DOCSEED_TOKEN=dsk_...
    python scripts/eval_search.py queries.json
    python scripts/eval_search.py queries.json --sem-min 0.75,0.80,0.85
    python scripts/eval_search.py queries.json --insecure   # Caddy の内部 CA の証明書を検証しない

外部ライブラリは使わない（Python 3.10 以上の標準ライブラリだけで動く）。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

MODES = ["hybrid", "semantic", "keyword"]


def search(base: str, token: str, q: str, mode: str, sem_min: float | None, ctx) -> list[str]:
    params = {"q": q, "mode": mode, "limit": 20}
    if sem_min is not None:
        params["sem_min"] = sem_min
    req = urllib.request.Request(f"{base}/api/v1/search?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        body = json.load(r)
    return [x["document"]["filename"] for x in body["results"]]


def evaluate(cases, base, token, mode, sem_min, ctx, verbose) -> dict[str, float]:
    r5 = r20 = mrr = 0.0
    misses = []
    for c in cases:
        names = search(base, token, c["q"], mode, sem_min, ctx)
        expect = set(c["expect"])
        rank = next((i + 1 for i, n in enumerate(names) if n in expect), None)
        if rank is not None:
            r5 += rank <= 5
            r20 += rank <= 20
            mrr += 1 / rank
        else:
            misses.append((c["q"], names[:3]))
    n = len(cases)
    if verbose and misses:
        print(f"  見つからなかった質問（{mode}）:", file=sys.stderr)
        for q, top in misses:
            print(f"    {q!r} → 上位: {top}", file=sys.stderr)
    return {"recall@5": r5 / n, "recall@20": r20 / n, "mrr": mrr / n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queries", help="質問ファイル（JSON）")
    ap.add_argument("--url", default=os.environ.get("DOCSEED_URL", "http://localhost:8000"))
    ap.add_argument("--token", default=os.environ.get("DOCSEED_TOKEN"))
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--sem-min", help="意味検索の類似度の下限（カンマ区切りで複数試せる。省略時はサーバーの設定値）")
    ap.add_argument("--insecure", action="store_true", help="HTTPS の証明書を検証しない（内部 CA の場合）")
    ap.add_argument("-v", "--verbose", action="store_true", help="見つからなかった質問を表示する")
    args = ap.parse_args()
    if not args.token:
        sys.exit("API トークンを --token か環境変数 DOCSEED_TOKEN で指定してください（設定画面で発行できます）")
    with open(args.queries, encoding="utf-8") as f:
        cases = json.load(f)
    if not cases:
        sys.exit("質問がありません")
    ctx = ssl._create_unverified_context() if args.insecure else None  # noqa: S323 - 利用者が明示した場合だけ
    floors = [float(x) for x in args.sem_min.split(",")] if args.sem_min else [None]
    print(f"質問 {len(cases)} 件\n")
    print("| 検索方法 | 類似度の下限 | Recall@5 | Recall@20 | MRR |")
    print("|---|---|---|---|---|")
    for mode in args.modes.split(","):
        for floor in (floors if mode != "keyword" else [None]):
            m = evaluate(cases, args.url.rstrip("/"), args.token, mode, floor, ctx, args.verbose)
            print(f"| {mode} | {'設定値' if floor is None else floor} | {m['recall@5']:.2f} | "
                  f"{m['recall@20']:.2f} | {m['mrr']:.2f} |")


if __name__ == "__main__":
    main()
