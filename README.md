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
| Phase 6 | HTTPS 証明書の方式の切り替え（internal / file / tailscale / acme_dns）と監視、アイコン、HEIC / AVIF、サムネイル・プレビュー | 実装済み |
| Phase 7 | ログイン（パスキー・確認コード・復旧コード・Google などの OIDC・再認証） | 実装済み |
| Phase 8 | URL の取り込み（Web クリップ。SSRF 対策つきの取得、OGP・本文の取り出しと検索、ブックマークレット） | 実装済み |
| Phase 9 | 整理の補助（フォルダ・タグの候補、小型 LLM による要約） | 実装済み（LLM のモデルは Pi 5 での計測で決める） |
| Phase 10 | 音声・動画の文字起こし（任意。バッチ処理・既定はオフ） | 実装済み |

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

既にアップロードしたファイルは「処理待ち」のままになっているので、worker が起動すると順に処理されます。進み具合は管理画面の「文字の読み取りと検索の準備」で確認できます。処理中のファイル（ページの進み・開始時刻・試行回数）と、待っているファイルの順番も同じ画面に出て、5 秒ごとに更新されます。API では `GET /api/v1/admin/jobs` の `running` と `waiting` で取れます。

- `tools` イメージは、モデルを ONNX 形式に変換するための torch を含むので大きめです（変換が終われば削除して構いません: `docker image rm docseed-tools`）
- worker は外部と通信しない設定です。モデルが無いと起動直後のジョブが失敗するので、先に `tools` を実行してください
- テキスト層のある PDF でも、ページに貼られた図・写真・スクリーンショットの中の文字を読み取ります（ページ全体を覆うスキャン画像は除きます。止めるには `DOCSEED_OCR_PDF_IMAGES=false`）。更新前に取り込んだ PDF は、管理画面の「古い版で処理したファイルを再処理」で読み直せます（PDF だけが対象で、画像などはやり直しません）

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

### Tailscale の証明書を使う（おすすめ）

Pi と使う端末に [Tailscale](https://tailscale.com/) を入れていれば、Tailscale が Let's Encrypt の証明書を取ってくれます。ブラウザの警告が出ず、パスキーも使え、インターネットに Pi を公開する必要もありません。

1. Tailscale の管理画面（DNS）で **MagicDNS** と **HTTPS Certificates** を有効にします
2. Pi の名前を確かめます: `tailscale status --json | jq -r .Self.DNSName`（末尾の `.` を除いた `<機械名>.<tailnet>.ts.net`）
3. `.env` を変えて Caddy を作り直します

```bash
# .env
DOCSEED_TLS_MODE=tailscale
DOCSEED_HOST=docseed.tailxxxx.ts.net
```

```bash
docker compose up -d --force-recreate caddy
docker compose logs caddy | tail -n 20
```

Caddy はホストの `/var/run/tailscale/tailscaled.sock` を通して証明書をもらいます。Tailscale を入れていない端末からは、今まで通り `DOCSEED_LAN_IP` の IP アドレスで開けます（内部 CA の証明書。パスキーは使えません）。

### 自分のドメインの証明書を DNS-01 で取る

自分のドメイン（例 `docseed.example.com`）があり、DNS を Cloudflare で管理していれば、Pi をインターネットに公開せずに Let's Encrypt の証明書を取れます（DNS の TXT レコードで確認するため）。

1. Cloudflare で **Zone:DNS:Edit の権限だけ**を付けた API トークンを作ります
2. DNS に `docseed.example.com` の A レコードを Pi の LAN の IP（または Tailscale の IP）で登録します。ルーターに「DNS リバインディングの保護」がある場合は、そのドメインを除外します（または Pi-hole などの LAN の DNS で引きます）
3. DNS のプラグイン入りの Caddy をビルドし（数分かかります）、`.env` を変えます

```bash
docker compose --profile acme build caddy-acme
```

```bash
# .env
DOCSEED_TLS_MODE=acme_dns
DOCSEED_HOST=docseed.example.com
DOCSEED_CADDY_IMAGE=docseed-caddy:latest
DOCSEED_ACME_DNS_TOKEN=xxxxxxxx
DOCSEED_ACME_EMAIL=you@example.com    # 空でもよい
DOCSEED_ACME_STAGING=true             # 最初は試験用の環境で確かめる（回数の制限に当たらないように）
```

```bash
docker compose up -d --force-recreate caddy
docker compose logs caddy | grep -i -E "certificate|error"   # 「certificate obtained successfully」が出れば成功
```

試験用の環境で取れたら `DOCSEED_ACME_STAGING=false` にして、もう一度 `docker compose up -d --force-recreate caddy` します。Cloudflare 以外の DNS 事業者は、`docker/caddy/Dockerfile` の `DNS_PLUGIN` と `docker/Caddyfile` の `(tls_acme_dns)` をその事業者のものに変えてください（[Caddy の DNS プラグインの一覧](https://caddyserver.com/docs/modules/)）。

### 証明書の監視

api が 6 時間ごとに Caddy の証明書を確かめ、検証できない（名前が合わない・期限切れ）ときと、期限まで 7 日を切ったときに、管理画面の「確認が必要な項目」に出します。証明書の状態は管理画面の「HTTPS の証明書」で見られます。

### インターネットに公開する

Tailscale や LAN の中だけで使うのが基本ですが、ルーターでポートを開けて公開する場合に備えて、Caddy が次の制限をかけています（設定なしで有効。[設計](docs/design/security.md) 8 章）。

- LAN・Tailscale の外からは、管理画面（`/admin`）・管理用 API（`/api/v1/admin`）・API の説明ページ（`/api/docs`）・`/healthz` が 404 になる。管理の操作は Tailscale 経由で行う
- 外から IP アドレス（`DOCSEED_LAN_IP`）で開こうとすると、何も返さずに接続を切る
- `Permissions-Policy`・`Cross-Origin-Opener-Policy` を付け、`Server` などのヘッダを外す

「内側」の範囲は `.env` の `DOCSEED_TRUSTED_NETS` で変えられます（既定は `private_ranges 100.64.0.0/10`）。例えば LAN からも管理させず Tailscale だけにするなら `DOCSEED_TRUSTED_NETS=100.64.0.0/10 fd7a:115c:a1e0::/48` にして `docker compose up -d --force-recreate caddy`。

公開するときは、あわせて次も行ってください。

- 管理画面の「ログインの設定」で 2 段階の確認を必須にする
- ルーターで開けるのは HTTPS のポートだけにする（80 は証明書の取得に使う場合だけ）。証明書は `acme_dns` などの公的なものにする
- 受け取り用の共有リンク（ログインなしでファイルを送ってもらう）は、期限が 30 日まで、1 リンク 10GB・全リンクで 24 時間 20GB まで、SSD の空きが 10% を下回ったら止まります（`.env` の `DOCSEED_SHARE_UPLOAD_*` で変更）
- ログインの失敗が続いてユーザー名がロックされても、以前ログインした端末とパスキーからはログインできます（攻撃者に締め出されないように）
- コンテナは読み取り専用・特権なしで動きます（`compose.yaml` の `x-hardening`。[設計](docs/design/deployment.md) 2.1.1）
- **IPv6 で公開する場合の注意**: Docker のポート公開では、IPv6 の接続は docker-proxy を通るため、接続元が Docker のゲートウェイ（`172.x.x.x`）に見え、上の制限をすり抜けます。IPv6 でも公開するなら `DOCSEED_TRUSTED_NETS` を `10.0.0.0/8 192.168.0.0/16 100.64.0.0/10 fd7a:115c:a1e0::/48` のように `172.16.0.0/12` を含まない値にしてください（そのぶん IPv6 で開いた LAN の端末も外側扱いになります）
- 外の回線（スマホのテザリングなど）から `https://<名前>/admin` と `https://<名前>/healthz` が 404 になることを確かめる
- ログイン以外にも、接続元の IP ごとに回数の上限があります（共有リンクの閲覧 1 分 600 回・受け取り 1 時間 30 回・無効なリンク 15 分 20 回、検索 1 分 60 回、ログインの開始 1 分 30 回）。超えると 429 を返します（`.env` の `DOCSEED_RATE_LIMIT=false` で止められます）

#### 失敗が続く接続元をファイアウォールで遮断する（fail2ban）

api は、ログインの失敗・無効な共有リンク・回数の上限超えなどを `<DOCSEED_DATA_DIR>/logs/security.log` に 1 行ずつ書きます。fail2ban にこれを読ませ、10 分に 10 回を超えた接続元を 1 時間（繰り返すたびに長く、最長 1 週間）遮断します。LAN と Tailscale のアドレスは遮断しません。

```bash
sudo scripts/ops/install-fail2ban.sh          # fail2ban を入れて設定を置く（初回だけ）
sudo fail2ban-client status docseed           # 遮断中の IP
sudo fail2ban-client set docseed unbanip <IP> # 誤って遮断したときの解除
```

- Docker が公開したポートへの通信はホストの通常の規則（`INPUT`）を通らないため、Docker の `DOCKER-USER` チェーンで止めます（設定済み）
- 上の IPv6 の注意と同じ理由で、IPv6 で届いた接続は遮断できません（`172.x` に見えるため。遮断すると全員が止まる）
- Cloudflare Tunnel（次の節）を使う場合、通信は cloudflared から届くので fail2ban では止められません。Cloudflare の側（WAF・Access）で止めてください。アプリの中の回数の制限は、本当の接続元で数えるのでそのまま効きます

### ポートを開けずに公開する（Cloudflare Tunnel）

ルーターのポートを開けず、自宅の IP アドレスも出さずに公開できます。Pi から Cloudflare へ外向きにつなぎ、Cloudflare が受けた通信をそこから流します（[設計](docs/design/security.md) 10 章）。Cloudflare のアカウントと、Cloudflare で管理しているドメインが要ります。

注意: Cloudflare が HTTPS を終端するため、通信の中身（アップロードするファイルを含む）を Cloudflare が見られる位置にいます。気になる場合は、前の節の直接公開か、Tailscale だけで使ってください。

1. Cloudflare の管理画面（Zero Trust → Networks → Tunnels）でトンネルを作り、表示されたトークンを `.env` の `DOCSEED_TUNNEL_TOKEN` に入れる
2. 同じ画面の「Public hostname」を追加する
   - Hostname: 公開する名前（例 `docs.example.com`）。`.env` の `DOCSEED_HOST` の先頭にもこの名前を書く（例 `DOCSEED_HOST=docs.example.com,pi.tailxxxx.ts.net`。パスキーは先頭の名前で登録される）
   - Service: `HTTPS` / `caddy:443`（`DOCSEED_HTTPS_PORT` を変えている場合はその番号）
   - Additional application settings → TLS → Origin Server Name に公開する名前を入れる。証明書が `internal` の場合は「No TLS Verify」をオンにする（Pi と Cloudflare の間はトンネルの中なので、`acme_dns` で公的な証明書にするとより確実）
3. `.env` の `COMPOSE_PROFILES` に `tunnel` を足して起動する

```bash
docker compose up -d                          # cloudflared が起動する
docker compose logs cloudflared | tail        # 「Registered tunnel connection」が出れば接続できている
```

おすすめ: Cloudflare Access（Zero Trust → Access → Applications）で公開する名前を保護し、メールや Google での確認を通った人だけが届くようにします。共有リンク（`/s/*`）だけは外の人が開くので、その経路を「Bypass」にしてください。

- トンネル経由の通信は、常に「外側」として扱います（管理画面などは 404）。管理は今まで通り Tailscale・LAN から行います
- Caddy は cloudflared のネットワーク（`DOCSEED_TUNNEL_SUBNET`、既定 `10.213.47.0/28`）から来たときだけ `CF-Connecting-IP` を本当の接続元として使います。ほかから送られたこのヘッダは無視します
- トンネルを使う場合は、ルーターのポートを閉じたままにできます（Tailscale と LAN からは今まで通り直接開けます）

### パスの下で動かす（`https://example.com/docseed/`）

ドメイン（サブドメイン）を他のサービスと分け合っていて直下に置けない場合は、パスの下で動かせます。

```bash
# .env
DOCSEED_BASE_PATH=/docseed
```

```bash
docker compose up -d --force-recreate   # ポートも変えたときは Caddy も作り直す必要があるので、全部を作り直す
```

`https://<名前>/docseed/` で開きます（`/docseed` だけで開いても `/docseed/` に移ります）。画面のリンク・リダイレクト・Cookie・共有リンク・ブックマークレット・通知のリンク・Google のリダイレクト URI に接頭辞が付きます。

置き方は次のどれかです。

- **docseed の Caddy がそのまま受ける**（`DOCSEED_HOST` がその名前）: ほかの設定は要りません。この場合は `/docseed/` の外（`/` など）で開いても docseed が出ます
- **外側の別のリバースプロキシ（nginx・Apache・別の Caddy）が `/docseed/` だけを docseed に転送する**: 手順・設定例・うまく行かないときの対処は [別の Web サーバーの下に置く](docs/guides/reverse-proxy.md) にまとめています。`Host` ヘッダは元のまま渡してください。接頭辞は付けたまま転送しても（`proxy_pass https://pi:8443;`）、外して転送しても（`proxy_pass https://pi:8443/;`）動きます。ブラウザから見たオリジンが `https://<DOCSEED_HOST>:<DOCSEED_HTTPS_PORT>` と違う（ポートを付けずに開くなど）ときは、`DOCSEED_PUBLIC_URL=https://example.com` も書きます（パスキーと Google のログインがこれを使います）
- **Cloudflare Tunnel**: 「Public hostname」の Path に `docseed` を入れ、ほかは前の節の通りに設定します

注意:

- **同じオリジンの別のアプリから守られません。** ブラウザはパスで区別しないため、同じ名前の下の別のアプリに XSS などの穴があると、docseed の画面も操作されえます。Cookie の `Path` は `/docseed` に絞りますが、これでは防げません。同じ名前の下には、信頼できるものだけを置いてください
- **外側のプロキシが LAN にあると、全員が「内側」に見えます。** Caddy には外側のプロキシのアドレスが接続元として届くため、管理画面などの 404 の制限（前の節）が効きません。外側のプロキシで `/docseed/admin`・`/docseed/api/v1/admin`・`/docseed/api/docs`・`/docseed/api/v1/openapi.json`・`/docseed/healthz` を外から開けないようにしてください。nginx の例:

  ```nginx
  location ~* ^/docseed/(admin|api/v1/admin|api/docs|api/v1/openapi\.json|healthz)(/|$) { return 404; }
  location /docseed/ {
      proxy_pass https://192.168.1.20:8443;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_ssl_server_name on;
      proxy_ssl_name docseed.example.com;      # DOCSEED_HOST（Caddy の証明書の名前）
      client_max_body_size 1g;                 # アップロードの上限（DOCSEED_MAX_UPLOAD_MB）
      proxy_request_buffering off;
      proxy_buffering off;
  }
  ```

- 名前を変えて移すと、パスキーの RP ID が変わり、登録済みのパスキーは使えなくなります（管理画面に警告が出ます）。確認コードかパスワードでログインして登録し直してください
- 接頭辞を後から付けると、それまでに配った共有リンク（`/s/…`）とブックマークレットの URL が変わります。共有リンクは配り直すか、外側のプロキシで `/s/` を `/docseed/s/` に転送してください。ブックマークレットは設定画面から登録し直します。Google のログインを使っているなら、Google Cloud のリダイレクト URI も `https://<名前>/docseed/auth/oidc/google/callback` に変えます

### Phase 7 までから更新する場合（写真・サムネイル）

```bash
git pull
docker compose up -d --build
docker compose exec api docseed reprocess unsupported   # 以前に保存した HEIC / AVIF の文字を読み取る
docker compose exec api docseed derive missing          # 保存済みの画像・PDF のサムネイルとプレビューを作る（文字の読み取りはやり直さない）
```

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
| `caddy` のログに `get_certificate tailscale` / `tailscaled.sock` のエラー | Pi で `tailscale status` が動いているか、管理画面で HTTPS Certificates が有効か、`DOCSEED_HOST` が `tailscale status --json` の `DNSName` と同じかを確認 |
| `caddy` のログに `module not registered: dns.providers.cloudflare` | `DOCSEED_TLS_MODE=acme_dns` には DNS のプラグイン入りの Caddy が要ります。`docker compose --profile acme build caddy-acme` と `.env` の `DOCSEED_CADDY_IMAGE=docseed-caddy:latest` |
| 証明書の警告で先に進めない（「この接続ではプライバシーが保護されません」で例外を追加するボタンが出ない） | 開いた名前と証明書が合っていません。そのドメインに HSTS（以前に Let's Encrypt の証明書で開いたなど）が付いていると、ブラウザは例外を許しません。上の「手元の証明書を使う」でその名前の証明書を Caddy に使わせてください |
| `caddy` が起動しない・`loading certificates` や `open /certs/...: no such file` のエラー | `.env` の `DOCSEED_TLS_CERT_DIR` と、その中の相対パス `DOCSEED_TLS_CERT` / `DOCSEED_TLS_KEY` を確認（`sudo ls -l /etc/letsencrypt/live/<名前>/`）。`DOCSEED_TLS_MODE` は `internal` / `file` / `tailscale` / `acme_dns` のどれか |
| 管理画面（`/admin`）や `/api/docs` が 404 になる | LAN・Tailscale の外からの接続とみなされています（「インターネットに公開する」）。Tailscale 経由で開くか、`.env` の `DOCSEED_TRUSTED_NETS` に接続元の範囲を足して `docker compose up -d --force-recreate caddy` |
| 共有リンクや検索で「アクセスが多すぎます」（429）になる | 接続元の IP ごとの回数の上限（「インターネットに公開する」）。しばらく待つと戻ります。fail2ban を入れている場合は `sudo fail2ban-client status docseed` で遮断されていないかも確認 |
| `cloudflared` のログに `Unauthorized` / `Invalid tunnel secret` | `.env` の `DOCSEED_TUNNEL_TOKEN` を Cloudflare の管理画面のトークンに合わせて `docker compose up -d` |
| トンネル経由で開くと 502 になる | Public hostname の Service が `https://caddy:<DOCSEED_HTTPS_PORT>` か、`internal` の証明書なら No TLS Verify がオンかを確認。開いた名前が `DOCSEED_HOST` に入っているかも確認 |
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
| `taken:2026-05`、`taken:2026-05-01..2026-05-10` | 写真の撮影日で絞り込む（検索画面の「撮影日（から・まで）」と同じ） |

検索語が無くても、種類・タグ・日付・撮影日で絞り込めば当てはまるファイルを一覧にします（文字の無い写真も撮影日で探せます）。

文書の画面には、内容が似ている **関連する文書** を 5 件まで出します（文字の読み取りが終わった文書だけ。見られるものだけを出します）。

### 検索の品質を確かめる・調整する

自分の文書に対する「質問」と「正解のファイル名」を 20〜30 件ほど JSON に書き、`scripts/eval_search.py` で Recall@5 / MRR を測れます（使い方はスクリプト冒頭のコメント）。
意味検索で無関係なファイルが多く出る・逆に見つからない場合は、`--sem-min 0.75,0.80,0.85` で類似度の下限を比べ、良かった値を `.env` の `DOCSEED_SEARCH_SEM_MIN` に設定します（既定 0.80）。

## URL の取り込み（Web クリップ）

Web ページの URL を渡すと、ページを取得して保存し、タイトル・説明・本文で検索できるようにします。PDF や画像の URL は、そのファイルを普通のファイルとして保存します。既定では無効です（Pi からインターネットへ通信するため）。

```bash
# .env
DOCSEED_WEBCLIP_ENABLED=true
COMPOSE_PROFILES=webclip
```

```bash
docker compose up -d     # fetcher（取得の担当）が起動する
```

- フォルダの画面の「URL を追加」で追加します。取得中は一覧に「取得中」と出て、終わると普通の文書と同じ行になります（種類は Web クリップ、サムネイルはページの OGP 画像）
- 設定画面の**ブックマークレット**をブックマークバーに登録すると、見ているページをそのまま保存できます（保存先を選ぶ画面が開きます）
- 文書の画面の「再取得」で、元のページの最新の内容に保存し直せます
- 保存したページの HTML は、安全のため画面には表示しません（ダウンロードはできます）。取り出した本文は「取り出したテキスト」で読めます
- JavaScript で本文を描くページ（SNS など）は、通常の取得では本文が空に近いことがあります。その場合は、任意の**描画サービス**（ヘッドレス Chromium）で JavaScript を動かしてから保存します（下の「JavaScript で描くページ」を参照）。描画もできなかったときは、タイトルと説明で検索でき、サイトが oEmbed に対応していれば、その文章も補います

**安全のための制限**: LAN の機器（ルーター・NAS など）や Pi 自身へのリクエストを作らせないよう、取得の前に名前を引いて、プライベートな IP（192.168.x.x・10.x.x.x・Tailscale の 100.64.0.0/10 など）を指す URL は取得しません。リダイレクトのたびに確かめ直し、確かめた IP にだけ接続します。ポートは 80・443・8080・8443 だけ、HTML は 5MB（`DOCSEED_WEBCLIP_MAX_MB`）・30 秒までです。取得するのは fetcher だけで、文字の読み取りを行う worker は今まで通り外部と通信しません。

`DOCSEED_WEBCLIP_PROXY` でプロキシを使うこともできます。そのときは名前の解決をプロキシに任せることになるので、**プロキシの側で LAN への通信を止めてください**。

### JavaScript で描くページ（任意）

本文が短いページ（JavaScript で組み立てるページなど）は、描画サービス（`webclip-browser`。Chromium を含む別のコンテナ）で JavaScript を動かしてから保存します。メモリを 1GB ほど使うので、既定では起動しません。

```bash
# .env
DOCSEED_WEBCLIP_ENABLED=true
DOCSEED_WEBCLIP_RENDER_URL=http://webclip-browser:8080
COMPOSE_PROFILES=webclip,webclip-render
```

```bash
docker compose build webclip-browser
docker compose up -d
```

- 描画は本文が 200 文字未満のページだけ行います。描画できなかったときは、取得した HTML のまま保存します（取り込みは失敗しません）
- 描画のときも、ページが出す通信（スクリプト・画像・リダイレクトの先を含む）は取得と同じ確認を通します。LAN やプライベートな IP には出ません
- 描画サービスは Cookie や保存領域を持ち越さず、docseed のデータも見られません。1 件ずつ処理します
- 描画できたかは、Web クリップの API の `rendered`（`true` / `false`）で分かります
- Chromium のサンドボックスが動かない機械では `.env` に `DOCSEED_RENDER_NO_SANDBOX=1` を設定します（コンテナの中の制限を緩めるので、必要なときだけ）
- **注意**: 描画サービスの通信は Docker のネットワーク越しに外へ出ます。WebSocket の通信は確認の対象にならないため、**LAN への出口は Pi のルーターや Docker 側でも止めてください**

## 音声・動画の文字起こし（任意）

音声（mp3・m4a・wav など）と動画（mp4・mov など）を、10 分ごとに区切って文字起こしし、検索できるようにします。既定はオフで、オフのときは従来どおり「保存のみ」です。**Pi 5 の CPU で動かすので遅く**（1 時間の動画に数時間かかることがあります）、夜間などにまとめて処理される前提のオプション機能です。外部とは通信しません。

1. イメージに ffmpeg と whisper-cli（whisper.cpp）を入れて作り直します。`.env` で `DOCSEED_IMAGE_TARGET=asr` にし、`docker compose build` してから `up -d` します（作るのに Pi 5 で数十分かかります）。
2. モデルを取得します（`small` は約 0.5GB。RAM に余裕がなければ `base`）。

   ```bash
   docker compose exec api docseed fetch-models --asr small
   ```

3. `.env` で `DOCSEED_ASR_ENABLED=true` にし、`DOCSEED_ASR_MODEL=ggml-small.bin`（取得時に表示された名前）を確かめて、worker を起動し直します（`docker compose up -d`）。
4. すでに保存してある音声・動画は、管理画面の「形式が未対応だったファイルを再処理」で読み直せます。

- 文字起こしの行の先頭には動画の中の時刻（`[00:12:34]`）を付けるので、検索結果で場所が分かります。
- worker の上限（`mem_limit: 3g`）は、文字起こしの間も共有されます。メモリが足りないときは `DOCSEED_ASR_MODEL=ggml-base.bin` にするか、compose の `mem_limit` を上げてください。
- 無音や音楽だけの音声は、警告「音声から文字を取り出せませんでした」が出ます。
- 文字の精度は音声の質に大きく左右されます。大事な記録は、必ず元の音声で確かめてください。
- 設定: `DOCSEED_ASR_LANGUAGE`（既定 `ja`）・`DOCSEED_ASR_THREADS`（既定 3）・`DOCSEED_ASR_TIMEOUT_S`（10 分の区切り 1 つの上限、既定 1800）。

## 整理の補助（フォルダ・タグの候補と要約）

似ている文書から、置くフォルダと付けるタグの候補を出します。候補は提案だけで、移動やタグ付けはボタンを押したときに行います（設定の「整理の補助」で、確かさの高いものを自動で適用することもできます）。

- **受け取り箱（Inbox）**・共有リンクで受け取った文書・マイファイルの直下に置いた文書には、処理が終わると候補を作ります。Inbox の一覧には各文書のおすすめのフォルダが出て、「移動」で 1 回で動かせます。チェックした文書をまとめて移動することもできます
- それ以外の文書は、文書の画面を開いたときに候補を出します（「整理の候補」の欄）
- 候補は、あなた自身のフォルダの中で、似ている文書が多いフォルダです。他人のフォルダは候補にしません。まだ文書の少ないフォルダは、フォルダの名前と内容の近さで候補にします
- 却下したタグの候補は、同じ文書には二度と出しません

候補のしきい値は仮の値です。分類済みの文書がある程度たまったら、次で正解率を確かめられます（1 位の正解率・上位 3 の正解率・「おすすめ」の正しさ）。

```bash
docker compose exec api docseed eval-suggest --username <名前>
```

### 質問に答える（小型の LLM、任意）

下の「要約」と同じ LLM を有効にすると、検索画面に **「この質問に、文書をもとに答える」** が出ます。押すと、検索で見つかった文書（見られるものだけ）を資料にして答え、根拠の資料に番号を付けて並べます（例「振込先は三井銀行 本店です [1]」）。資料が見つからないときは答えません。Pi では答えが出きるまで数十秒かかりますが、出来た分から表示します。

- 有効にすると、api のコンテナも LLM を呼ぶために `internal` ネットワークに入ります（外部へは出られないネットワーク）
- 要約を作っている途中でも答えられるよう、LLM の同時の枠を 2 にしました（`DOCSEED_LLM_PARALLEL=2`、`DOCSEED_LLM_CTX=8192`）。メモリが足りなければ `.env` で 1 と 4096 に戻してください
- 小型のモデルなので誤りがありえます。番号の資料で確かめてください。API は `POST /api/v1/ask`

### 要約（小型の LLM、任意）

Pi の中で小型の LLM（llama.cpp）を動かし、文書ごとに 2〜3 文の要約と、新しいタグの候補を作ります。要約は文書の画面と検索結果に出ます（検索の対象にはしません）。外部とは通信しません。**RAM 16GB を推奨**します（8GB なら小さいモデル `qwen3-0.6b` を使ってください）。

```bash
docker compose exec api docseed fetch-models --llm lfm2.5-1.2b-jp    # models/llm/ に GGUF を置く（約 0.7GB）
```

```bash
# .env
DOCSEED_LLM_ENABLED=true
DOCSEED_LLM_MODEL=LFM2.5-1.2B-JP-Q4_K_M.gguf   # fetch-models の最後に表示された名前
COMPOSE_PROFILES=llm            # Web クリップも使うなら webclip,llm
```

```bash
docker compose up -d
```

| 名前（`--llm`） | ライセンス | 備考 |
|---|---|---|
| `lfm2.5-1.2b-jp`（既定） | LFM Open License v1.0 | CPU 向けの設計。Pi 5 で最も軽かった（中央値 約 20 秒、メモリ 1.4GB）。年間売上 1,000 万ドル未満なら商用も無料（ライセンスの条件を確認してください） |
| `sarashina2.2-1b` | MIT | 日本語の学習が中心。速さは lfm とほぼ同じで、メモリは 1.9GB。ライセンスを MIT にしたいとき |
| `sarashina2.2-1b-q8` | MIT | Q8_0（少し遅く、少し正確） |
| `qwen3-1.7b` | Apache-2.0 | Pi 5 では 1 件 90 秒を超えることがあった |
| `qwen3-0.6b` | Apache-2.0 | RAM 8GB 向け |

モデルには、文書の**種類**（模試・プリント・請求書など）・**分野**（地理・数学など。自由に書かせる）・**主題**（国・地域・ものなど）と、2 文の要約を書かせます。種類・分野・主題はタグの候補になり、すでに使っているタグに合うものがあれば、その表記にそろえます。

要約は 1 件ずつ、ほかの処理（アップロード・再処理）が無いときだけ行います。1 件 180 秒（`DOCSEED_LLM_TIMEOUT_S`）を超えたら、その文書は要約なしにします。

要約やタグの候補が合わないときは、文書の画面の「整理の候補」にある **「要約とタグをやり直す」** で作り直せます（今の要約は上書きされます）。**「要約を直す・消す」** で、要約を手で書き直したり、その文書では出さないようにしたりもできます（直した要約は検索にも使います。同じ内容のほかのファイルの要約は変わりません。「自動の要約に戻す」で元に戻せます）。文書の画面は、文字の読み取りや要約の作り直しが終わると自動で新しくなります。`DOCSEED_LLM_MODEL` を別のモデルに変えたときは、管理画面の「要約（LLM）」に旧モデルで作った要約の件数が出るので、**「旧モデルの要約を今のモデルで上書き」** でまとめて作り直せます。一覧で複数のファイルを選ぶと、選択バーから **タグを付ける・外す**、**再処理**、**要約とタグをやり直す** をまとめて行えます。

画像やスキャンした PDF の文書の画面では、「取り出したテキスト」の **「読み取った位置を画像に重ねて表示」** で、どこをどう読み取ったかを画像の上で確かめられます。検索結果から開いたときは、検索語を含む行が色付きの枠で示されます。ページごとの **「このページを読み直す」** で、そのページだけを読み直せます（PDF は埋め込みテキストを使わず画像として読むこともできます）。

サイドの **「タグ」** で、自分のファイルのタグの一覧と件数を見て、名前を変えたり（既にある名前にすると 1 つにまとまります）、外したりできます。却下したフォルダ・タグの候補は、設定の「整理の補助」の **「却下した候補の一覧」** から取り消せます。

#### モデルを比べる

自分の文書で、モデルごとの速さ・メモリ・要約の出来を 1 回のコマンドで比べられます（Pi の上で、リポジトリの中から実行）。

```bash
scripts/bench_llm.sh sarashina2.2-1b qwen3-1.7b lfm2.5-1.2b-jp   # 名前で選ぶ（未取得なら取得する）
scripts/bench_llm.sh                                            # models/llm/ にある GGUF を全部比べる
```

これだけで次を行います。

1. 比べる文書を書き出す（初回だけ。新しい文書から 30 件を `/srv/docseed/bench/docs` に。本文が 100 字未満の文書と、同じ内容のファイルは除く）
2. 計測の間だけ worker と llm を止める（CPU とメモリを取り合わないように。終わると、Ctrl+C で止めても、起動し直す）
3. モデルごとに llama-server を本番と同じ設定（`.env` のスレッド数・コンテキスト 4096・メモリ 2GB）で起動し、全文書を要約して時間とメモリを測る
4. 比較表 `/srv/docseed/bench/llm-<日時>/report.md` を作る

比較表の先頭には、モデルごとの平均・中央値・最大の秒数、失敗と頼み直しの件数、最大のメモリと、条件（1 件 90 秒以内・メモリ 2GB 以内）を満たすかが並びます。その下に文書ごとに各モデルの要約とタグが並ぶので、本文に無いことを書いていないかを見比べて「誤り」の欄に印を付けてください（20 件中 2 件以下が目安）。

| オプション | 内容 |
|---|---|
| `--limit N` / `--username NAME` | 書き出す文書の数（既定 30）／そのユーザーの文書だけにする |
| `--refresh-docs` | 文書を書き出し直す（既定では前回の文書を使い回すので、モデルを後から足しても同じ文書で比べられる） |
| `--docs DIR` | 手で用意したテキスト（`.txt`）で比べる |
| `--out DIR` | 前回の結果のディレクトリを渡すと、そこにモデルを足して比較表を作り直す |
| `--threads N` / `--mem 3g` / `--timeout S` | llama-server のスレッド数・メモリの上限・1 件の上限を変えて試す |

要約には文書の内容が入るので、結果はリポジトリではなくデータディレクトリに置きます。共有するときは先頭の表だけにしてください。

## 写真（HEIC など）とサムネイル

iPhone の写真（HEIC / HEIF）と AVIF も、ほかの画像と同じように文字を読み取って検索できます。

- 一覧には画像と PDF のサムネイルを、文書の画面には写真の大きさ・撮影日時・カメラを表示します
- HEIC など、Safari 以外のブラウザが表示できない形式や、とても大きな画像は、表示用のプレビュー（WebP）を作って表示します。ダウンロードすると原本（そのままのファイル）が手に入ります
- 音声（mp3・m4a・wav・ogg・flac など）と動画（mp4・webm・mov）は、文書の画面と共有ページで再生できます。途中からの再生（シーク）にも対応します。AVI・WMV・MKV などは、ブラウザで安定して再生できないため、保存とダウンロードだけです
- 映像は保存時に、通信量を減らすための軽いプレビュー用の動画（H.264・MP4、長辺 854px 程度）を作って再生します。原本は変えません。`DOCSEED_VIDEO_PROXY=false` にすると作らず、原本をそのまま再生します
- サムネイルとプレビューは、向きを直して位置情報などの EXIF を消したものです。**原本には撮影場所が残る**ので、位置情報を含む写真を共有リンクで渡そうとすると、共有ダイアログに注意が出ます

## Office 文書

Word（docx）・Excel（xlsx）・PowerPoint（pptx）の文字も検索できます（マクロ付きの docm / xlsm / pptm も同じ）。

| 形式 | 取り出し方 |
|---|---|
| Word | 本文と表。見出し 1・2 ごとに区切って表示します |
| Excel | シートごと。セルの値（数式は計算結果）。1 シート 100 万セルまで |
| PowerPoint | スライドごと。図形・表・ノート |

- 古い形式（doc / xls / ppt）とパスワード付きのファイルは、保存・共有はできますが文字は取り出しません
- 画像の中の文字（貼り付けたスクリーンショットなど）は読み取りません。必要なら PDF に変換してアップロードしてください

## 画面の色とキーボード操作

- 画面は端末の設定に合わせて **ダークモード** になります。設定の「画面の色」で、ライト・ダークに固定することもできます
- **`?`** でキーボード操作の一覧を出せます。主なもの: `/` 検索、`g` → `m` マイファイル / `f` お気に入り / `t` タグ / `d` ゴミ箱、一覧で `j` `k` と `Enter`、`s` お気に入り、`u` アップロード

## お気に入り・最近開いたファイル・検索の履歴

- ファイルやフォルダの画面の **「☆お気に入り」** で、左のメニューの **「お気に入り」** に並べられます。同じ画面に **最近開いたファイル**（新しい 20 件）も出ます
- 検索画面は、検索語を入れる前に **保存した検索** と **最近の検索** を出します。結果の上の **「この検索を保存」** で、よく使う条件（例 `tag:請求書` と撮影日など）に名前を付けて残せます
- どれも本人だけが見られます。最近開いたファイルと検索の履歴は、それぞれの画面から消せます

## 過去の版

同じ名前のファイルを「置き換え」でアップロードしたとき（API の `on_conflict=replace`）、文書の画面の **「新しい版をアップロード」** を使ったとき、Web クリップを再取得したときは、それまでの中身を **過去の版** として残します。文書の画面の「過去の版」から、ダウンロード・**この版に戻す**（今の版も過去の版に残るので、戻したことも取り消せます）・削除ができます。

- 1 つのファイルにつき新しいものから `DOCSEED_VERSIONS_KEEP`（既定 10）件まで残し、古いものから消します。`0` にすると版を残しません（減らしたときは、次の毎日の片付けで多い分を消します）
- 過去の版は検索の対象になりません。画面の「使用量」にも含めません（SSD の空きには影響します）
- ファイルをゴミ箱から完全に削除すると、そのファイルの過去の版も消えます

## 容量の上限とゴミ箱の保持日数

- 管理者は、管理画面の「ユーザー」で **容量の上限** を決められます（全体の既定と、ユーザーごと。空なら既定、0 なら上限なし）。上限を超えるアップロード・新しい版・コピーは断ります
- 数えるのは、自分のフォルダにあるファイル・ゴミ箱の中・過去の版です。共有したフォルダや受け取り用のリンクに置かれたファイルは、フォルダの持ち主の分になります
- 左のメニューの下に使用量（上限があればメーター）、設定の「容量とゴミ箱」に内訳が出ます
- **ゴミ箱に残す日数** は、各自が設定で 7〜365 日から選べます（選ばなければ `DOCSEED_TRASH_RETENTION_DAYS`）

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
- 毎月第 1 日曜 5:30 に **復旧の練習** を自動で行い、最新のバックアップから本当に戻せるかを確かめます（一時ディレクトリに復元して、DB と原本の対応を確認し、消す）。復元する大きさ分の空きが `/var/tmp`（`DOCSEED_DRILL_DIR` で変更）に無い月は飛ばして警告を出します。結果は管理画面の「バックアップ」に出ます
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

管理画面の「確認が必要な項目」に、worker の停止・失敗したジョブ・SSD の空き不足・バックアップの遅れや失敗・CPU の高温（`DOCSEED_THERMAL_WARN_C`、既定 75℃）・メモリ不足（`DOCSEED_MEMORY_WARN_PCT`、既定 10%）をまとめて表示します。項目があると、左メニューの「管理」に赤いバッジが付きます。同じ内容を `docker compose exec api docseed status` や `GET /api/v1/admin/status`（管理者の API トークン）でも確認できます。

Prometheus で集めるときは `GET /api/v1/admin/metrics` を使います（管理者の API トークン（read）を bearer で渡す。設定の例は [配置・運用](docs/design/deployment.md) の 6.2）。処理待ちの件数、OCR・埋め込みにかかった時間、検索の速さ、CPU 温度、SSD の空き、ユーザーごとの使用量などを出します。

管理画面の「システム」には、CPU の温度・負荷、メモリ、SSD、データベースの大きさ、ファイル・過去の版・検索の索引の件数、使っているモデル、docseed の版を表示します（`GET /api/v1/admin/system` でも取れます）。

### 通知

`.env` に宛先を書くと、次のことを知らせます。

- **監視の警告**（上の「確認が必要な項目」の注意・異常）: 出たとき（一時的なもので鳴らないよう、5 分おきの確認で 2 回続けて出たとき）、重くなったとき、続いていれば 1 日ごと、解消したとき
- **共有リンクでの受け取り**: 受け取り用のリンクからファイルが届いたとき（続けて届いた分は 1 通にまとめる）
- **受け取り箱の取り込み**: inbox から取り込んだとき・取り込めなかったとき

```bash
# .env（例: ntfy.sh と Discord に送る）
DOCSEED_NOTIFY_URLS=https://ntfy.sh/<推測されにくいトピック名>,https://discord.com/api/webhooks/<ID>/<トークン>
# メールでも送る場合
DOCSEED_NOTIFY_EMAIL=me@example.com
DOCSEED_SMTP_HOST=smtp.example.com
DOCSEED_SMTP_USER=docseed@example.com
DOCSEED_SMTP_PASSWORD=...
```

```bash
docker compose up -d api      # 設定を読み直す
```

管理画面の「通知」にある **「テスト通知を送る」** で届くか確かめられます。最近の通知と、送れなかったときのエラーもそこに出ます。

- 送るのは api だけです（worker は外部と通信しないまま。受け取り箱の通知も api が送ります）。送れないときは 1 分おきに 5 回まで送り直します
- 通知にはファイル名・フォルダ名が入ります。外部のサービス（ntfy.sh・Discord など）に送ってよいかを考えて宛先を選んでください。ntfy.sh のトピックは名前を知っていれば誰でも読めるので、推測されにくい名前にするか、自前の ntfy（`ntfy+https://...`、Basic 認証は `https://ユーザー:パスワード@ホスト/トピック`）を使ってください
- リンク（管理画面・フォルダ）は `DOCSEED_HOST` から作ります

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

API の仕様は起動後に `/api/docs` で確認できます（ログインが必要。LAN・Tailscale の中からだけ開けます）。

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
| `docseed fetch-models --llm NAME` | 要約に使う LLM（GGUF）を `models/llm/` に取得する |
| `docseed export-texts --out DIR` | LLM の比較（`scripts/bench_llm.sh`）に使う文書のテキストを書き出す（`--limit`・`--username`） |
| `docseed eval-suggest --username NAME` | フォルダの候補のしきい値を、分類済みの文書で評価する |
| `docseed fetcher` | URL の取り込み（Web クリップ）の取得を行う（`DOCSEED_WEBCLIP_ENABLED=true` のとき。compose の profile `webclip`） |
| `python -m docseed.webclip.browser` | JavaScript で描くページの描画を行う（描画サービス。compose の `webclip-browser`・profile `webclip-render`。`DOCSEED_WEBCLIP_RENDER_URL` を設定したとき） |
| `docseed derive missing\|all` | 画像・PDF のサムネイルとプレビューを作る（`all` は作り直す。文字の読み取りはやり直さない） |
| `docseed reprocess failed\|outdated\|unsupported\|all` | 文書を再処理する（`--force-ocr` で PDF も画像として読み取る） |

## 文書

- [別の Web サーバーの下に置く（Apache・nginx、パスの下で動かす）](docs/guides/reverse-proxy.md)
- [設計書](docs/design.md)（詳細設計は `docs/design/`。今後の拡張は [拡張機能](docs/design/extensions.md)）
- [ローカル OCR / Embedding モデル調査](docs/research/ocr-embedding-models.md)
- [Phase 0 計測結果](docs/research/phase0-results.md)
- [新機能の実現手段の調査](docs/research/new-features.md)（URL 取り込み・小型 LLM・画像形式・アイコン・HTTPS・ログイン）
