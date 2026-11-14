# 困ったとき（管理者向け）

利用者からの問い合わせが多いことは [利用者ガイドの困ったとき](../user/faq.md) にもまとめています。

まず次を見ます。

```bash
docker compose ps
```

```bash
docker compose logs --tail 100 caddy api worker
```

```bash
docker compose exec api docseed status
```

## 起動しない

| 症状 | 対処 |
|---|---|
| `permission denied ... docker.sock` | `sudo usermod -aG docker $USER` のあと、ログインし直す |
| `PermissionError: ... '/srv/docseed/...'` または「データディレクトリに書き込めません」 | コンテナは UID 1000 で動くため、データディレクトリの所有者を合わせる: `sudo chown -R 1000:1000 /srv/docseed`（`.env` の `DOCSEED_DATA_DIR` を変えている場合はその場所） |
| `api` がすぐ止まる | `docker compose logs api` を確認。`.env` の `DOCSEED_SECRET_KEY` 未設定や、`/srv/docseed` の所有者（UID 1000 が必要）が原因のことが多い |
| `DOCSEED_BASE_PATH の形が正しくありません` | `/docseed` のように、英数字と `- _ .` だけの接頭辞にする |
| `OIDC の提供者「…」には … が必要です` | `DOCSEED_OIDC_<名前>_ISSUER`・`_CLIENT_ID`・`_CLIENT_SECRET` の 3 つをそろえる（[ユーザーとログイン](users.md#google-などでログインするoidc)） |

## ポート・開けない

| 症状 | 対処 |
|---|---|
| `failed to bind host port 0.0.0.0:80`（または `:443`）`: address already in use` | 他のサービスがポートを使っています。`sudo ss -ltnp 'sport = :80'` で確認し、そのサービスを止めるか、`.env` の `DOCSEED_HTTP_PORT` / `DOCSEED_HTTPS_PORT` を空いている番号に変えて `docker compose up -d` |
| ポートを変えたのに、開くと Apache など 80 / 443 の別のサービスに飛ぶ・IP アドレスで開けない | `git pull` で最新にし（Caddy が中でも同じポートで待ち受ける設定）、IP で開くなら `.env` の `DOCSEED_LAN_IP` に Pi の IP を設定して `docker compose up -d --force-recreate caddy` |
| 管理画面（`/admin`）や `/api/docs` が 404 になる | LAN・Tailscale の外からの接続とみなされています（[インターネットに公開する](exposure.md#ルーターでポートを開けて公開する)）。Tailscale 経由で開くか、`.env` の `DOCSEED_TRUSTED_NETS` に接続元の範囲を足して `docker compose up -d --force-recreate caddy` |
| 共有リンクや検索で「アクセスが多すぎます」（429）になる | 接続元の IP ごとの回数の上限（[インターネットに公開する](exposure.md#ルーターでポートを開けて公開する)）。しばらく待つと戻ります。fail2ban を入れている場合は `sudo fail2ban-client status docseed` で遮断されていないかも確認 |

## 証明書

| 症状 | 対処 |
|---|---|
| `caddy` のログに `get_certificate tailscale` / `tailscaled.sock` のエラー | Pi で `tailscale status` が動いているか、管理画面で HTTPS Certificates が有効か、`DOCSEED_HOST` が `tailscale status --json` の `DNSName` と同じかを確認 |
| `caddy` のログに `module not registered: dns.providers.cloudflare` | `DOCSEED_TLS_MODE=acme_dns` には DNS のプラグイン入りの Caddy が要ります。`docker compose --profile acme build caddy-acme` と `.env` の `DOCSEED_CADDY_IMAGE=docseed-caddy:latest` |
| 証明書の警告で先に進めない（「この接続ではプライバシーが保護されません」で例外を追加するボタンが出ない） | 開いた名前と証明書が合っていません。そのドメインに HSTS（以前に Let's Encrypt の証明書で開いたなど）が付いていると、ブラウザは例外を許しません。[手元の証明書を使う](https.md#手元の証明書を使うcertbot-の-lets-encrypt-の証明書など)でその名前の証明書を Caddy に使わせてください |
| `caddy` が起動しない・`loading certificates` や `open /certs/...: no such file` のエラー | `.env` の `DOCSEED_TLS_CERT_DIR` と、その中の相対パス `DOCSEED_TLS_CERT` / `DOCSEED_TLS_KEY` を確認（`sudo ls -l /etc/letsencrypt/live/<名前>/`）。`DOCSEED_TLS_MODE` は `internal` / `file` / `tailscale` / `acme_dns` のどれか |
| パスキーを登録できない | 名前（`DOCSEED_HOST`）で、信頼された証明書で開いているか。IP アドレスでは使えない（[パスキーを使うための条件](users.md#パスキーを使うための条件)） |

## Cloudflare Tunnel

| 症状 | 対処 |
|---|---|
| `cloudflared` のログに `Unauthorized` / `Invalid tunnel secret` | `.env` の `DOCSEED_TUNNEL_TOKEN` を Cloudflare の管理画面のトークンに合わせて `docker compose up -d` |
| トンネル経由で開くと 502 になる | Public hostname の Service が `https://caddy:<DOCSEED_HTTPS_PORT>` か、`internal` の証明書なら No TLS Verify がオンかを確認。開いた名前が `DOCSEED_HOST` に入っているかも確認 |

## 処理が進まない

| 症状 | 対処 |
|---|---|
| ファイルが「処理に失敗」になり、ジョブのエラーに「モデルがありません」と出る | `docker compose --profile tools run --rm tools` でモデルを取得し、管理画面の「失敗したファイルをすべて再処理」 |
| ずっと「処理待ち」のまま | `docker compose ps` で worker が `Up` か。`docker compose logs worker` に温度での一時停止（`DOCSEED_THERMAL_PAUSE_C`）が出ていないか。管理画面の「文字の読み取りと検索の準備」で処理中のファイルを見る |
| worker がメモリ不足で止まる（`OOMKilled`） | 文字起こしのモデルを `ggml-base.bin` にする、`compose.yaml` の worker の `mem_limit` を上げる |
| 要約が作られない | `DOCSEED_LLM_ENABLED=true` と `COMPOSE_PROFILES` の `llm`、`docker compose logs llm` にモデルの読み込みのエラーが無いか（[小型の LLM](llm.md)） |
| Web クリップが「取得に失敗」 | `docker compose logs fetcher`。LAN の IP を指す名前・許可していないポートは取らない（[Web クリップ](webclip.md)） |
| 通知が届かない | 管理画面の「通知」の「テスト通知を送る」と、そこに出るエラー（[通知](notifications.md)） |

## データ

| 症状 | 対処 |
|---|---|
| ファイルとデータベースが合っているか確かめたい | `docker compose exec api docseed verify-files`（「問題はありません」なら正常。`--quick` で SHA-256 を省く） |
| データベースを戻したい | api と worker を止めて `docker compose run --rm --no-deps api docseed restore-db`（[バックアップと復旧](backup.md)） |
