# 導入

## 用意するもの

| 項目 | 内容 |
|---|---|
| 機械 | Raspberry Pi 5（RAM 8GB 以上。小型の LLM を使うなら 16GB）、アクティブクーラー |
| 保存先 | NVMe SSD（256GB 以上）。データディレクトリ（既定 `/srv/docseed`）を SSD に置く |
| OS | Raspberry Pi OS 64-bit |
| ソフト | Docker Engine と Compose プラグイン（`docker compose version` が動くこと） |
| ネットワーク | LAN の中で使うのが基本。外からは Tailscale などの VPN を推奨（[インターネットに公開する](exposure.md)） |

## 手順

```bash
git clone https://github.com/kimu1109/docseed.git && cd docseed
```

```bash
sudo mkdir -p /srv/docseed && sudo chown 1000:1000 /srv/docseed
```

コンテナは UID 1000 で動くので、データディレクトリの持ち主を 1000 にします。

```bash
cp .env.example .env
```

`.env` を編集します。**必須は 2 つ** です。

| 設定 | 値 |
|---|---|
| `DOCSEED_HOST` | ブラウザで開く名前（例 `docseed.local`、`<機械名>.<tailnet>.ts.net`） |
| `DOCSEED_SECRET_KEY` | Cookie の署名の秘密鍵。`python3 -c "import secrets; print(secrets.token_urlsafe(48))"` で作る |

IP アドレスでも開くなら `DOCSEED_LAN_IP` に Pi の LAN の IP を書きます。80 / 443 番がほかのサービスで使われているなら `DOCSEED_HTTP_PORT` / `DOCSEED_HTTPS_PORT` を変えます。ほかの設定は [設定の一覧](configuration.md)。

モデルを取得します（初回だけ。OCR と埋め込みのモデルを `/srv/docseed/models` に置きます。10〜20 分）。

```bash
docker compose --profile tools run --rm --build tools
```

起動します。

```bash
docker compose up -d --build
```

管理者を作ります（パスワードは対話で入れます）。

```bash
docker compose exec api docseed create-admin --username <名前>
```

ブラウザで `https://<DOCSEED_HOST>/` を開いてログインします。

- 既定では Caddy の内部 CA の証明書を使うため、初回はブラウザの警告が出ます。警告を出さない方法とパスキーを使う条件は [HTTPS の証明書](https.md)
- 起動しないときは [困ったとき](troubleshooting.md)

## 確かめる

```bash
docker compose ps
```

`caddy`・`api`・`worker` が `Up` になっていれば動いています。試しにファイルを 1 つアップロードし、状態が「検索できます」になれば、文字の読み取り（worker）も動いています。

```bash
docker compose logs -f worker
```

処理の様子が見られます（`Ctrl+C` で抜けます）。進み具合は管理画面の「文字の読み取りと検索の準備」でも見られます（処理中のファイルのページの進み・開始時刻・試行回数と、待っているファイルの順番。5 秒ごとに更新）。

## 補足

- `tools` イメージは、モデルを ONNX 形式に変換するための torch を含むので大きめです。変換が終われば消して構いません: `docker image rm docseed-tools`
- worker は外部と通信しない設定です。モデルが無いと起動直後のジョブが失敗するので、先に `tools` を実行してください
- テキスト層のある PDF でも、ページに貼られた図・写真の中の文字を読み取ります（ページ全体を覆うスキャン画像は除く。止めるには `DOCSEED_OCR_PDF_IMAGES=false`）

## 次に

1. [HTTPS の証明書](https.md) を決める（パスキーを使うなら必須）
2. [ユーザーとログイン](users.md) でユーザーを足す
3. [バックアップと復旧](backup.md) を設定する
4. 使う人に、開く URL と、画面の左のメニューの「ヘルプ」に [利用者ガイド](../user/README.md) があることを伝える
