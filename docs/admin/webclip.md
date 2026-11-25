# URL の取り込み（Web クリップ）

Web ページの URL を渡すと、ページを取得して保存し、タイトル・説明・本文で検索できるようにします。PDF や画像の URL は、そのファイルを普通のファイルとして保存します。既定では無効です（Pi からインターネットへ通信するため）。

## 有効にする

```bash
# .env
DOCSEED_WEBCLIP_ENABLED=true
COMPOSE_PROFILES=webclip
```

```bash
docker compose up -d     # fetcher（取得の担当）が起動する
```

## 使われ方

利用者向けの説明は [利用者ガイドの Web ページを保存する](../user/webclip.md)。

- フォルダの画面の「URL を追加」で追加します。取得中は一覧に「取得中」と出て、終わると普通の文書と同じ行になります（種類は Web クリップ、サムネイルはページの OGP 画像）
- 設定画面の**ブックマークレット**をブックマークバーに登録すると、見ているページをそのまま保存できます（保存先を選ぶ画面が開きます）
- 文書の画面の「再取得」で、元のページの最新の内容に保存し直せます
- 保存したページの HTML は、安全のため画面には表示しません（ダウンロードはできます）。取り出した本文は「取り出したテキスト」で読めます
- JavaScript で本文を描くページ（SNS など）は、通常の取得では本文が空に近いことがあります。その場合は、任意の**描画サービス**（ヘッドレス Chromium）で JavaScript を動かしてから保存します（[JavaScript で描くページ](#javascript-で描くページ任意)）。描画もできなかったときは、タイトルと説明で検索でき、サイトが oEmbed に対応していれば、その文章も補います

## 安全のための制限

 LAN の機器（ルーター・NAS など）や Pi 自身へのリクエストを作らせないよう、取得の前に名前を引いて、プライベートな IP（192.168.x.x・10.x.x.x・Tailscale の 100.64.0.0/10 など）を指す URL は取得しません。リダイレクトのたびに確かめ直し、確かめた IP にだけ接続します。ポートは 80・443・8080・8443 だけ、HTML は 5MB（`DOCSEED_WEBCLIP_MAX_MB`）・30 秒までです。取得するのは fetcher だけで、文字の読み取りを行う worker は今まで通り外部と通信しません。

`DOCSEED_WEBCLIP_PROXY` でプロキシを使うこともできます。そのときは名前の解決をプロキシに任せることになるので、**プロキシの側で LAN への通信を止めてください**。

## JavaScript で描くページ（任意）

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
- 描画サービスと fetcher は、caddy・api とは別の Docker のネットワーク（`webclip`）に入ります。描画サービスが外のページに乗っ取られても、api には直接届きません
- **注意**: 描画サービスの通信は Docker のネットワーク越しに外へ出ます。WebSocket の通信は確認の対象にならないため、**LAN への出口は Pi のルーターや Docker 側でも止めてください**

## 関連

- 設定: [設定の一覧](configuration.md#web-クリップ)
- 設計: [拡張機能](../design/extensions.md) 6 章
