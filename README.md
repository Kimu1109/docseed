# docseed

Raspberry Pi 5 で動くローカルのドキュメントサーバーです。ファイルの保存・共有、OCR（PP-OCRv6）、日本語埋め込み（ruri-v3-70m）によるハイブリッド検索を、すべて手元の機械で行います。

| 段階 | 内容 | 状態 |
|---|---|---|
| Phase 0 | 実機での計測（OCR・埋め込み・検索） | 完了 |
| Phase 1 | ログイン、フォルダ、アップロード / ダウンロード、ゴミ箱、ユーザー管理、画面 | 完了 |
| Phase 2 | OCR・埋め込みの処理（worker） | 完了 |
| Phase 3 | 検索 | 完了 |
| Phase 4 | 共有（フォルダのメンバー、共有リンク、受け取り箱） | 完了 |
| Phase 5 | 運用（バックアップ、監視、Office 文書） | 実装済み（Pi での復旧の練習は導入時に） |
| Phase 6 | HTTPS 証明書の方式の切り替え・アイコン・HEIC など | 一部実装（手元の証明書ファイルの指定） |
| Phase 7 | ログイン（パスキー・確認コード・復旧コード・Google などの OIDC・再認証） | 実装済み |

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

ブラウザで `https://<DOCSEED_HOST>/` を開きます。既定では Caddy の内部 CA の証明書を使うため、初回はブラウザの警告が出ます（端末に CA を入れれば消えます）。Let's Encrypt などの証明書を持っている場合は、次の「手元の証明書を使う」でその証明書を使えます。

### 手元の証明書を使う（certbot の Let's Encrypt の証明書など）

Pi に certbot などで取った証明書があれば、Caddy にそのファイルを使わせられます。ブラウザの警告が出なくなり、インターネットから届かない環境（LAN や Tailscale 経由だけ）でも使えます。

```bash
# .env
DOCSEED_TLS_MODE=file
DOCSEED_HOST=example.com                           # 証明書に含まれる名前（sudo certbot certificates の Domains）
DOCSEED_TLS_CERT_DIR=/etc/letsencrypt              # live/ の中は archive/ へのリンクなので、ディレクトリごと渡す
DOCSEED_TLS_CERT=live/example.com/fullchain.pem    # DOCSEED_TLS_CERT_DIR からの相対パス
DOCSEED_TLS_KEY=live/example.com/privkey.pem
```

```bash
docker compose up -d --force-recreate caddy
docker compose logs caddy | tail -n 20              # 証明書の読み込みに失敗するとここにエラーが出る
sudo scripts/ops/install-cert-hook.sh               # certbot の更新のたびに Caddy を再起動して新しい証明書を読ませる
```

- **証明書の名前で開く**必要があります（`https://example.com:<DOCSEED_HTTPS_PORT>/`）。Tailscale の名前（`*.ts.net`）や IP アドレスで開くと、名前が合わないためブラウザが拒否します
- その名前が、使う端末から Pi に引けるようにします。例: DNS で `example.com`（またはサブドメイン）の A レコードを Pi の Tailscale の IP（`100.x.y.z`。`tailscale ip -4` で確認）か LAN の IP にする、または端末の hosts ファイルに書く
- IP アドレス（`DOCSEED_LAN_IP`）で開いたときは、今まで通り内部 CA の証明書になります
- Caddy は証明書ファイルの変更を監視しないので、更新後の再起動（上の `install-cert-hook.sh`）が必要です。certbot の更新がインターネットからの接続（HTTP-01）を使う場合、WAN から届かないと更新に失敗します。`sudo certbot renew --dry-run` で確かめ、失敗するなら DNS-01 での取得に切り替えてください

### Phase 4 から更新する場合（Office 文書・バックアップ）

```bash
git pull
docker compose up -d --build
docker compose exec api docseed reprocess unsupported   # 以前に保存した Word / Excel / PowerPoint を処理する
```

続けて、下の「バックアップと復旧」の設定を行ってください。

### うまく起動しないとき

| 症状 | 対処 |
|---|---|
| `permission denied ... docker.sock` | `sudo usermod -aG docker $USER` のあと、ログインし直す |
| `failed to bind host port 0.0.0.0:80`（または `:443`）`: address already in use` | 他のサービスがポートを使っています。`sudo ss -ltnp 'sport = :80'` で確認し、そのサービスを止めるか、`.env` の `DOCSEED_HTTP_PORT` / `DOCSEED_HTTPS_PORT` を空いている番号に変えて `docker compose up -d` |
| ファイルが「処理に失敗」になり、ジョブのエラーに「モデルがありません」と出る | `docker compose --profile tools run --rm tools` でモデルを取得し、管理画面の「失敗したファイルをすべて再処理」 |
| `PermissionError: ... '/srv/docseed/...'` または「データディレクトリに書き込めません」 | コンテナは UID 1000 で動くため、データディレクトリの所有者を合わせる: `sudo chown -R 1000:1000 /srv/docseed`（`.env` の `DOCSEED_DATA_DIR` を変えている場合はその場所） |
| ポートを変えたのに、開くと Apache など 80 / 443 の別のサービスに飛ぶ・IP アドレスで開けない | `git pull` で最新にし（Caddy が中でも同じポートで待ち受ける設定）、IP で開くなら `.env` の `DOCSEED_LAN_IP` に Pi の IP を設定して `docker compose up -d --force-recreate caddy` |
| 証明書の警告で先に進めない（「この接続ではプライバシーが保護されません」で例外を追加するボタンが出ない） | 開いた名前と証明書が合っていません。そのドメインに HSTS（以前に Let's Encrypt の証明書で開いたなど）が付いていると、ブラウザは例外を許しません。上の「手元の証明書を使う」でその名前の証明書を Caddy に使わせてください |
| `caddy` が起動しない・`loading certificates` や `open /certs/...: no such file` のエラー | `.env` の `DOCSEED_TLS_CERT_DIR` と、その中の相対パス `DOCSEED_TLS_CERT` / `DOCSEED_TLS_KEY` を確認（`sudo ls -l /etc/letsencrypt/live/<名前>/`）。`DOCSEED_TLS_MODE` は `internal` か `file` |
| `api` がすぐ止まる | `docker compose logs api` を確認。`.env` の `DOCSEED_SECRET_KEY` 未設定や、`/srv/docseed` の所有者（UID 1000 が必要）が原因のことが多い |

## ログイン方法（パスキー・確認コード・Google など）

右上のメニュー →「設定」→「セキュリティ（ログイン方法）」で、ユーザーごとに設定します。変更の前に、パスワード（＋確認コード）かパスキーでもう一度本人確認をします（10 分間有効）。

| 方法 | 内容 |
|---|---|
| パスキー | 端末の指紋・顔認証や PIN でログイン。ログイン画面の「パスキーでログイン」か、ユーザー名の欄の自動入力の候補から選ぶ |
| 確認コード（2 段階認証） | パスワードの後に、認証アプリ（Google Authenticator など）の 6 桁のコードを入力。有効にしたときに表示される**復旧コード 10 個**を控えておく（端末をなくしたとき、1 つずつ 1 回だけ使える） |
| 外部アカウント（OIDC） | 設定で連携した Google などのアカウントでログイン。連携していないアカウントではログインできない（メールアドレスが同じでも自動では結び付けない） |
| パスワードでのログインを止める | パスキーか外部アカウントを 1 つ以上設定すると、パスワードではログインできないようにできる |

管理画面の「ログイン」で、パスワードでログインする人に**確認コードを必須**にできます（未設定の人はログイン直後に設定画面へ移ります）。ユーザーがすべての方法を失ったときは、管理画面のユーザーの行の「ログイン方法をリセット」で仮のパスワードを発行します。管理者自身が失ったときは Pi で次を実行します。

```bash
docker compose exec api docseed reset-auth --username <名前>
```

### パスキーを使うための条件

- **`DOCSEED_HOST` の名前（ドメイン名）で、信頼された証明書の HTTPS で開く**必要があります（パスキーは名前に結び付くため、IP アドレスで開くと使えません。内部 CA の証明書は、端末に CA 証明書を入れていれば使えます）。上の「手元の証明書を使う」か Tailscale の名前が便利です
- パスキーは `DOCSEED_HOST` の最初の名前（`DOCSEED_RP_ID` で変更可）に登録されます。**この名前を後から変えると、登録済みのパスキーはすべて使えなくなります**（管理画面に警告が出ます）
- `DOCSEED_HTTPS_PORT` を 443 以外にしている場合も、そのポート付きの URL で開けば使えます

### Google でログインする

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作り、「API とサービス」→「OAuth 同意画面」で種類を「外部」にして、範囲（スコープ）は `openid`・`email`・`profile` だけにします
2. 公開ステータスが「テスト」のままなら、使う人の Google アカウントを「テストユーザー」に登録します（最大 100 人）。この 3 つの範囲だけなら「本番環境」に公開することもできます（Google の規則は変わることがあるので、画面の案内に従ってください）
3. 「認証情報」→「OAuth クライアント ID を作成」→ 種類「ウェブ アプリケーション」。「承認済みのリダイレクト URI」に `https://<DOCSEED_HOST>[:<DOCSEED_HTTPS_PORT>]/auth/oidc/google/callback` を登録します。Google は HTTPS と公開されたドメイン名（`*.ts.net` や自分のドメイン）を求めるので、`.local` や IP アドレスでは使えません（Pi がインターネットから届く必要はありません）
4. `.env` に書いて `docker compose up -d` で api を作り直します

```bash
DOCSEED_OIDC_PROVIDERS=google
DOCSEED_OIDC_GOOGLE_ISSUER=https://accounts.google.com
DOCSEED_OIDC_GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
DOCSEED_OIDC_GOOGLE_CLIENT_SECRET=xxxxxxxx
DOCSEED_OIDC_GOOGLE_LABEL=Google
```

5. 各ユーザーが一度パスワードなどでログインし、「セキュリティ」→「Google と連携」で自分のアカウントを結び付けます。管理画面で、連携できるメールアドレスのドメインを絞れます（例 `example.com`）

Pocket ID・Authelia・Keycloak など、ほかの OIDC の提供者も同じ書き方で足せます（`DOCSEED_OIDC_PROVIDERS=google,pocketid` と、`DOCSEED_OIDC_POCKETID_ISSUER` など）。

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

## Office 文書

Word（docx）・Excel（xlsx）・PowerPoint（pptx）の文字も検索できます（マクロ付きの docm / xlsm / pptm も同じ）。

| 形式 | 取り出し方 |
|---|---|
| Word | 本文と表。見出し 1・2 ごとに区切って表示します |
| Excel | シートごと。セルの値（数式は計算結果）。1 シート 100 万セルまで |
| PowerPoint | スライドごと。図形・表・ノート |

- 古い形式（doc / xls / ppt）とパスワード付きのファイルは、保存・共有はできますが文字は取り出しません
- 画像の中の文字（貼り付けたスクリーンショットなど）は読み取りません。必要なら PDF に変換してアップロードしてください

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

## バックアップと復旧

毎日 3:00 に、DB のスナップショットを作ってから、原本（`files/`）・DB のスナップショット・`.env` を restic で別の機器（外付け USB ドライブや NAS）へ送ります。Pi 本体の systemd タイマーで動きます。SSD は 1 台なので、**必ず別の機器に**取ってください。

1. 保存先を用意する（例: 外付け USB ドライブを ext4 でフォーマットし、`/etc/fstab` で `/mnt/backup` にマウント）
2. 設定する
   ```bash
   sudo apt install -y restic
   sudo scripts/ops/install-backup.sh        # タイマーの登録、/etc/docseed/backup.env とパスワードの生成
   sudo nano /etc/docseed/backup.env         # RESTIC_REPOSITORY を保存先に合わせる（NAS なら sftp: / rest:）
   sudo bash -c 'set -a; . /etc/docseed/backup.env; restic init'
   sudo cat /etc/docseed/restic-password     # 紙やパスワード管理ツールに控える（失うと復元できません）
   ```
3. 試しに実行して、復旧できることを確かめる
   ```bash
   sudo systemctl start docseed-backup.service && journalctl -u docseed-backup -n 30
   sudo scripts/ops/restore-drill.sh         # 最新のバックアップを一時ディレクトリに復元し、原本と DB の対応を確認
   ```
   「成功: … から復旧できることを確認しました」と出れば完了です。管理画面の「バックアップ」に最終結果が表示されます。

- 毎週日曜 4:30 に `restic check` でバックアップの一部を読み出して確かめます
- 残す世代は日次 7・週次 4・月次 6（`/etc/docseed/backup.env` で変更）
- バックアップが失敗したり 36 時間以上成功していなかったりすると、管理画面に警告が出ます

### Pi を作り直して復旧する

1. 新しい SSD に OS・Docker・restic を入れ、このリポジトリを clone する（最初の手順と同じ場所に）
2. 保存先をつなぎ、控えておいたパスワードを `/etc/docseed/restic-password` に置いて復元する
   ```bash
   export RESTIC_REPOSITORY=/mnt/backup/docseed-restic RESTIC_PASSWORD_FILE=/etc/docseed/restic-password
   sudo -E restic restore latest --tag docseed --target /   # /srv/docseed と .env が元の場所に戻る
   ```
3. DB を戻し、モデルを取得して起動する
   ```bash
   docker compose run --rm --no-deps api docseed restore-db
   docker compose --profile tools run --rm --build tools
   docker compose up -d --build
   docker compose exec api docseed verify-files               # 「問題はありません」なら完了
   sudo scripts/ops/install-backup.sh                         # バックアップのタイマーを入れ直す
   ```

最後のバックアップより後にアップロードしたファイルは戻りません。詳しくは [配置・運用](docs/design/deployment.md) の 5 章。

## 状態の確認

管理画面の「確認が必要な項目」に、worker の停止・失敗したジョブ・SSD の空き不足・バックアップの遅れや失敗・CPU の高温をまとめて表示します。項目があると、左メニューの「管理」に赤いバッジが付きます。同じ内容を `docker compose exec api docseed status` や `GET /api/v1/admin/status`（管理者の API トークン）でも確認できます。

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
| `docseed reset-auth --username NAME` | ログイン方法（パスキー・確認コード・外部アカウントの連携）を消し、仮のパスワードを発行。すべての端末をログアウトし、API トークンを無効にする |
| `docseed purge-trash` | 保持期間を過ぎたゴミ箱の項目を完全に削除 |
| `docseed backup-db` | DB のスナップショットを `backup/` に作成（7 世代。整合性を確認してから置き換える） |
| `docseed restore-db [パス]` | DB をスナップショット（省略時は `backup/` の最新）から戻す。api と worker を止めて実行 |
| `docseed verify-files [--quick]` | DB と原本ファイルの対応（存在・SHA-256）を確認する。問題があれば終了コード 1 |
| `docseed status [--json]` | 状態と警告を表示する（管理画面と同じ内容） |
| `docseed worker` | 文字の読み取り（OCR）と埋め込みの処理を行う |
| `docseed fetch-models` | OCR と埋め込みのモデルを取得する（`.[tools]` が必要） |
| `docseed reprocess failed\|outdated\|unsupported\|all` | 文書を再処理する（`--force-ocr` で PDF も画像として読み取る） |

## 文書

- [設計書](docs/design.md)（詳細設計は `docs/design/`。今後の拡張は [拡張機能](docs/design/extensions.md)）
- [ローカル OCR / Embedding モデル調査](docs/research/ocr-embedding-models.md)
- [Phase 0 計測結果](docs/research/phase0-results.md)
- [新機能の実現手段の調査](docs/research/new-features.md)（URL 取り込み・小型 LLM・画像形式・アイコン・HTTPS・ログイン）
