# 設定の一覧

docseed の設定は、リポジトリの `.env`（`cp .env.example .env` で作る）に書きます。`.env` は Git に入れないでください（秘密鍵・トークンが入ります）。

- **反映**: `.env` を変えたら `docker compose up -d`（変わったコンテナが作り直されます）。Caddy の設定（HTTPS と入口）は `docker compose up -d --force-recreate caddy`
- **使う所**: その値を読むコンテナ。`caddy` / `api` / `worker` / `fetcher`（Web クリップ）/ `webclip-browser`（描画）/ `llm` / `compose`（`compose.yaml` が読む）/ `ops`（`scripts/ops/` のスクリプト）
- 空や書いていない設定は、既定の値で動きます
- 秘密の値（**秘密**の印）は人に見せないでください。バックアップには `.env` も含まれます

この一覧は `tests/test_docs.py` が、コードにある `DOCSEED_*` と照らし合わせています。設定を足したら、ここにも足してください。

## 必須

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_HOST` | なし | caddy・api | ブラウザで開く名前。カンマで複数書ける（先頭がパスキーの登録先・通知のリンクの名前） |
| `DOCSEED_SECRET_KEY` | なし | api | **秘密**。Cookie の署名の鍵。`python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |

## 保存とファイル

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_DATA_DIR` | `/srv/docseed` | compose・ops | ホスト側のデータディレクトリ（SSD 上。持ち主は UID 1000）。コンテナの中では常に `/srv/docseed` |
| `DOCSEED_MAX_UPLOAD_MB` | `1024` | api | 1 ファイルのアップロードの上限（MB） |
| `DOCSEED_TRASH_RETENTION_DAYS` | `30` | api | ゴミ箱に残す日数の既定（利用者は設定画面で変えられる） |
| `DOCSEED_VERSIONS_KEEP` | `10` | api | 1 ファイルに残す過去の版の数。`0` なら残さない |
| `DOCSEED_TIMEZONE` | `Asia/Tokyo` | api | 画面に出す時刻のタイムゾーン |
| `DOCSEED_LOG_LEVEL` | `INFO` | api・worker | ログの細かさ（`DEBUG` / `INFO` / `WARNING`） |
| `DOCSEED_COOKIE_SECURE` | `true` | api | HTTPS 用の Cookie にする。HTTP で直接試すときだけ `false` |

## HTTPS と入口

[HTTPS の証明書](https.md)・[インターネットに公開する](exposure.md)・[別の Web サーバーの下に置く](reverse-proxy.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_LAN_IP` | 空 | caddy | IP アドレスでも開くときの Pi の LAN の IP（内部 CA の証明書になる） |
| `DOCSEED_HTTPS_PORT` | `443` | caddy・api | Caddy の HTTPS のポート。443 以外なら URL にポートを付けて開く |
| `DOCSEED_HTTP_PORT` | `80` | caddy | Caddy の HTTP のポート（HTTPS へ転送する） |
| `DOCSEED_TLS_MODE` | `internal` | caddy・api | `internal` / `file` / `tailscale` / `acme_dns` |
| `DOCSEED_TLS_CERT_DIR` | `./docker/certs` | compose | `file` のとき: 証明書のあるホスト側のディレクトリ（certbot なら `/etc/letsencrypt`） |
| `DOCSEED_TLS_CERT` | 空 | caddy | `file` のとき: `DOCSEED_TLS_CERT_DIR` からの証明書の相対パス |
| `DOCSEED_TLS_KEY` | 空 | caddy | `file` のとき: 秘密鍵の相対パス |
| `DOCSEED_TAILSCALE_SOCKET_DIR` | `/var/run/tailscale` | compose | `tailscale` のとき: `tailscaled.sock` のあるディレクトリ |
| `DOCSEED_CADDY_IMAGE` | `caddy:2` | compose | `acme_dns` のとき `docseed-caddy:latest`（`docker compose --profile acme build caddy-acme` で作る） |
| `DOCSEED_ACME_DNS_TOKEN` | 空 | caddy | **秘密**。`acme_dns` のときの DNS 事業者の API トークン（Cloudflare は Zone:DNS:Edit だけ） |
| `DOCSEED_ACME_EMAIL` | 空 | caddy | Let's Encrypt に登録する連絡先 |
| `DOCSEED_ACME_STAGING` | `false` | caddy | `true` で Let's Encrypt の試験用の環境を使う |
| `DOCSEED_TLS_CHECK_ADDR` | `caddy:<HTTPS_PORT>` | api | 証明書の監視で接続する先。compose が設定する（ふつうは書かない）。空なら監視しない |
| `DOCSEED_TRUSTED_NETS` | `192.168.0.0/16 10.0.0.0/8 127.0.0.1/8 fd00::/8 ::1 100.64.0.0/10` | caddy | 「内側」とみなす接続元（空白区切りの CIDR）。外側からは管理画面などが 404。`172.16.0.0/12` は既定に入れていない（Docker のゲートウェイ。LAN がこの範囲なら足す） |
| `DOCSEED_RATE_LIMIT` | `true` | api | ログイン以外の、接続元ごとの回数の上限 |
| `DOCSEED_TUNNEL_TOKEN` | 空 | compose | **秘密**。Cloudflare Tunnel のトークン（`COMPOSE_PROFILES` に `tunnel`） |
| `DOCSEED_TUNNEL_SUBNET` | `10.213.47.0/28` | compose・caddy | cloudflared だけが入るネットワーク。重なるときだけ変える |
| `DOCSEED_FRONT_SUBNET` | `10.213.47.16/28` | compose・api | Caddy と api だけが入るネットワーク。api はここから届いた `X-Forwarded-For` だけを信頼する。重なるときだけ変える |
| `DOCSEED_CLOUDFLARED_IMAGE` | `cloudflare/cloudflared:latest` | compose | cloudflared のイメージ |
| `DOCSEED_BASE_PATH` | 空 | api | パスの下で動かすときの接頭辞（例 `/docseed`） |
| `DOCSEED_PUBLIC_URL` | 空 | api | ブラウザから見たオリジン。外側のプロキシなどで `https://<HOST>:<HTTPS_PORT>` と違うときだけ（パスまで書くと接頭辞にもなる） |

## ログイン

[ユーザーとログイン](users.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_RP_ID` | `DOCSEED_HOST` の先頭 | api | パスキーを登録する名前。**変えると登録済みのパスキーは使えなくなる** |
| `DOCSEED_OIDC_PROVIDERS` | 空 | api | 外部アカウントの提供者の名前（カンマ区切り。例 `google,pocketid`） |
| `DOCSEED_OIDC_<名前>_ISSUER` | なし | api | 提供者の issuer（Google は `https://accounts.google.com`） |
| `DOCSEED_OIDC_<名前>_CLIENT_ID` | なし | api | クライアント ID |
| `DOCSEED_OIDC_<名前>_CLIENT_SECRET` | なし | api | **秘密**。クライアントシークレット |
| `DOCSEED_OIDC_<名前>_LABEL` | 名前 | api | ログイン画面のボタンの表示（例 `Google`） |

`<名前>` は `DOCSEED_OIDC_PROVIDERS` に書いた名前を大文字にしたもの（`-` は `_`）。例: `DOCSEED_OIDC_GOOGLE_ISSUER`・`DOCSEED_OIDC_GOOGLE_CLIENT_ID`・`DOCSEED_OIDC_GOOGLE_CLIENT_SECRET`・`DOCSEED_OIDC_GOOGLE_LABEL`。

## 共有リンクの受け取り

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_SHARE_UPLOAD_LINK_GB` | `10` | api | 受け取り用のリンク 1 つで受け取る合計（GB） |
| `DOCSEED_SHARE_UPLOAD_DAILY_GB` | `20` | api | すべての受け取り用のリンクで、直近 24 時間に受け取る合計（GB） |
| `DOCSEED_SHARE_UPLOAD_MIN_FREE_PCT` | `10` | api | 保存先の空きがこの割合（%）を下回ったら受け取らない |

## 文字の読み取りと検索

[検索と候補の調整](tuning.md)。ふつうは変えません（Phase 0 の計測で決めた値）。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_OCR_ENGINE` | `ppocrv6_small` | worker | OCR のモデル。`ppocrv6_medium` は精度重視（約 4 倍遅い） |
| `DOCSEED_OCR_DPI` | `200` | worker | PDF を画像にするときの解像度 |
| `DOCSEED_OCR_DET_SIDE` | `1600` | worker | 文字の領域の検出に使う画像の長辺（px） |
| `DOCSEED_OCR_MIN_SCORE` | `0.5` | worker・api | これ未満の確かさの行は本文に使わない |
| `DOCSEED_OCR_PDF_IMAGES` | `true` | worker | テキスト層のある PDF でも、貼られた図・写真の中の文字を読む |
| `DOCSEED_OCR_THREADS` | `3` | worker | OCR のスレッド数 |
| `DOCSEED_EMBED_MODEL` | `cl-nagoya/ruri-v3-70m` | worker・api | 埋め込みのモデル（変えると全文書の再処理が要る） |
| `DOCSEED_EMBED_THREADS` | `3` | worker | 埋め込みのスレッド数 |
| `DOCSEED_QUERY_THREADS` | `1` | api | 検索語の埋め込みのスレッド数 |
| `DOCSEED_SEARCH_SEM_MIN` | `0.80` | api | 意味検索だけで見つかった部分を結果に含める類似度の下限（0〜1） |

## 監視

[状態の確認](monitoring.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_THERMAL_PAUSE_C` | `80` | worker | CPU がこの温度（℃）を超えたら処理を一時停止する |
| `DOCSEED_THERMAL_WARN_C` | `75` | api | この温度（℃）を超えたら警告する |
| `DOCSEED_MEMORY_WARN_PCT` | `10` | api | 使えるメモリがこの割合（%）を下回ったら警告する |

## 通知

[通知](notifications.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_NOTIFY_URL` | 空 | api | **秘密**。異常の通知の送り先（ntfy・Discord・Slack・任意の JSON の Webhook）。[異常の通知と更新](security-notify.md) |
| `DOCSEED_NOTIFY_FORMAT` | `auto` | api | 異常の通知の形式（`auto` / `ntfy` / `discord` / `slack` / `json`。`auto` は URL から決める） |
| `DOCSEED_NOTIFY_LOGIN_FAILURES` | `20` | api | 15 分のログインの失敗がこの回数以上になったら知らせる |
| `DOCSEED_UPDATE_REBUILD` | `false` | ホストの `/etc/docseed/update.env` | `true` にすると、週ごとの更新で docseed 自身のイメージも作り直す |
| `DOCSEED_UPDATE_CONF` | `/etc/docseed/update.env` | `scripts/ops/update-images.sh` | 更新の設定ファイルの場所 |
| `DOCSEED_NOTIFY_URLS` | 空 | api | **秘密**。Webhook の URL（カンマか空白区切り。ntfy・Discord・Slack は形式を見分ける。`ntfy+https://…` のように形式を前に付けることもできる） |
| `DOCSEED_NOTIFY_EMAIL` | 空 | api | メールの宛先（カンマ区切り。`DOCSEED_SMTP_HOST` も要る） |
| `DOCSEED_NOTIFY_EVENTS` | `alert,share_upload,inbox` | api | 知らせること（`alert` 監視の警告 / `share_upload` 共有リンクでの受け取り / `inbox` 受け取り箱） |
| `DOCSEED_SMTP_HOST` | 空 | api | SMTP サーバー |
| `DOCSEED_SMTP_PORT` | `587` | api | SMTP のポート |
| `DOCSEED_SMTP_SECURITY` | `starttls` | api | `starttls`（587）/ `ssl`（465）/ `none` |
| `DOCSEED_SMTP_USER` | 空 | api | SMTP のユーザー |
| `DOCSEED_SMTP_PASSWORD` | 空 | api | **秘密**。SMTP のパスワード |
| `DOCSEED_SMTP_FROM` | `DOCSEED_SMTP_USER` | api | 差出人 |

## 受け取り箱

[受け取り箱](inbox.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_INBOX_ENABLED` | `false` | worker | `<DATA_DIR>/inbox/<ユーザー名>/` のファイルを各ユーザーの Inbox へ取り込む |
| `DOCSEED_INBOX_KEEP` | `false` | worker | 取り込んだ元のファイルを消さず `.imported/` に移す |

## Web クリップ

[Web クリップ](webclip.md)。`COMPOSE_PROFILES` に `webclip`（描画も使うなら `webclip-render`）を足します。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_WEBCLIP_ENABLED` | `false` | api・fetcher | URL の取り込みを有効にする |
| `DOCSEED_WEBCLIP_MAX_MB` | `5` | fetcher | HTML の大きさの上限（MB）。PDF・画像は `DOCSEED_MAX_UPLOAD_MB` まで |
| `DOCSEED_WEBCLIP_PROXY` | 空 | fetcher | 取得に使うプロキシ。**プロキシの側で LAN への通信を止めること** |
| `DOCSEED_WEBCLIP_RENDER_URL` | 空 | fetcher | 描画サービスの URL（`http://webclip-browser:8080`）。空なら描画しない |
| `DOCSEED_RENDER_NO_SANDBOX` | `0` | webclip-browser | `1` で Chromium のサンドボックスを外す（動かない機械だけ） |
| `DOCSEED_RENDER_PORT` | `8080` | webclip-browser | 描画サービスの待ち受けのポート（ふつうは変えない） |
| `DOCSEED_RENDER_CHROMIUM` | 空 | webclip-browser | Chromium の実行ファイル（空なら Playwright の同梱のもの） |

## 整理の補助と LLM

[小型の LLM](llm.md)。LLM を使うときは `COMPOSE_PROFILES` に `llm` を足します。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_SUGGEST_ENABLED` | `true` | api・worker | 置き場所・タグの候補 |
| `DOCSEED_LLM_ENABLED` | `false` | api・worker | 要約・タグの提案・質問に答える |
| `DOCSEED_LLM_MODEL` | `LFM2.5-1.2B-JP-Q4_K_M.gguf` | llm・api・worker | `models/llm/` の GGUF のファイル名 |
| `DOCSEED_LLM_URL` | `http://llm:8080` | api・worker | llama-server の URL（ふつうは変えない） |
| `DOCSEED_LLM_IMAGE` | `ghcr.io/ggml-org/llama.cpp:server` | compose | llama-server のイメージ |
| `DOCSEED_LLM_THREADS` | `3` | llm | スレッド数 |
| `DOCSEED_LLM_TIMEOUT_S` | `180` | worker・api | 1 件の上限（秒） |
| `DOCSEED_LLM_MAX_INPUT_CHARS` | `3000` | worker・api | 渡す本文の上限（文字） |
| `DOCSEED_LLM_PARALLEL` | `2` | llm | 同時の枠。メモリが足りなければ `1` |
| `DOCSEED_LLM_CTX` | `8192` | llm | 文脈の長さ（枠ごとに `CTX / PARALLEL`）。`PARALLEL=1` なら `4096` |

## 音声・動画

[文字起こし](asr.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `DOCSEED_IMAGE_TARGET` | `base` | compose | `asr` にすると ffmpeg と whisper-cli を入れたイメージを作る |
| `DOCSEED_ASR_ENABLED` | `false` | worker | 文字起こしを有効にする |
| `DOCSEED_ASR_MODEL` | `ggml-small.bin` | worker | `models/asr/` の ggml のファイル名 |
| `DOCSEED_ASR_BIN` | `whisper-cli` | worker | whisper.cpp の実行ファイル（ふつうは変えない） |
| `DOCSEED_ASR_LANGUAGE` | `ja` | worker | 言語 |
| `DOCSEED_ASR_THREADS` | `3` | worker | スレッド数 |
| `DOCSEED_ASR_TIMEOUT_S` | `1800` | worker | 10 分の区切り 1 つの上限（秒） |
| `DOCSEED_VIDEO_PROXY` | `true` | worker | 映像の保存時に軽い再生用の動画を作る |

## compose のプロファイル

`COMPOSE_PROFILES` に、使う任意のコンテナをカンマ区切りで書きます（例 `COMPOSE_PROFILES=webclip,llm,tunnel`）。

| プロファイル | 起動するもの |
|---|---|
| `webclip` | fetcher（Web クリップの取得） |
| `webclip-render` | webclip-browser（JavaScript の描画） |
| `llm` | llm（llama-server） |
| `tunnel` | cloudflared |
| `acme` | caddy-acme（DNS のプラグイン入りの Caddy をビルドするときだけ） |
| `tools` | tools（モデルの取得。`docker compose --profile tools run --rm tools`） |

## バックアップのスクリプト

`.env` ではなく `/etc/docseed/backup.env`（`scripts/ops/backup.env.example` から作る）に書きます。[バックアップと復旧](backup.md)。

| 設定 | 既定 | 使う所 | 内容 |
|---|---|---|---|
| `RESTIC_REPOSITORY` | なし | ops | restic の保存先 |
| `RESTIC_PASSWORD_FILE` | なし | ops | restic のパスワードのファイル（**秘密**。失うと復元できない） |
| `DOCSEED_KEEP_DAILY` / `DOCSEED_KEEP_WEEKLY` / `DOCSEED_KEEP_MONTHLY` | `7` / `4` / `6` | ops | 残す世代 |
| `DOCSEED_CHECK_SUBSET` | `5%` | ops | 週 1 回の検査で読み出すデータの割合 |
| `DOCSEED_DRILL_DIR` | `/var/tmp` | ops | 復旧の練習で復元する場所（復元する分の空きが要る） |
| `DOCSEED_DRILL_MARGIN` | `1.2` | ops | 復元する大きさの何倍の空きがあれば練習するか |
| `DOCSEED_DIR` | スクリプトの 2 つ上 | ops | リポジトリの場所 |
| `DOCSEED_BACKUP_CONF` | `/etc/docseed/backup.env` | ops | この設定ファイルの場所 |
| `DOCSEED_EXEC` | `docker compose … exec -T api docseed` | ops | docseed のコマンドの呼び方（試験用） |
| `DOCSEED_LOCAL` | `0` | ops | `1` で復旧の練習をコンテナを使わずに行う（試験用） |

## スクリプトの環境変数

| 設定 | 使う所 | 内容 |
|---|---|---|
| `DOCSEED_URL` | `scripts/eval_search.py` | docseed の URL |
| `DOCSEED_TOKEN` | `scripts/eval_search.py` | **秘密**。API トークン |
