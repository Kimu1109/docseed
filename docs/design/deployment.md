# 配置・運用 詳細設計

[設計書](../design.md) の「3. 全体構成」「8. 非機能要件」のうち、配置・設定・バックアップ・監視の詳細。

## 1. ホストの準備

| 項目 | 内容 |
|---|---|
| ハードウェア | Raspberry Pi 5（8GB 以上）、NVMe SSD（M.2 HAT 経由、256GB 以上）、公式アクティブクーラー、27W 電源 |
| OS | Raspberry Pi OS Lite 64-bit を SSD から起動 |
| PCIe | 安定性を優先して既定（Gen 2）のまま。Phase 0 で Gen 3 の速度と安定性を確認してから決める |
| スワップ | zram（2GB 程度）。SSD へのスワップは使わない |
| 時刻 | systemd-timesyncd を有効化（ログ・期限の判定に必要） |
| Docker | Docker Engine + Compose プラグイン（公式の arm64 パッケージ） |
| ファイアウォール | ufw で 443（と Samba を使う場合は LAN からの 445）のみ許可 |
| 外部アクセス | Tailscale をホストに導入。インターネットへのポート開放はしない |

データディレクトリ:

```
/srv/docseed/
├── files/        原本（.tmp/ は一時ファイル）
├── derived/      サムネイル・ページ画像のキャッシュ
├── db/           docseed.sqlite（+ -wal, -shm）
├── models/       PP-OCRv6 ONNX（RapidOCR）、ruri-v3-70m
├── inbox/        Samba の投入フォルダ（任意）
├── backup/       DB の日次スナップショット
├── run/          worker への通知用ファイル等
└── caddy/        Caddy の証明書・設定データ
```

`files/` 以外（DB・モデル・キャッシュ）を同じ SSD に置く。SSD は 1 台のため、バックアップは必ず別の機器に取る（5 章）。

## 2. コンテナ構成（compose.yaml の概要）

```yaml
services:
  caddy:
    image: caddy:2
    ports: ["443:443", "80:80"]
    volumes:
      - ./docker/Caddyfile:/etc/caddy/Caddyfile:ro
      - /srv/docseed/caddy:/data
    depends_on: [api]
    restart: unless-stopped

  api:
    image: docseed:latest
    command: ["docseed", "serve"]          # 起動時にマイグレーションを実行してから Uvicorn を起動
    env_file: .env
    volumes:
      - /srv/docseed:/srv/docseed
      - /sys/class/thermal:/host/thermal:ro
    mem_limit: 1536m
    restart: unless-stopped

  worker:
    image: docseed:latest
    command: ["docseed", "worker"]
    env_file: .env
    volumes:
      - /srv/docseed:/srv/docseed
      - /sys/class/thermal:/host/thermal:ro
    mem_limit: 3g
    networks: [internal]                   # 外部へ通信させない
    restart: unless-stopped

  # Samba はコンテナにせず、Pi 本体の samba パッケージで /srv/docseed/inbox/<ユーザー名> を公開する（Phase 4 で決定）。
  # 理由: 保守されている arm64 のイメージの選択肢が少ないこと、ファイルの持ち主（UID 1000）を
  # force user で合わせるだけで済み、設定が単純なこと。手順は README の「受け取り箱」。

networks:
  default: {}
  internal:
    internal: true
```

- api と worker は同じイメージで、起動コマンドだけを変える。
- api は Uvicorn 1 プロセス（ワーカー 1）。SQLite の書き込みとメモリの都合で増やさない。非同期 I/O で同時接続をさばく。
- api と worker は同じ SQLite ファイルを開く（WAL モードで読み書きの並行が可能。書き込みは短いトランザクションにする）。

### 2.1 Docker イメージ

```
FROM python:3.12-slim-bookworm（arm64）
  ├ apt: libgl1 など OpenCV の最小依存（RapidOCR が必要とする場合）、tini
  ├ pip: 本体 + onnxruntime + sentence-transformers + torch（CPU 版）+ sqlite-vec + pypdfium2 + Pillow ...
  ├ 非 root ユーザー docseed（UID 1000）で実行
  └ ENTRYPOINT ["tini", "--"]
```

- 依存は当面 `pip install .`（`pyproject.toml` の下限指定）で入れる。Phase 2 で依存が増えた時点で `uv` のロックファイル（`uv.lock`）に切り替えて再現性を保つ。
- モデルはイメージに含めない（大きいため。`/srv/docseed/models` に置く）。
- Pi 上で直接ビルドするか、GitHub Actions の arm64 ランナーでビルドして GHCR に置く（Phase 1 で決める）。

### 2.1.1 コンテナの強化

インターネットに公開しても、1 つのコンテナが乗っ取られたときの被害を小さくするため、`compose.yaml` の `x-hardening` を caddy・api・worker・fetcher・llm に適用する。

| 設定 | 内容 |
|---|---|
| `read_only: true` | イメージの中を書き換えさせない。書けるのはデータディレクトリ（ボリューム）と `/tmp`（tmpfs、256MB）だけ |
| `cap_drop: [ALL]` | Linux の capabilities をすべて外す。Caddy だけ 80 / 443 を待ち受けるため `NET_BIND_SERVICE` を戻す |
| `no-new-privileges` | setuid などで権限を上げさせない |
| `pids_limit` | プロセス数の上限（fork 爆弾の対策）。api・worker 256、cloudflared 64、ほかは 128 |
| `TMPDIR` | api・worker・fetcher は `/srv/docseed/files/.tmp`。アップロードの受信中の一時ファイル（最大 1GB）を RAM ではなく SSD に置く |

- Caddy は設定の控え（`/config/caddy/autosave.json`）を tmpfs の `/config` に書く。証明書と内部 CA は `/data`（ボリューム）。内部 CA をコンテナの信頼ストアに入れる処理は `skip_install_trust` で止める
- tools（モデルの取得と変換）はキャッシュを書くので `read_only` にしない（`cap_drop` と `no-new-privileges` だけ）
- cloudflared（Cloudflare Tunnel、profile `tunnel`）も同じ設定で動く（[セキュリティ](security.md) 10 章）
- 確認済み: caddy（80 / 443 の待ち受け）、api（ログイン・5MB のアップロード・受け取り用リンク）、worker（PDF の画像化と OCR）、fetcher、llm（llama-server の起動と /health）が、この設定のまま動くこと

### 2.2 Caddyfile の概要

```
{$DOCSEED_HOST} {
    encode zstd gzip
    request_body {
        max_size 1GB
    }
    reverse_proxy api:8000 {
        flush_interval -1          # SSE とストリーミングのため
    }
    header {
        Strict-Transport-Security "max-age=31536000"
        -Server
    }
}
```

- 証明書: Tailscale の名前（`*.ts.net`）を使う場合は Tailscale の証明書、LAN のみの場合は Caddy の内部 CA（`tls internal`）を使い、利用する端末に CA 証明書を入れる。`DOCSEED_TLS_MODE` で切り替える（`internal` と、手元の証明書ファイルを使う `file` は実装済み。`tailscale` / `acme_dns` は Phase 6）（[拡張機能](extensions.md) 2 章）。fetcher と llm のサービスの追加も同じ文書の 1.1 節。
- 静的ファイル（CSS/JS）はアプリが配信し、長期キャッシュのヘッダを付ける（ファイル名にハッシュを含める）。

## 3. 設定

環境変数（`.env`）で設定する。秘密の値は `.env` のみに置き、Git には入れない（`.env.example` を用意）。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `DOCSEED_HOST` | — | 公開ホスト名（例 `docseed.tailxxxx.ts.net`） |
| `DOCSEED_BASE_PATH` | （空） | URL の接頭辞（例 `/docseed` で `https://<名前>/docseed/` に置く）。下の 3.1 |
| `DOCSEED_DATA_DIR` | `/srv/docseed` | データディレクトリ |
| `DOCSEED_SECRET_KEY` | — | Cookie 署名用。32 バイト以上の乱数（必須） |
| `DOCSEED_MAX_UPLOAD_MB` | `1024` | 1 ファイルの上限 |
| `DOCSEED_TRASH_RETENTION_DAYS` | `30` | ゴミ箱の保持日数の既定（利用者は設定で 7〜365 日から選べる） |
| `DOCSEED_VERSIONS_KEEP` | `10` | 1 文書あたりに残す過去の版の数（0 なら残さない） |
| `DOCSEED_OCR_ENGINE` | `ppocrv6_small` | OCR エンジン（`ppocrv6_small` / `ppocrv6_medium`） |
| `DOCSEED_OCR_DPI` | `200` | PDF のページ画像化の解像度 |
| `DOCSEED_OCR_THREADS` | `3` | ONNX Runtime のスレッド数 |
| `DOCSEED_EMBED_MODEL` | `cl-nagoya/ruri-v3-70m` | 埋め込みモデル |
| `DOCSEED_EMBED_BACKEND` | `onnx` | `onnx`（fp32）、`onnx-int8`、`torch` |
| `DOCSEED_EMBED_THREADS` | `3`（worker）/ `1`（api） | |
| `DOCSEED_THERMAL_PAUSE_C` | `80` | worker が一時停止する CPU 温度 |
| `DOCSEED_INBOX_ENABLED` | `false` | inbox 取り込みの有効化 |
| `DOCSEED_LOG_LEVEL` | `INFO` | |

### 3.1 パスの下で動かす（`DOCSEED_BASE_PATH`）

- アプリの中のパスは接頭辞なし（`/d/1`）のまま。一番外側の ASGI ミドルウェア（`docseed/urls.py` の `BasePathMiddleware`）が、届いた `/docseed/…` から接頭辞を外す。外側のプロキシが先に外して送ってきた場合も、そのまま受け付ける
- `scope["root_path"]` は使わない。Starlette は `path` が `root_path` で始まる前提で `Mount`（`/static`）を扱うため、外した `path` と組み合わせられない。接頭辞はリクエストごとの `ContextVar` に入れ、URL を作る側が読む
  - テンプレート: `{{ base }}/d/{{ id }}`。JS: `<meta name="docseed-base">` を読んだ `BASE`。Python: `urls.href()`・`urls.public_root()`
  - リダイレクト（`Location`・`HX-Redirect` など）と `Set-Cookie` の `Path` は、ミドルウェアが応答のヘッダにまとめて接頭辞を付ける（アプリは今まで通り `/d/1` や `Path=/` を書く）
- 外から開く URL: `public_url` はオリジンだけ（パスキーの origin）、`public_base` は接頭辞まで（OIDC のリダイレクト URI・通知のリンク・ブックマークレット）
- 接頭辞の最初の部分がアプリのパス（`/s`・`/api` など）と重なる値は、起動時に止める（外して送られたときに区別できないため）
- Caddy の「外側からは 404」（security.md 8 章）は、接頭辞の中身を問わず末尾で合わせる `path_regexp` でも行う（`.env` の書き方に左右されないように）
- 接頭辞を付けたときに、画面のリンク・フォーム・htmx・画像がすべて接頭辞の下を指し、たどれることは `tests/test_base_path.py` で確かめる

## 4. 初期セットアップ手順

```bash
# 1. データディレクトリ
sudo mkdir -p /srv/docseed && sudo chown 1000:1000 /srv/docseed

# 2. 設定
cp .env.example .env && $EDITOR .env      # DOCSEED_HOST, DOCSEED_SECRET_KEY を設定

# 3. モデルの取得（ONNX に変換して /srv/docseed/models に配置）
docker compose --profile tools run --rm --build tools

# 4. 起動
docker compose up -d

# 5. 管理者の作成
docker compose exec api docseed create-admin --username kimu

# 6. バックアップ（5 章）
sudo apt install -y restic
sudo scripts/ops/install-backup.sh
```

## 5. バックアップと復旧

### 5.1 対象と方法

| 対象 | 方法 | 頻度 |
|---|---|---|
| DB | `docseed backup-db` で SQLite のオンラインバックアップ API を使って `backup/docseed-YYYYMMDD.sqlite` を作成（7 日分保持）。一時ファイルに書き、`PRAGMA quick_check` で確かめてから置き換える。写しは WAL を使わない 1 ファイル | 毎日 3:00 |
| 原本 `files/`、DB スナップショット `backup/`、Caddy のデータ `caddy/`（内部 CA の鍵） | restic で外付け USB ドライブ、または別のマシン（SFTP / REST サーバー）へ | 毎日（DB スナップショットの直後） |
| `.env`、compose.yaml | restic に含める（`.env` は restic のリポジトリ暗号化で保護） | 毎日 |
| 対象外 | `derived/`、`models/`（再生成・再取得できる）、`files/.tmp/`、動いている DB と `-wal`/`-shm`（スナップショットで代替）、`inbox/` | — |

- 実行はホストの systemd タイマー（コンテナ内の cron は使わない）。`scripts/ops/install-backup.sh` がユニット（`deploy/systemd/`）を置き、設定 `/etc/docseed/backup.env` と restic のパスワードのファイル `/etc/docseed/restic-password` を用意する。
  - `docseed-backup.timer`（毎日 3:00、最大 10 分ずらす、電源断で逃した分は次の起動時に実行）→ `scripts/ops/backup.sh`: `docker compose exec api docseed backup-db` → `restic backup --tag docseed` → `restic forget --prune`
  - `docseed-backup-check.timer`（毎週日曜 4:30）→ `backup.sh check`: `restic check --read-data-subset=5%`
- restic の保持: 日次 7、週次 4、月次 6（`backup.env` で変更可）。
- `backup.sh` は結果を `backup/restic-status.json`（`{"time", "ok", "snapshot", "message", "host"}`）に書く。管理画面がこれを読み、失敗・36 時間以上の遅れを警告する（6 章）。
- 設計時は DB を 3:00、restic を 3:30 と分けていたが、スナップショットの完成を待ってから restic を動かせるよう、1 つのスクリプトで順に実行する形にした。

### 5.2 復旧手順

SSD の故障などで Pi を作り直す場合:

1. 新しい SSD に OS と Docker を入れ、リポジトリを取得する（`git clone`、1 章の準備）。`sudo apt install -y restic`。
2. バックアップ先（USB ドライブなど）をつなぎ、restic で復元する。パスワードは控えておいたものを使う。
   ```bash
   export RESTIC_REPOSITORY=/mnt/backup/docseed-restic RESTIC_PASSWORD_FILE=/etc/docseed/restic-password
   restic snapshots --tag docseed                      # 復元するスナップショットを選ぶ（既定は最新）
   sudo -E restic restore latest --tag docseed --target /       # 元と同じ場所（/srv/docseed、リポジトリの .env）に戻る
   ```
   リポジトリを別の場所に clone した場合は、`.env` を `--target /tmp/r` などに復元してから移す。
3. DB をスナップショットから戻す: `docker compose run --rm --no-deps api docseed restore-db`（`backup/` の最新を `db/docseed.sqlite` に置く。既存の DB があれば `*.before-restore-<日時>` に退避し、古い `-wal`/`-shm` も片付ける）。
4. `docker compose --profile tools run --rm tools` でモデルを再取得する。
5. `docker compose up -d`。サムネイルは表示時に再生成される。
6. 動作確認: ログイン、検索、ダウンロード。`docker compose exec api docseed verify-files` で DB と原本ファイルの対応（存在・大きさ・SHA-256）を確認する。DB に記録の無いファイル（スナップショットの後にアップロードされたもの）は一覧に出るが、問題ではない。
7. バックアップのタイマーを入れ直す（`sudo scripts/ops/install-backup.sh`）。

最後のバックアップ以降にアップロードされたファイルは失われる（原本が残っていても、DB の記録が無いため一覧に出ない）。

### 5.3 復旧の練習

`sudo scripts/ops/restore-drill.sh` で、動いている docseed に触れずに 5.2 の 2・3・6 を試せる。restic の最新スナップショットを一時ディレクトリ（`/var/tmp/docseed-drill.*`）に復元し、そこで `restore-db` と `verify-files` を実行し、終わったら消す（`--keep` で残す）。

- **毎月の自動の練習**（実装で追加）: `docseed-restore-drill.timer`（毎月第 1 日曜 5:30。`install-backup.sh` が入れる）が `restore-drill.sh --auto` を実行する。
  - 先に `restic stats --mode restore-size` で復元する大きさを調べ、`DOCSEED_DRILL_DIR`（既定 `/var/tmp`）の空きがその `DOCSEED_DRILL_MARGIN`（既定 1.2）倍に足りなければ、練習を飛ばして記録だけを残す（終了コード 0）。これで「原本と同じだけの空きが要る」ために定期実行できなかった問題を避ける。手で実行したとき（`--auto` なし）は、足りなければ失敗にする。
  - 結果（`ok` / `failed` / `skipped`、スナップショット、復元した大きさ、所要時間、最後に成功した日時（飛ばした・失敗したときも前の記録から引き継ぐ））を `<データディレクトリ>/backup/restore-drill-status.json` に書く（`backup.sh` はこのファイルをバックアップに入れない）。手で実行したときも書く。
  - 管理画面の「バックアップ」に結果を出し、警告にする: 記録が無い（info）、失敗（error）、空きが足りず飛ばした（warning）、40 日以上成功していない（warning）。警告なので、通知（6.1）が設定されていれば知らせる。指標（6.2）は `docseed_backup_last_success_timestamp_seconds{kind="restore_drill"}` と `docseed_restore_drill_ok`。

Phase 5 では開発環境で、アップロード → `backup.sh`（restic のローカルリポジトリ）→ `restore-drill.sh` が成功すること、復元した原本を 1 件書き換え・1 件削除すると `verify-files` が検出して失敗することを確かめた。Pi 実機（USB ドライブへのバックアップ）での練習は、導入時に利用者の手元で行う。自動の練習は、本物の restic で `backup.sh` → `restore-drill.sh --auto` を動かすテスト（`tests/test_drill.py`。成功・空きが足りず飛ばす・原本の書き換えで失敗・restic の設定が無い）で確かめる（CI に restic を入れている）。

## 6. 監視・ログ

| 項目 | 方法 |
|---|---|
| ログ | 1 行 1 件の JSON（`time`, `level`, `logger`, `msg`, 例外は `exc`）で標準出力 → Docker の json-file ドライバ（`max-size: 10m`, `max-file: 5`）。Uvicorn のアクセスログは Uvicorn の形式のまま |
| 死活 | `/healthz`。Docker の healthcheck に設定（Dockerfile） |
| worker の生存 | worker が 30 秒ごとに `settings.worker.heartbeat` を更新。2 分以上止まっていたら警告（error） |
| ジョブの失敗 | 失敗したジョブがあれば警告（warning） |
| 温度 | 管理画面に表示。`DOCSEED_THERMAL_WARN_C`（既定 75℃）以上で「高め」の警告、`DOCSEED_THERMAL_PAUSE_C`（既定 80℃）以上で一時停止の警告。`vcgencmd get_throttled` 相当のスロットリング履歴はホストで確認 |
| メモリ | `/proc/meminfo` の MemAvailable が `DOCSEED_MEMORY_WARN_PCT`（既定 10%）を下回ったら警告（warning） |
| ディスク | 空き容量が 10% を切ったら警告（warning）。5% を切ったら警告（error）し、アップロードを拒否（507） |
| バックアップ | DB スナップショットが無い・36 時間以上新しくない、restic の記録が無い（info）・失敗（error）・36 時間以上成功していない（warning） |
| SSD の状態 | `smartctl` / `nvme smart-log` を週 1 回ホストで実行（任意） |

- 警告は `docseed.monitor.status()` で一か所にまとめ、管理画面の「確認が必要な項目」、左メニューの「管理」のバッジ（error と warning の件数）、`GET /api/v1/admin/status`、`docseed status`（error があれば終了コード 1）で同じものを見せる。
- 管理画面の「システム」と `GET /api/v1/admin/system`（`monitor.system_info()`）に、CPU 温度・負荷（1・5・15 分）・コア数、メモリ（機械全体）、SSD、DB の大きさ、件数（ファイル・ゴミ箱・過去の版・フォルダ・ユーザー・実体・ページ・チャンク）、過去の版だけが使っている容量、モデル、版（docseed・スキーマ・Python）、起動からの時間を出す。

### 6.1 通知

| 項目 | 内容 |
|---|---|
| 宛先 | `DOCSEED_NOTIFY_URLS`（Webhook。カンマか空白区切り）と、`DOCSEED_SMTP_HOST` + `DOCSEED_NOTIFY_EMAIL`（メール）。どちらも無ければ通知の仕組みは動かない |
| Webhook の形式 | `ntfy`（`https://host/topic` を `https://host/` への JSON 発行にする。priority は error 5 / warning 4 / info 3）、`discord`（`{"content"}`、2000 字まで）、`slack`（`{"text"}`）、`json`（`{"source", "level", "title", "message", "text"}`）。`ntfy+https://...` のように形式を前に付けられ、無ければ discord.com / hooks.slack.com / ntfy.sh をホスト名で見分け、それ以外は json |
| 知らせること | `DOCSEED_NOTIFY_EVENTS`（既定 `alert,share_upload,inbox`）。`alert`: `monitor.status()` の warning 以上。`share_upload`: 受け取り用の共有リンクからの受け取り。`inbox`: 受け取り箱の取り込み・取り込みの失敗 |
| 警告の知らせ方 | 5 分ごとに `status()` を確かめ、前回の状態（`settings` の `notify.alerts`）と比べる。2 回続けて出たら知らせる（worker の再起動などの一時的なものを除く）。知らせた警告が重くなったら知らせ直し、続いていれば 24 時間ごとに知らせ直す。知らせた警告が消えたら「解消」を知らせる |
| 送り方 | 知らせることは `notifications` 表（0009）に積み、api の中のスレッドが 1 分ごとに送る。worker（外部と通信しない）は積むだけ。同じ `group_key`（共有リンクごと・受け取り箱のユーザーごと）の送信待ちは 1 通にまとめる。一部の宛先だけ失敗したら送ったことにしてエラーを残し、すべて失敗したら 1 分おきに 5 回まで送り直して `failed` にする。送った記録は 30 日で消す |
| 確認 | 管理画面の「通知」（宛先は形式とホスト名だけを出す。Webhook の URL は秘密を含むため）、「テスト通知を送る」（`POST /api/v1/admin/notify/test`。積んですぐ送る）、最近の通知（`GET /api/v1/admin/notifications`） |

- ユーザーごとの宛先（自分の共有リンクの受け取りだけを自分に送る）は持たない。宛先は管理者が `.env` で決める。

### 6.2 指標（Prometheus）

- `GET /api/v1/admin/metrics` が Prometheus のテキスト形式（`text/plain; version=0.0.4`）で返す（`docseed.metrics`。依存は増やさず自前で書く）。管理者のセッションか、**管理者の API トークン（read）** で呼べる（`deps.require_admin_read`。`/admin/status`・`/admin/system` も同じ。ほかの管理の操作はこれまでどおりブラウザのセッションだけ）。
- 出すもの: 版（`docseed_info`）、文書・過去の版・フォルダ・ページ・チャンク・実体の数と大きさ、ジョブの数（type・status ごと）といちばん古い処理待ちの待ち時間、直近 24 時間に終わったジョブの平均・最大の所要時間（OCR と埋め込みは `process_blob`）、worker の生存と最終応答、警告の数（level ごと）と出ている警告（code）、CPU 温度・負荷・メモリ・SSD・DB の大きさ、バックアップの最終成功時刻（DB スナップショット・restic）、証明書の期限、ユーザーごとの使用量・上限・有効か、通知の数、検索の時間のヒストグラム（`docseed_search_duration_seconds`、mode ごと。api の中で数えるので再起動で 0 に戻る）。
- 取得のたびに少しの集計（SELECT）を行う。取得の間隔は 30 秒〜1 分を想定。
- Prometheus の設定の例（Pi の外の Prometheus から）:

```yaml
scrape_configs:
  - job_name: docseed
    scheme: https
    metrics_path: /api/v1/admin/metrics
    authorization:
      credentials_file: /etc/prometheus/docseed-token   # 管理者の API トークン（read）
    static_configs:
      - targets: ["docs.example.com"]
```

## 7. 更新手順

1. バックアップを手動で実行する（`sudo systemctl start docseed-backup.service`）。
2. `git pull` → `docker compose build`（または新しいイメージを pull）。
3. `docker compose up -d`。api が起動時にマイグレーションを適用する。worker は api のマイグレーション完了（`schema_migrations` の版）を待ってから処理を始める。
4. パイプラインの版や埋め込みモデルが変わった場合は、管理画面から一括再処理を実行する（優先度が低いので通常の利用は妨げない）。
