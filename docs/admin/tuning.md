# 検索と候補の調整

既定の値は Pi 5 での計測で決めたもので、ふつうは変えなくて構いません。「関係のない結果が多い」「あるはずのものが見つからない」と言われたときに使います。

## 検索の品質を測る

自分の文書に対する「質問」と「正解のファイル名」を 20〜30 件ほど JSON に書き、`scripts/eval_search.py` で Recall@5 / MRR を測れます（使い方はスクリプト冒頭のコメント）。
意味検索で無関係なファイルが多く出る・逆に見つからない場合は、`--sem-min 0.75,0.80,0.85` で類似度の下限を比べ、良かった値を `.env` の `DOCSEED_SEARCH_SEM_MIN` に設定します（既定 0.80）。

```bash
export DOCSEED_URL=https://docseed.local DOCSEED_TOKEN=dsk_...
```

```bash
python scripts/eval_search.py queries.json --sem-min 0.75,0.80,0.85
```

- 質問ファイルは `[{"q": "振込先の口座番号", "expect": ["請求書_A社.pdf"]}, ...]` の形です。文書の中身が分かるので、リポジトリには入れず手元に置いてください
- 内部 CA の証明書のときは `--insecure`
- 値を変えたら `docker compose up -d api` で読み直します

## 置き場所の候補のしきい値

候補のしきい値は仮の値です。分類済みの文書がある程度たまったら、次で正解率を確かめられます（1 位の正解率・上位 3 の正解率・「おすすめ」の正しさ）。

```bash
docker compose exec api docseed eval-suggest --username <名前>
```

`--sim-min`・`--conf-min` で比べる値を変えられます（[コマンドの一覧](cli.md)）。置き場所・タグの候補そのものを止めるには `DOCSEED_SUGGEST_ENABLED=false`。

## 文字の読み取り（OCR）

| 設定 | 既定 | 変えるとき |
|---|---|---|
| `DOCSEED_OCR_ENGINE` | `ppocrv6_small` | 精度を優先するなら `ppocrv6_medium`（約 4 倍遅い） |
| `DOCSEED_OCR_DPI` | 200 | 小さな文字のスキャンが読めないとき上げる（遅くなる） |
| `DOCSEED_OCR_THREADS`・`DOCSEED_EMBED_THREADS` | 3 | 4 コアのうち 1 つは Web 画面用に残す |

変えたあと読み直すには、管理画面の再処理か `docseed reprocess`（[状態の確認](monitoring.md#文字の読み取りのやり直し)）。計測の記録は [Phase 0 計測結果](../research/phase0-results.md)。

## 要約のモデル

[小型の LLM のモデルを比べる](llm.md#モデルを比べる)。
