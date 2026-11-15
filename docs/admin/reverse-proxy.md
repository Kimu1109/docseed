# 別の Web サーバーの下に置く（Apache・nginx、パスの下で動かす）

Pi の 80 / 443 番を Apache や nginx がすでに使っていて、同じ名前の下（例 `https://example.com/docseed/`）で docseed も開きたい場合の手順です。

- 例の値は自分の環境に読み替えてください: 名前 `example.com`、接頭辞 `/docseed`、docseed の Caddy のポート `6060` / `6443`
- 接頭辞を付けず、docseed 専用の名前の直下（`https://docs.example.com/`）で開く場合も同じ考え方です。最後の「接頭辞を付けない場合」を見てください

## しくみ

```
ブラウザ ──https://example.com/docseed/…──▶ Apache / nginx（80・443）
                                              │ /docseed/ だけを転送
                                              ▼
                                         docseed の Caddy（6443）──▶ api
```

- 80 / 443 番は、今までの Web サーバーが受けたままにします。docseed の Caddy は別のポート（`6060` / `6443`）で待ち受けます
- Web サーバーは `/docseed/` で始まるものだけを Caddy に転送します。ほかのパスは今まで通り、そのサーバーのサイトが返します
- docseed は、画面のリンク・リダイレクト・Cookie などに `/docseed` を付けて返します（`DOCSEED_BASE_PATH`）

## 1. docseed の設定

`.env`:

```bash
# Caddy のポート（80 / 443 は Web サーバーが使っているので、空いている番号にする）
DOCSEED_HTTPS_PORT=6443
DOCSEED_HTTP_PORT=6060

# パスの下で動かす
DOCSEED_BASE_PATH=/docseed

# ブラウザで開く名前（複数あるなら、これを先頭にする。パスキーはこの名前で登録される）
DOCSEED_HOST=example.com
# ブラウザから見たオリジン（ポートなし）。パスキーと Google のログインがこれを使う
DOCSEED_PUBLIC_URL=https://example.com
```

ポートを変えたときは **Caddy も作り直す**必要があります。サービスの名前を付けず、全部を作り直してください。

```bash
docker compose up -d --force-recreate
```

```bash
docker compose ps
```

`caddy`・`api`・`worker` が `Up` になっていれば次へ進みます。

Pi の中から、Caddy が直接返すことを確かめます（Web サーバーを通さない）。

```bash
curl -skI --resolve example.com:6443:127.0.0.1 https://example.com:6443/docseed/
```

`HTTP/2 303` と `location: /docseed/login?next=/` が返れば、docseed の側はできています。

## 2a. Apache

### 設定は別ファイルに書き、サイトの中から読み込む

転送の設定は、**その名前の 443 番の `VirtualHost` の中**で効く必要があります。次のどちらでもなく、「別ファイル + `Include`」にします。

| やり方 | 結果 |
|---|---|
| `sites-available/` に、同じ `ServerName` の `<VirtualHost *:443>` をもう 1 つ作る | 同じ名前の `VirtualHost` が 2 つになり、Apache は先に読んだほうしか使わない。docseed が出ないか、今のサイトが出なくなる |
| `conf-available/` に置いて `a2enconf` | サーバー全体に効くので、ほかのサイトにも `/docseed/` の転送が付く |
| **別ファイルに書き、`VirtualHost` の中で `Include`** | その名前のサイトにだけ効く（おすすめ） |

**(1) 転送の設定を書く**

自動では読み込まれない場所に置きます。

```bash
sudo mkdir -p /etc/apache2/snippets
```

```bash
sudo nano /etc/apache2/snippets/docseed.conf
```

```apache
# docseed（/docseed/ だけを docseed の Caddy へ転送する）。
# サイトの <VirtualHost *:443> の中から Include して使う（単独では読み込まない）

# Caddy へは HTTPS でつなぐ。Caddy の証明書は内部 CA のことがあるので検証しない（同じ Pi の中の通信）
SSLProxyEngine on
SSLProxyVerify none
SSLProxyCheckPeerName off
SSLProxyCheckPeerExpire off

# Host を元のまま渡す（Caddy は DOCSEED_HOST の名前で受ける）
ProxyPreserveHost On
# 大きなファイルのアップロード・ダウンロードで切れないように
ProxyTimeout 600
RequestHeader set X-Forwarded-Proto https

# 管理系のパスは、この経路では開かせない（「制限」の節を参照）。
# 管理は LAN・Tailscale から Caddy に直接つないで行う: https://example.com:6443/docseed/admin
<LocationMatch "^/docseed/(?i:admin|api/v1/admin|api/docs|api/v1/openapi\.json|healthz)(/|$)">
    Require all denied
</LocationMatch>

# 「/docseed」だけで開いたとき
RedirectMatch 301 ^/docseed$ /docseed/

# 6443 は .env の DOCSEED_HTTPS_PORT
ProxyPass        /docseed/ https://127.0.0.1:6443/docseed/ flushpackets=on
ProxyPassReverse /docseed/ https://127.0.0.1:6443/docseed/
```

**(2) サイトの設定に `Include` の 1 行だけ足す**

その名前の `<VirtualHost *:443>` があるファイルを開きます。certbot で証明書を取っているなら、ふつうは `/etc/apache2/sites-available/000-default-le-ssl.conf` などの `…-le-ssl.conf` です。`</VirtualHost>` の手前に足します。

```apache
<VirtualHost *:443>
    ServerName example.com
    # ...（今までの設定）...

    Include /etc/apache2/snippets/docseed.conf
</VirtualHost>
```

- どのファイルか分からないときは `sudo apachectl -S` を実行します。`*:443` の行に、その名前とファイルの場所が出ます
- certbot の証明書の更新（`certbot renew`）では `…-le-ssl.conf` の中身は書き換わらないので、足した行は残ります

**(3) 必要なモジュールを有効にして反映する**

```bash
sudo a2enmod proxy proxy_http ssl headers
```

```bash
sudo apachectl configtest && sudo systemctl reload apache2
```

`Syntax OK` の後に再読み込みされます。ブラウザで `https://example.com/docseed/` を開きます。

### アップロードの大きさ

Apache 2.4.54 以降は、リクエストの本文の上限（`LimitRequestBody`）が既定で 1GiB です。docseed の上限（`DOCSEED_MAX_UPLOAD_MB`、既定 1024MB）より大きくした場合は、`docseed.conf` に `LimitRequestBody 0`（上限なし。docseed の側で止める）を足します。

## 2b. nginx

その名前の `server { listen 443 ssl; ... }` の中に書きます（`include /etc/nginx/snippets/docseed.conf;` で別ファイルにしても構いません）。

```nginx
# 管理系のパスは、この経路では開かせない
location ~* ^/docseed/(admin|api/v1/admin|api/docs|api/v1/openapi\.json|healthz)(/|$) { return 404; }

location = /docseed { return 301 /docseed/; }

location /docseed/ {
    proxy_pass https://127.0.0.1:6443;     # 末尾に / を付けない（接頭辞を付けたまま渡す）
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_ssl_server_name on;
    proxy_ssl_name $host;                  # Caddy は DOCSEED_HOST の名前の証明書を出す
    client_max_body_size 1g;               # DOCSEED_MAX_UPLOAD_MB に合わせる
    proxy_request_buffering off;           # アップロードを溜め込まずに流す
    proxy_buffering off;                   # ダウンロード・質問への回答を溜め込まずに流す
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 3. 確かめる

外の回線（スマホのテザリングなど）から確かめます。

| 開く URL | 期待する結果 |
|---|---|
| `https://example.com/docseed/` | docseed のログイン画面 |
| `https://example.com/docseed` | `/docseed/` に移る |
| `https://example.com/` | 今までのサイト（docseed ではない） |
| `https://example.com/docseed/admin` | 403 か 404（Web サーバーが止める） |
| `https://example.com/docseed/healthz` | 403 か 404 |

続けて、ログイン・アップロード・ダウンロード・共有リンクを一通り試します。パスキーを使うなら、設定画面で登録してからログインできることも確かめます。

## 4. うまく行かないとき

| 症状 | 原因と対処 |
|---|---|
| ポートを変えたのに、前のポートで開ける・新しいポートで開けない | Caddy のコンテナが作り直されていない。`docker compose up -d --force-recreate`（サービスの名前を付けない）で全部を作り直し、`docker compose ps` で `caddy` が `Up` か確かめる |
| `DOCSEED_HTTPS_PORT=443` にしたら Caddy が起動しない | 443 を Web サーバーが使っている。docseed の Caddy は別のポートのままにし、Web サーバーから転送する（この文書のやり方） |
| `/docseed/` を開くと Web サーバーの 404 が出る | 転送の設定が効いていない。Apache なら `Include` がその名前の `<VirtualHost *:443>` の中にあるか（`sudo apachectl -S`）、`a2enmod proxy proxy_http ssl headers` をしたか、再読み込みしたかを確かめる。80 番（HTTP）の `VirtualHost` にだけ書いていないかも見る |
| 502 / 503（Proxy Error） | Web サーバーから Caddy につながらない。上の `curl -skI --resolve …` で Caddy が返すか確かめる。Apache なら `SSLProxyEngine on` があるか、`/var/log/apache2/error.log` に何が出ているか |
| `/docseed/docseed/…` になる・ログイン後に `/` に飛ぶ | 接頭辞の付け方が二重・不足。Apache は `ProxyPass /docseed/ https://127.0.0.1:6443/docseed/`、nginx は `proxy_pass https://127.0.0.1:6443;`（末尾の / なし）の形にする。docseed の側の `DOCSEED_BASE_PATH` も確かめる |
| 開くと Caddy の画面が出ない・証明書のエラー・「接続できない」がすぐ出る | `DOCSEED_HOST` が、ブラウザで開く名前と違う。Caddy はその名前でだけ応答する。`ProxyPreserveHost On`（nginx は `proxy_set_header Host $host;`）も必要 |
| パスキーで「パスキーは … で開いたときに使えます」と出る | `DOCSEED_HOST` の先頭の名前（パスキーの RP ID）が、ブラウザで開いている名前と違う。先頭をその名前にする |
| パスキーの登録・ログインが失敗する（上のメッセージは出ない） | `DOCSEED_PUBLIC_URL` がブラウザのオリジン（`https://example.com`。ポートなし・パスなし）と違う |
| 大きなファイルのアップロードが途中で失敗する | 画面からのアップロードは 8MB ずつに分けて送り、切れても続きから再開します（api.md 3.3.2）。プロキシが `PATCH`・`HEAD` と `Upload-*`・`Tus-*` のヘッダを通しているか確かめてください。API の `POST /api/v1/documents` で 1 回で送る場合は、Apache は `LimitRequestBody`、nginx は `client_max_body_size` を `DOCSEED_MAX_UPLOAD_MB` 以上にし、時間で切れるなら `ProxyTimeout` / `proxy_read_timeout` を延ばす |
| 画面の表示が崩れる・ボタンが動かない | 更新前の JS が残っている。ブラウザで強制再読み込み（キャッシュの破棄）をする |

ログの見方:

```bash
docker compose logs caddy | tail -n 30
```

```bash
docker compose logs api | tail -n 30
```

```bash
sudo tail -n 30 /var/log/apache2/error.log
```

## 5. 制限

- **管理画面は、Web サーバー経由では開けません。** Web サーバーから Caddy に届く通信は、接続元がすべて Pi 自身（Docker の内部アドレス）に見えます。そのため Caddy の「外からは管理系のパスを 404 にする」制限が効かず、Web サーバーの側で止めています。管理は、LAN か Tailscale から Caddy に直接つないで行ってください（`https://example.com:6443/docseed/admin`）
- **接続元ごとの回数の制限が、全員の合計で数えられます。** 同じ理由で、アプリからは全員が同じ接続元に見えるためです。ログインの失敗の制限・検索の回数の上限などが全員まとめて数えられ、fail2ban も個別の接続元を遮断できません。一人や家族で使う分には困りませんが、多くの人に公開するなら、[Cloudflare Tunnel](exposure.md#ポートを開けずに公開するcloudflare-tunnel)か Caddy での直接の公開を検討してください
- **同じ名前の下の別のアプリからは守られません。** ブラウザは同じ名前（オリジン）の中をパスで区別しないため、`example.com` の別のアプリに XSS などの穴があると、docseed の画面も操作されえます。同じ名前の下には信頼できるものだけを置いてください
- 後から接頭辞を付けたり名前を変えたりした場合の影響（共有リンク・ブックマークレット・パスキー・Google のリダイレクト URI）は、[パスの下で動かす](exposure.md#パスの下で動かす)を見てください

## 接頭辞を付けない場合（専用の名前の直下）

`https://docs.example.com/` のように、docseed 専用の名前を Web サーバーで受けて転送する場合は、次のようにします。

- `.env` の `DOCSEED_BASE_PATH` は書かない（空）。`DOCSEED_HOST` と `DOCSEED_PUBLIC_URL` はその名前にする
- Apache は、その名前の `<VirtualHost *:443>` を新しく作り（ほかのサイトと `ServerName` が違うので、別のサイトとして作って構いません）、上の `docseed.conf` の `/docseed/` を `/` に読み替えて書く。`RedirectMatch` の行は要らない:

  ```apache
  <LocationMatch "^/(?i:admin|api/v1/admin|api/docs|api/v1/openapi\.json|healthz)(/|$)">
      Require all denied
  </LocationMatch>
  ProxyPass        / https://127.0.0.1:6443/ flushpackets=on
  ProxyPassReverse / https://127.0.0.1:6443/
  ```

- nginx も同じく `location /` で `proxy_pass https://127.0.0.1:6443;` にする
