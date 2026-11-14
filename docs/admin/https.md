# HTTPS の証明書

docseed の入口の Caddy が HTTPS を受けます。証明書の方式は `.env` の `DOCSEED_TLS_MODE` で選びます。

| 方式 | 向いている場合 | ブラウザの警告 | パスキー |
|---|---|---|---|
| `internal`（既定） | とりあえず動かす | 出る（端末に CA を入れれば消える） | CA を入れた端末だけ |
| `file` | certbot などで取った証明書が Pi にある | 出ない | 使える |
| `tailscale`（**おすすめ**） | Pi と端末に Tailscale を入れている | 出ない | 使える |
| `acme_dns` | 自分のドメインがあり、DNS を Cloudflare で管理している | 出ない | 使える |

どの方式でも、`DOCSEED_LAN_IP` の IP アドレスで開いたときは内部 CA の証明書になります（パスキーは使えません）。

## 内部 CA（既定）の警告を消す

`internal` では Caddy が自分で CA を作って証明書を出します。端末にその CA の証明書を入れると、警告が消え、パスキーも使えます。

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./docseed-root.crt
```

この `docseed-root.crt` を使う端末に送り、「信頼する証明書」として入れます（iPhone はプロファイルを入れたあと「設定」→「一般」→「情報」→「証明書信頼設定」で有効にする）。端末が多いなら、次の `tailscale` などの方式の方が手間がかかりません。

## 手元の証明書を使う（certbot の Let's Encrypt の証明書など）

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

## Tailscale の証明書を使う（おすすめ）

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

## 自分のドメインの証明書を DNS-01 で取る

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

## 証明書の監視

api が 6 時間ごとに Caddy の証明書を確かめ、検証できない（名前が合わない・期限切れ）ときと、期限まで 7 日を切ったときに、管理画面の「確認が必要な項目」に出します。証明書の状態は管理画面の「HTTPS の証明書」で見られます。

## 関連

- 設定: [設定の一覧](configuration.md#https-と入口)
- うまく行かないとき: [困ったとき](troubleshooting.md#証明書)
- 設計: [拡張機能](../design/extensions.md) 2 章
