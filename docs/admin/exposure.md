# インターネットに公開する

Tailscale や LAN の中だけで使うのが基本です。外の人に共有リンクを渡したいなど、インターネットから届くようにする場合の設定と注意をまとめます。

| 方法 | 内容 |
|---|---|
| [ルーターでポートを開ける](#ルーターでポートを開けて公開する) | Caddy が直接受ける。fail2ban で遮断できる |
| [Cloudflare Tunnel](#ポートを開けずに公開するcloudflare-tunnel) | ポートを開けず、自宅の IP も出さない。Cloudflare が通信の中身を見られる位置にいる |
| [パスの下で動かす](#パスの下で動かす) | `https://example.com/docseed/` のように、ほかのサービスと名前を分け合う |
| [別の Web サーバーの下に置く](reverse-proxy.md) | Apache・nginx がすでに 80 / 443 を使っている |

## ルーターでポートを開けて公開する

Tailscale や LAN の中だけで使うのが基本ですが、ルーターでポートを開けて公開する場合に備えて、Caddy が次の制限をかけています（設定なしで有効。[設計](../design/security.md) 8 章）。

- LAN・Tailscale の外からは、管理画面（`/admin`）・管理用 API（`/api/v1/admin`）・API の説明ページ（`/api/docs`）・`/healthz` が 404 になる。管理の操作は Tailscale 経由で行う
- 外から IP アドレス（`DOCSEED_LAN_IP`）で開こうとすると、何も返さずに接続を切る
- `Permissions-Policy`・`Cross-Origin-Opener-Policy` を付け、`Server` などのヘッダを外す

「内側」の範囲は `.env` の `DOCSEED_TRUSTED_NETS` で変えられます（既定は `private_ranges 100.64.0.0/10`）。例えば LAN からも管理させず Tailscale だけにするなら `DOCSEED_TRUSTED_NETS=100.64.0.0/10 fd7a:115c:a1e0::/48` にして `docker compose up -d --force-recreate caddy`。

公開するときは、あわせて次も行ってください。

- 管理画面の「ログイン」で確認コード（2 段階認証）を必須にする（[ユーザーとログイン](users.md#2-段階認証を必須にする)）
- ルーターで開けるのは HTTPS のポートだけにする（80 は証明書の取得に使う場合だけ）。証明書は `acme_dns` などの公的なものにする
- 受け取り用の共有リンク（ログインなしでファイルを送ってもらう）は、期限が 30 日まで、1 リンク 10GB・全リンクで 24 時間 20GB まで、SSD の空きが 10% を下回ったら止まります（`.env` の `DOCSEED_SHARE_UPLOAD_*` で変更）
- ログインの失敗が続いてユーザー名がロックされても、以前ログインした端末とパスキーからはログインできます（攻撃者に締め出されないように）
- コンテナは読み取り専用・特権なしで動きます（`compose.yaml` の `x-hardening`。[設計](../design/deployment.md) 2.1.1）
- **IPv6 で公開する場合の注意**: Docker のポート公開では、IPv6 の接続は docker-proxy を通るため、接続元が Docker のゲートウェイ（`172.x.x.x`）に見え、上の制限をすり抜けます。IPv6 でも公開するなら `DOCSEED_TRUSTED_NETS` を `10.0.0.0/8 192.168.0.0/16 100.64.0.0/10 fd7a:115c:a1e0::/48` のように `172.16.0.0/12` を含まない値にしてください（そのぶん IPv6 で開いた LAN の端末も外側扱いになります）
- 外の回線（スマホのテザリングなど）から `https://<名前>/admin` と `https://<名前>/healthz` が 404 になることを確かめる
- ログイン以外にも、接続元の IP ごとに回数の上限があります（共有リンクの閲覧 1 分 600 回・受け取り 1 時間 30 回・無効なリンク 15 分 20 回、検索 1 分 60 回、ログインの開始 1 分 30 回）。超えると 429 を返します（`.env` の `DOCSEED_RATE_LIMIT=false` で止められます）

### 失敗が続く接続元をファイアウォールで遮断する（fail2ban）

api は、ログインの失敗・無効な共有リンク・回数の上限超えなどを `<DOCSEED_DATA_DIR>/logs/security.log` に 1 行ずつ書きます。fail2ban にこれを読ませ、10 分に 10 回を超えた接続元を 1 時間（繰り返すたびに長く、最長 1 週間）遮断します。LAN と Tailscale のアドレスは遮断しません。

```bash
sudo scripts/ops/install-fail2ban.sh          # fail2ban を入れて設定を置く（初回だけ）
sudo fail2ban-client status docseed           # 遮断中の IP
sudo fail2ban-client set docseed unbanip <IP> # 誤って遮断したときの解除
```

- Docker が公開したポートへの通信はホストの通常の規則（`INPUT`）を通らないため、Docker の `DOCKER-USER` チェーンで止めます（設定済み）
- 上の IPv6 の注意と同じ理由で、IPv6 で届いた接続は遮断できません（`172.x` に見えるため。遮断すると全員が止まる）
- Cloudflare Tunnel（次の節）を使う場合、通信は cloudflared から届くので fail2ban では止められません。Cloudflare の側（WAF・Access）で止めてください。アプリの中の回数の制限は、本当の接続元で数えるのでそのまま効きます

## ポートを開けずに公開する（Cloudflare Tunnel）

ルーターのポートを開けず、自宅の IP アドレスも出さずに公開できます。Pi から Cloudflare へ外向きにつなぎ、Cloudflare が受けた通信をそこから流します（[設計](../design/security.md) 10 章）。Cloudflare のアカウントと、Cloudflare で管理しているドメインが要ります。

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

## パスの下で動かす

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
- **外側の別のリバースプロキシ（nginx・Apache・別の Caddy）が `/docseed/` だけを docseed に転送する**: 手順・設定例・うまく行かないときの対処は [別の Web サーバーの下に置く](reverse-proxy.md) にまとめています。`Host` ヘッダは元のまま渡してください。接頭辞は付けたまま転送しても（`proxy_pass https://pi:8443;`）、外して転送しても（`proxy_pass https://pi:8443/;`）動きます。ブラウザから見たオリジンが `https://<DOCSEED_HOST>:<DOCSEED_HTTPS_PORT>` と違う（ポートを付けずに開くなど）ときは、`DOCSEED_PUBLIC_URL=https://example.com` も書きます（パスキーと Google のログインがこれを使います）
- **Cloudflare Tunnel**: 「Public hostname」の Path に `docseed` を入れ、ほかは [Cloudflare Tunnel](#ポートを開けずに公開するcloudflare-tunnel) の通りに設定します

注意:

- **同じオリジンの別のアプリから守られません。** ブラウザはパスで区別しないため、同じ名前の下の別のアプリに XSS などの穴があると、docseed の画面も操作されえます。Cookie の `Path` は `/docseed` に絞りますが、これでは防げません。同じ名前の下には、信頼できるものだけを置いてください
- **外側のプロキシが LAN にあると、全員が「内側」に見えます。** Caddy には外側のプロキシのアドレスが接続元として届くため、管理画面などの 404 の制限（[ルーターでポートを開けて公開する](#ルーターでポートを開けて公開する)）が効きません。外側のプロキシで `/docseed/admin`・`/docseed/api/v1/admin`・`/docseed/api/docs`・`/docseed/api/v1/openapi.json`・`/docseed/healthz` を外から開けないようにしてください。nginx の例:

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

## 関連

- 設定: [設定の一覧](configuration.md#https-と入口)
- 設計: [認証・権限・共有](../design/security.md) 8〜10 章、[配置・運用](../design/deployment.md) 3.1
