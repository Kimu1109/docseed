# 状態の確認

## 管理画面

左のメニューの「管理」（管理者だけ。LAN・Tailscale の中からだけ開けます）。

| 欄 | 内容 |
|---|---|
| 確認が必要な項目 | 警告をまとめて出します（下の表）。項目があると、左のメニューの「管理」に赤いバッジが付きます |
| システム | CPU の温度・負荷、メモリ、SSD、データベースの大きさ、ファイル・過去の版・検索の索引の件数、使っているモデル、docseed の版 |
| HTTPS の証明書 | 証明書の名前・期限（[HTTPS の証明書](https.md#証明書の監視)） |
| 要約（LLM） | 要約の件数、旧モデルで作った要約の上書き（[小型の LLM](llm.md)） |
| バックアップ | 最後のバックアップ・検査・復旧の練習の結果（[バックアップと復旧](backup.md)） |
| 通知 | テスト通知、最近の通知と送れなかったときのエラー（[通知](notifications.md)） |
| 文字の読み取りと検索の準備 | 処理中・待ちのファイル、失敗したジョブ、まとめての再処理 |
| ログイン / ユーザー | [ユーザーとログイン](users.md) |

### 確認が必要な項目

| 警告 | 見ること |
|---|---|
| worker が止まっている | `docker compose ps`・`docker compose logs worker` |
| 失敗したジョブがある | 「文字の読み取りと検索の準備」で理由を見る。モデルが無いなら [困ったとき](troubleshooting.md#処理が進まない) |
| SSD の空きが少ない | ゴミ箱・過去の版（`DOCSEED_VERSIONS_KEEP`）・容量の上限を見直す |
| バックアップの遅れ・失敗 | 36 時間以上成功していない・失敗した。`journalctl -u docseed-backup -n 50` |
| CPU の温度が高い | `DOCSEED_THERMAL_WARN_C`（既定 75℃）を超えた。`DOCSEED_THERMAL_PAUSE_C`（既定 80℃）を超えると処理を一時停止する。クーラーを確かめる |
| メモリが少ない | 使えるメモリが `DOCSEED_MEMORY_WARN_PCT`（既定 10%）未満。任意の機能（LLM・描画）を止めるか、LLM のモデルを小さくする |
| 証明書 | 検証できない・期限まで 7 日を切った |

### 文字の読み取りのやり直し

管理画面の「文字の読み取りと検索の準備」のボタン、または [コマンド](cli.md) の `docseed reprocess` で行います。

| ボタン | 使うとき |
|---|---|
| 失敗したファイルをすべて再処理 | モデルを取得し直した後など |
| 古い版で処理したファイルを再処理 | 更新で読み取りが良くなったとき（対象は更新の内容による） |
| 形式が未対応だったファイルを再処理 | 新しい形式に対応した後（HEIC・Office・文字起こしなど） |

## コマンドで確かめる

```bash
docker compose exec api docseed status
```

管理画面の「確認が必要な項目」と同じ内容です（`--json` も可）。重大な警告（error）があれば終了コード 1 になるので、cron などから呼べます。

```bash
docker compose ps
```

```bash
docker compose logs --tail 100 api worker
```

## API・Prometheus

管理者の API トークン（[利用者ガイドの API トークン](../user/api-tokens.md)。閲覧のみで可）を bearer で渡します。

| パス | 内容 |
|---|---|
| `GET /api/v1/admin/status` | 状態と警告（`docseed status --json` と同じ） |
| `GET /api/v1/admin/system` | 管理画面の「システム」 |
| `GET /api/v1/admin/jobs` | 処理中（`running`）・待ち（`waiting`）のファイル |
| `GET /api/v1/admin/metrics` | Prometheus の形式の指標。処理待ちの件数、OCR・埋め込みにかかった時間、検索の速さ、CPU 温度、SSD の空き、ユーザーごとの使用量など |

Prometheus の設定の例は [配置・運用](../design/deployment.md) の 6.2。

## ログ

| 場所 | 内容 |
|---|---|
| `docker compose logs <サービス>` | 各コンテナのログ（1 つ 10MB × 5 世代で回す） |
| `<DOCSEED_DATA_DIR>/logs/security.log` | ログインの失敗・無効な共有リンク・回数の上限超え（fail2ban が読む。[インターネットに公開する](exposure.md#失敗が続く接続元をファイアウォールで遮断するfail2ban)） |
