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
├── models/       PP-OCRv5 ONNX、ruri-v3-70m
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

  samba:                                    # 任意（profiles で有効化）
    profiles: ["samba"]
    image: <arm64 対応の Samba イメージ>
    ports: ["445:445"]
    volumes:
      - /srv/docseed/inbox:/shares/inbox
    restart: unless-stopped

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
  ├ apt: libgl1 など OpenCV の最小依存（RapidOCR 等が必要とする場合）、tini
  ├ pip: 本体 + onnxruntime + sentence-transformers + torch（CPU 版）+ sqlite-vec + pypdfium2 + Pillow ...
  ├ 非 root ユーザー docseed（UID 1000）で実行
  └ ENTRYPOINT ["tini", "--"]
```

- 依存は `uv` でロックファイル（`uv.lock`）から入れて再現性を保つ。
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

- 証明書: Tailscale の名前（`*.ts.net`）を使う場合は Tailscale の証明書、LAN のみの場合は Caddy の内部 CA（`tls internal`）を使い、利用する端末に CA 証明書を入れる。
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
| `DOCSEED_OCR_ENGINE` | `ppocrv5_mobile` | OCR エンジン |
| `DOCSEED_OCR_DPI` | `200` | PDF のページ画像化の解像度 |
| `DOCSEED_OCR_THREADS` | `3` | ONNX Runtime のスレッド数 |
| `DOCSEED_EMBED_MODEL` | `cl-nagoya/ruri-v3-70m` | 埋め込みモデル |
| `DOCSEED_EMBED_BACKEND` | `torch` | `torch` または `onnx` |
| `DOCSEED_EMBED_THREADS` | `3`（worker）/ `1`（api） | |
| `DOCSEED_THERMAL_PAUSE_C` | `80` | worker が一時停止する CPU 温度 |
| `DOCSEED_INBOX_ENABLED` | `false` | inbox 取り込みの有効化 |
| `DOCSEED_LOG_LEVEL` | `INFO` | |

## 4. 初期セットアップ手順（予定）

```bash
# 1. データディレクトリ
sudo mkdir -p /srv/docseed && sudo chown 1000:1000 /srv/docseed

# 2. 設定
cp .env.example .env && $EDITOR .env      # DOCSEED_HOST, DOCSEED_SECRET_KEY を設定

# 3. モデルの取得（チェックサムを確認して /srv/docseed/models に配置）
docker compose run --rm api docseed fetch-models

# 4. 起動
docker compose up -d

# 5. 管理者の作成
docker compose exec api docseed create-admin --username kimu
```

## 5. バックアップと復旧

### 5.1 対象と方法

| 対象 | 方法 | 頻度 |
|---|---|---|
| DB | `docseed backup-db` で SQLite のオンラインバックアップ API を使って `backup/docseed-YYYYMMDD.sqlite` を作成（7 日分保持） | 毎日 3:00 |
| 原本 `files/` と DB スナップショット | restic で外付け USB ドライブ、または別のマシン（SFTP / REST サーバー）へ | 毎日 3:30 |
| `.env`、compose.yaml | restic に含める（`.env` は restic のリポジトリ暗号化で保護） | 毎日 |
| 対象外 | `derived/`、`models/`（再生成・再取得できる）、`-wal`/`-shm`（スナップショットで代替） | — |

- restic の保持: 日次 7、週次 4、月次 6。
- ホストの systemd タイマーで実行する（コンテナ内の cron は使わない）。
- 週 1 回 `restic check` を実行する。

### 5.2 復旧手順

1. 新しい SSD に OS と Docker を入れ、リポジトリを取得する。
2. restic で `files/`、DB スナップショット、`.env` を復元する。
3. DB スナップショットを `db/docseed.sqlite` に配置する。
4. `docseed fetch-models` でモデルを再取得する。
5. `docker compose up -d`。サムネイルは表示時に再生成される。
6. 動作確認: ログイン、検索、ダウンロード。`docseed verify-files` で DB と原本ファイルの対応（存在・SHA-256）を確認する。

最後のバックアップ以降にアップロードされたファイルは失われる。Phase 5 で実際に復旧手順を試す。

## 6. 監視・ログ

| 項目 | 方法 |
|---|---|
| ログ | JSON 形式で標準出力 → Docker の json-file ドライバ（`max-size: 10m`, `max-file: 5`） |
| 死活 | `/healthz`。Docker の healthcheck に設定 |
| worker の生存 | worker が 30 秒ごとに `settings.worker.heartbeat` を更新。2 分以上止まっていたら管理画面に警告 |
| ジョブの滞留・失敗 | 管理画面に件数を表示。失敗が出たらヘッダに管理者向けの警告アイコン |
| 温度 | 管理画面に表示。`vcgencmd get_throttled` 相当のスロットリング履歴もホスト側スクリプトで確認 |
| ディスク | 空き容量が 10% を切ったら管理画面に警告。5% を切ったらアップロードを拒否（507） |
| SSD の状態 | `smartctl` / `nvme smart-log` を週 1 回ホストで実行（任意） |

外部の監視サービスへの通知は初版では行わない（必要になったら ntfy 等への通知を追加）。

## 7. 更新手順

1. `restic backup` を手動で実行する。
2. `git pull` → `docker compose build`（または新しいイメージを pull）。
3. `docker compose up -d`。api が起動時にマイグレーションを適用する。worker は api のマイグレーション完了（`schema_migrations` の版）を待ってから処理を始める。
4. パイプラインの版や埋め込みモデルが変わった場合は、管理画面から一括再処理を実行する（優先度が低いので通常の利用は妨げない）。
