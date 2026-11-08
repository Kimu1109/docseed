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
| `DOCSEED_DATA_DIR` | `/srv/docseed` | データディレクトリ |
| `DOCSEED_SECRET_KEY` | — | Cookie 署名用。32 バイト以上の乱数（必須） |
| `DOCSEED_MAX_UPLOAD_MB` | `1024` | 1 ファイルの上限 |
| `DOCSEED_TRASH_RETENTION_DAYS` | `30` | ゴミ箱の保持日数 |
| `DOCSEED_OCR_ENGINE` | `ppocrv6_small` | OCR エンジン（`ppocrv6_small` / `ppocrv6_medium`） |
| `DOCSEED_OCR_DPI` | `200` | PDF のページ画像化の解像度 |
| `DOCSEED_OCR_THREADS` | `3` | ONNX Runtime のスレッド数 |
| `DOCSEED_EMBED_MODEL` | `cl-nagoya/ruri-v3-70m` | 埋め込みモデル |
| `DOCSEED_EMBED_BACKEND` | `onnx` | `onnx`（fp32）、`onnx-int8`、`torch` |
| `DOCSEED_EMBED_THREADS` | `3`（worker）/ `1`（api） | |
| `DOCSEED_THERMAL_PAUSE_C` | `80` | worker が一時停止する CPU 温度 |
| `DOCSEED_INBOX_ENABLED` | `false` | inbox 取り込みの有効化 |
| `DOCSEED_LOG_LEVEL` | `INFO` | |

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

`sudo scripts/ops/restore-drill.sh` で、動いている docseed に触れずに 5.2 の 2・3・6 を試せる。restic の最新スナップショットを一時ディレクトリ（`/var/tmp/docseed-drill.*`）に復元し、そこで `restore-db` と `verify-files` を実行し、終わったら消す（`--keep` で残す）。原本と同じだけの空き容量が必要なので、定期実行はせず、設定を変えたときと数か月に 1 度、手で実行する。

Phase 5 では開発環境で、アップロード → `backup.sh`（restic のローカルリポジトリ）→ `restore-drill.sh` が成功すること、復元した原本を 1 件書き換え・1 件削除すると `verify-files` が検出して失敗することを確かめた。Pi 実機（USB ドライブへのバックアップ）での練習は、導入時に利用者の手元で行う。

## 6. 監視・ログ

| 項目 | 方法 |
|---|---|
| ログ | 1 行 1 件の JSON（`time`, `level`, `logger`, `msg`, 例外は `exc`）で標準出力 → Docker の json-file ドライバ（`max-size: 10m`, `max-file: 5`）。Uvicorn のアクセスログは Uvicorn の形式のまま |
| 死活 | `/healthz`。Docker の healthcheck に設定（Dockerfile） |
| worker の生存 | worker が 30 秒ごとに `settings.worker.heartbeat` を更新。2 分以上止まっていたら警告（error） |
| ジョブの失敗 | 失敗したジョブがあれば警告（warning） |
| 温度 | 管理画面に表示。`DOCSEED_THERMAL_PAUSE_C`（既定 80℃）以上なら警告。`vcgencmd get_throttled` 相当のスロットリング履歴はホストで確認 |
| ディスク | 空き容量が 10% を切ったら警告（warning）。5% を切ったら警告（error）し、アップロードを拒否（507） |
| バックアップ | DB スナップショットが無い・36 時間以上新しくない、restic の記録が無い（info）・失敗（error）・36 時間以上成功していない（warning） |
| SSD の状態 | `smartctl` / `nvme smart-log` を週 1 回ホストで実行（任意） |

- 警告は `docseed.monitor.status()` で一か所にまとめ、管理画面の「確認が必要な項目」、左メニューの「管理」のバッジ（error と warning の件数）、`GET /api/v1/admin/status`、`docseed status`（error があれば終了コード 1）で同じものを見せる。
- 外部の監視サービスへの通知は初版では行わない。必要になったら、API トークンで `/api/v1/admin/status` を定期的に取得し、ntfy 等へ送るスクリプトをホストに置く。

## 7. 更新手順

1. バックアップを手動で実行する（`sudo systemctl start docseed-backup.service`）。
2. `git pull` → `docker compose build`（または新しいイメージを pull）。
3. `docker compose up -d`。api が起動時にマイグレーションを適用する。worker は api のマイグレーション完了（`schema_migrations` の版）を待ってから処理を始める。
4. パイプラインの版や埋め込みモデルが変わった場合は、管理画面から一括再処理を実行する（優先度が低いので通常の利用は妨げない）。
