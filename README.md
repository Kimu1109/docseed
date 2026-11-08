# docseed

Raspberry Pi 5 で動くローカルのドキュメントサーバーです。ファイルの保存・共有、OCR（PP-OCRv6）、日本語埋め込み（ruri-v3-70m）によるハイブリッド検索を、すべて手元の機械で行います。

| 段階 | 内容 | 状態 |
|---|---|---|
| Phase 0 | 実機での計測（OCR・埋め込み・検索） | 完了 |
| Phase 1 | ログイン、フォルダ、アップロード / ダウンロード、ゴミ箱、ユーザー管理、画面 | 完了 |
| Phase 2 | OCR・埋め込みの処理（worker） | 完了 |
| Phase 3 | 検索 | 完了 |
| Phase 4 | 共有（フォルダのメンバー、共有リンク、受け取り箱） | 完了 |
| Phase 5 | 運用（バックアップ、監視、Office 文書） | これから |

## Raspberry Pi で動かす（Docker）

前提: Raspberry Pi OS 64-bit、Docker Engine と Compose プラグイン、SSD 上のデータディレクトリ。

```bash
git clone https://github.com/kimu1109/docseed.git && cd docseed
sudo mkdir -p /srv/docseed && sudo chown 1000:1000 /srv/docseed

cp .env.example .env
# .env を編集: DOCSEED_HOST と DOCSEED_SECRET_KEY は必須
#   秘密鍵の生成: python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# モデルの取得（初回だけ。OCR と埋め込みのモデルを /srv/docseed/models に置く。10〜20 分）
docker compose --profile tools run --rm --build tools

docker compose up -d --build
docker compose exec api docseed create-admin --username <名前>   # パスワードは対話入力
```

### 文字の読み取りを有効にする（Phase 1 から更新する場合）

Phase 1 の構成から更新するときは、モデルの取得と worker の起動が必要です。

```bash
git pull
docker compose --profile tools run --rm --build tools   # モデルの取得（初回だけ）
docker compose up -d --build                            # api の更新と worker の起動
docker compose logs -f worker                           # 処理の様子（Ctrl+C で抜ける）
```

既にアップロードしたファイルは「処理待ち」のままになっているので、worker が起動すると順に処理されます。進み具合は管理画面の「文字の読み取りと検索の準備」で確認できます。

- `tools` イメージは、モデルを ONNX 形式に変換するための torch を含むので大きめです（変換が終われば削除して構いません: `docker image rm docseed-tools`）
- worker は外部と通信しない設定です。モデルが無いと起動直後のジョブが失敗するので、先に `tools` を実行してください

ブラウザで `https://<DOCSEED_HOST>/` を開きます。Caddy の内部 CA の証明書を使うため、初回はブラウザの警告が出ます（端末に CA を入れるか、Tailscale の名前を使えば解消できます。`docker/Caddyfile` を参照）。

### うまく起動しないとき

| 症状 | 対処 |
|---|---|
| `permission denied ... docker.sock` | `sudo usermod -aG docker $USER` のあと、ログインし直す |
| `failed to bind host port 0.0.0.0:80`（または `:443`）`: address already in use` | 他のサービスがポートを使っています。`sudo ss -ltnp 'sport = :80'` で確認し、そのサービスを止めるか、`.env` の `DOCSEED_HTTP_PORT` / `DOCSEED_HTTPS_PORT` を空いている番号に変えて `docker compose up -d` |
| ファイルが「処理に失敗」になり、ジョブのエラーに「モデルがありません」と出る | `docker compose --profile tools run --rm tools` でモデルを取得し、管理画面の「失敗したファイルをすべて再処理」 |
| `PermissionError: ... '/srv/docseed/...'` または「データディレクトリに書き込めません」 | コンテナは UID 1000 で動くため、データディレクトリの所有者を合わせる: `sudo chown -R 1000:1000 /srv/docseed`（`.env` の `DOCSEED_DATA_DIR` を変えている場合はその場所） |
| ポートを変えたのに、開くと Apache など 80 / 443 の別のサービスに飛ぶ・IP アドレスで開けない | `git pull` で最新にし（Caddy が中でも同じポートで待ち受ける設定）、IP で開くなら `.env` の `DOCSEED_LAN_IP` に Pi の IP を設定して `docker compose up -d --force-recreate caddy` |
| `api` がすぐ止まる | `docker compose logs api` を確認。`.env` の `DOCSEED_SECRET_KEY` 未設定や、`/srv/docseed` の所有者（UID 1000 が必要）が原因のことが多い |

## 検索

画面上部の検索欄（`/` キーで移動）か、左のメニューの「検索」から使います。語が一致する部分と、意味が近い部分の両方を探します。

| 書き方 | 意味 |
|---|---|
| `請求書 振込先` | 両方の語を含む部分と、意味が近い部分 |
| `"株式会社サンプル"` | ダブルクォートの語を必ず含むものだけ |
| `見積書 -下書き` | `-` を付けた語を含むものを除く |
| `type:pdf tag:経理 after:2026-01-01 before:2026-06-30` | 種類・タグ・更新日で絞り込む |

### 検索の品質を確かめる・調整する

自分の文書に対する「質問」と「正解のファイル名」を 20〜30 件ほど JSON に書き、`scripts/eval_search.py` で Recall@5 / MRR を測れます（使い方はスクリプト冒頭のコメント）。
意味検索で無関係なファイルが多く出る・逆に見つからない場合は、`--sem-min 0.75,0.80,0.85` で類似度の下限を比べ、良かった値を `.env` の `DOCSEED_SEARCH_SEM_MIN` に設定します（既定 0.80）。

## 共有

フォルダやファイルの画面の「共有」から設定します。

| 相手 | 方法 | できること |
|---|---|---|
| docseed のほかのユーザー | フォルダの「共有」→ ユーザーを追加 | 「閲覧のみ」か「編集できる」。下のフォルダにも引き継がれます。追加・変更は所有者だけ |
| docseed のアカウントが無い人 | 「共有」→ リンクを作成 | 閲覧・ダウンロード用、またはフォルダへファイルを送ってもらう受け取り用。有効期限（既定 7 日）・パスワード・ダウンロード回数の上限を付けられます |

- リンクはいつでも「無効にする」で止められます。自分が作ったリンクの一覧は「設定」にあります
- リンクを作った人がそのフォルダの権限を失うと、そのリンクは自動的に使えなくなります
- フォルダの「ZIP」で、中身をまとめてダウンロードできます（2GB まで）
- 外部の人にリンクを渡すには、docseed にインターネットから届く必要があります。Tailscale を使う場合は Tailscale Funnel などで公開範囲を慎重に決めてください

## 受け取り箱（LAN からファイルを放り込む）

`/srv/docseed/inbox/<ユーザー名>/` に置いたファイルが、そのユーザーの「Inbox」フォルダに自動で取り込まれます（30 秒ごと。サブフォルダの構造も保ちます）。スキャナーの保存先や PC からのドラッグ＆ドロップに使えます。

1. `.env` で有効にして worker を再起動する
   ```bash
   # .env
   DOCSEED_INBOX_ENABLED=true
   docker compose up -d worker
   ```
   有効にすると、ユーザーごとのフォルダ（`/srv/docseed/inbox/<ユーザー名>/`）が作られます。

2. Pi の Samba で LAN に公開する（Pi 本体に入れます）
   ```bash
   sudo apt install -y samba
   getent passwd 1000 | cut -d: -f1       # UID 1000 のユーザー名（多くは最初に作ったユーザー）を確認
   sudo smbpasswd -a <そのユーザー名>      # LAN から接続するときのパスワードを設定
   ```
   `/etc/samba/smb.conf` の末尾に、使う人ごとに追記します（`kimu` は docseed のユーザー名、`<UID1000のユーザー名>` は上で確認した名前）。
   ```ini
   [docseed-kimu]
      path = /srv/docseed/inbox/kimu
      read only = no
      valid users = <UID1000のユーザー名>
      force user = <UID1000のユーザー名>
      create mask = 0644
      directory mask = 0755
   ```
   ```bash
   sudo systemctl restart smbd
   sudo ufw allow from 192.168.0.0/16 to any port 445 proto tcp   # ufw を使っている場合（LAN の範囲に合わせる）
   ```
   Windows なら `\\<Pi の IP アドレス>\docseed-kimu`、Mac なら Finder の「サーバへ接続」で `smb://<Pi の IP アドレス>/docseed-kimu` を開きます。

- 書き込み中のファイルを避けるため、置いてから 30〜60 秒ほどで取り込まれます
- 取り込んだファイルは受け取り箱から消えます（`DOCSEED_INBOX_KEEP=true` なら `.imported/` に残す）
- 取り込めなかったファイルは `.failed/` に移り、理由が `<ファイル名>.error.txt` に書かれます
- `force user` でファイルの持ち主を UID 1000 に合わせているので、worker が読み取り・削除できます

## 開発

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests

# 手元で起動（HTTP で試すので Cookie の Secure を外す）
export DOCSEED_DATA_DIR=./data DOCSEED_SECRET_KEY=dev-secret-key-change-me-0123456789 DOCSEED_COOKIE_SECURE=false
docseed create-admin --username admin
docseed serve --port 8000
```

API の仕様は起動後に `/api/docs` で確認できます（ログインが必要）。

## コマンド

| コマンド | 内容 |
|---|---|
| `docseed serve` | Web サーバーを起動（起動時にマイグレーションを適用） |
| `docseed migrate` | マイグレーションだけを適用 |
| `docseed create-admin --username NAME` | 管理者を作成 |
| `docseed purge-trash` | 保持期間を過ぎたゴミ箱の項目を完全に削除 |
| `docseed backup-db` | DB のスナップショットを `backup/` に作成（7 世代） |
| `docseed worker` | 文字の読み取り（OCR）と埋め込みの処理を行う |
| `docseed fetch-models` | OCR と埋め込みのモデルを取得する（`.[tools]` が必要） |
| `docseed reprocess failed\|outdated\|all` | 文書を再処理する（`--force-ocr` で PDF も画像として読み取る） |

## 文書

- [設計書](docs/design.md)（詳細設計は `docs/design/`）
- [ローカル OCR / Embedding モデル調査](docs/research/ocr-embedding-models.md)
- [Phase 0 計測結果](docs/research/phase0-results.md)
