# コマンドの一覧

## docseed のコマンド

動いている api コンテナで実行します。

```bash
docker compose exec api docseed <コマンド> [オプション]
```

`docker compose exec api docseed <コマンド> --help` で、そのコマンドのオプションを見られます。この一覧は `tests/test_docs.py` が `src/docseed/cli.py` と照らし合わせています。

### 管理

| コマンド | 内容 |
|---|---|
| `docseed create-admin --username NAME` | 管理者を作る（`--display-name`。パスワードは対話で入れる。`--password` も使えるが履歴に残る） |
| `docseed reset-auth --username NAME` | ログイン方法（パスキー・確認コード・外部アカウントの連携）を消し、仮のパスワードを発行する。すべての端末をログアウトし、API トークンを無効にする |
| `docseed notify [--test]` | 異常の通知を 1 回確かめて送る。`--test` はテストの通知を送って、宛先に届くか確かめる（[異常の通知と更新](security-notify.md)） |
| `docseed status [--json]` | 状態と警告を表示する（管理画面と同じ）。重大な警告があれば終了コード 1 |
| `docseed purge-trash` | 保持期間を過ぎたゴミ箱の項目を完全に削除する（worker が毎日行うので、ふつうは不要） |

### 処理のやり直し

| コマンド | 内容 |
|---|---|
| `docseed reprocess failed\|outdated\|unsupported\|all` | 文書を再処理する。`failed` 失敗したもの / `outdated` 古い版の処理のもの / `unsupported` 形式が未対応だったもの / `all` すべて。`--force-ocr` でテキスト層のある PDF も画像として読む |
| `docseed derive missing\|all` | 画像・PDF のサムネイルとプレビューを作る（`all` は作り直す。文字の読み取りはやり直さない） |

### バックアップ

[バックアップと復旧](backup.md)。

| コマンド | 内容 |
|---|---|
| `docseed backup-db [--keep N]` | DB のスナップショットを `backup/` に作る（既定 7 世代。整合性を確かめてから置き換える） |
| `docseed restore-db [スナップショット]` | DB をスナップショット（省略時は `backup/` の最新）から戻す。api と worker を止めて `docker compose run --rm --no-deps api docseed restore-db` で実行（`--force` で worker が動いている形跡があっても実行） |
| `docseed verify-files [--quick] [--json]` | DB と原本ファイルの対応（存在・SHA-256）を確かめる。`--quick` は大きさだけ。問題があれば終了コード 1 |

### モデル

| コマンド | 内容 |
|---|---|
| `docseed fetch-models` | OCR と埋め込みのモデルを取得する（`tools` コンテナで実行: `docker compose --profile tools run --rm --build tools`。`--force` で取り直す） |
| `docseed fetch-models --llm NAME` | 要約に使う LLM（GGUF）を `models/llm/` に取得する（[小型の LLM](llm.md)） |
| `docseed fetch-models --asr base\|small` | 文字起こしのモデルを `models/asr/` に取得する（[文字起こし](asr.md)） |

### 評価・計測

[検索と候補の調整](tuning.md)。

| コマンド | 内容 |
|---|---|
| `docseed eval-suggest --username NAME` | 置き場所の候補のしきい値を、分類済みの文書で評価する（`--sim-min`・`--conf-min`・`--limit`・`--json`） |
| `docseed export-texts --out DIR` | LLM の比較（`scripts/bench_llm.sh`）に使う文書のテキストを書き出す（`--limit`・`--username`） |

### サービス（compose が起動する）

ふつうは手で実行しません。

| コマンド | 内容 |
|---|---|
| `docseed serve` | Web サーバー（api）。起動時にマイグレーションを適用する（`--host`・`--port`・`--forwarded-allow-ips`。`X-Forwarded-For` を信頼する送信元で、既定は `127.0.0.1`。compose では Caddy と api だけのネットワーク） |
| `docseed migrate` | マイグレーションだけを適用する |
| `docseed worker` | 文字の読み取り（OCR）・埋め込み・要約・文字起こしの処理 |
| `docseed fetcher` | Web クリップの取得（profile `webclip`） |
| `python -m docseed.webclip.browser` | JavaScript で描くページの描画サービス（`webclip-browser`。profile `webclip-render`） |

## 運用のスクリプト

Pi 本体で、リポジトリのディレクトリから実行します。

| スクリプト | 内容 |
|---|---|
| `sudo scripts/ops/install-backup.sh` | バックアップ・検査・復旧の練習の systemd タイマーを入れ、`/etc/docseed/backup.env` とパスワードを作る |
| `scripts/ops/backup.sh` | バックアップの本体（タイマーが毎日 3:00 に実行。手で動かすなら `sudo systemctl start docseed-backup.service`） |
| `sudo scripts/ops/restore-drill.sh` | 最新のバックアップを一時ディレクトリに復元し、原本と DB の対応を確かめる（毎月第 1 日曜に自動） |
| `sudo scripts/ops/install-cert-hook.sh` | certbot の更新のたびに Caddy を再起動するフックを入れる（[HTTPS の証明書](https.md)） |
| `sudo scripts/ops/install-fail2ban.sh` | fail2ban を入れて docseed 用の設定を置く（[インターネットに公開する](exposure.md)） |
| `scripts/bench_llm.sh [名前…]` | LLM のモデルを自分の文書で比べる（[小型の LLM](llm.md#モデルを比べる)） |
| `python scripts/eval_search.py queries.json` | 検索の品質（Recall@5・MRR）を測る（[検索と候補の調整](tuning.md)） |
