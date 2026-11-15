# 異常の通知と更新

ログインの異常や管理者の操作をスマホなどに知らせ、依存関係とイメージを定期的に更新・検査します。ファイルの受け取りなどの通知は [通知](notifications.md)（宛先の設定は別です）。

| 項目 | 内容 |
|---|---|
| 既定 | オフ（`DOCSEED_NOTIFY_URL` が空） |
| 外部との通信 | **あり**（api から宛先へ。更新のタイマーはイメージの取得） |
| 追加のメモリ | なし |
| 追加のコンテナ | なし |

## 通知する

`.env` の `DOCSEED_NOTIFY_URL` に送り先を書くと、次のことが起きたときに知らせます（[設計](../design/security.md) 11 章）。

- 外側（LAN・Tailscale 以外）の新しい接続元からのログイン
- ログインの失敗の急増（15 分に 20 回以上。`DOCSEED_NOTIFY_LOGIN_FAILURES` で変更）
- 管理者の操作（ユーザーの作成・変更、ログインの設定の変更、ログイン方法のリセット）、確認コードの無効化、パスキーの複製の疑い
- 共有リンクのパスワードの失敗が続いたとき
- [状態の確認](monitoring.md) の「確認が必要な項目」が新しく出たとき（worker の停止・SSD の空き・バックアップの失敗・証明書の期限など）
- 週ごとのイメージの更新の結果（下の「イメージを毎週取り直す」）

| 送り先 | `DOCSEED_NOTIFY_URL` の例 |
|---|---|
| [ntfy](https://ntfy.sh/)（スマホのアプリで受け取れる） | `https://ntfy.sh/<推測されにくい名前>`（自分の ntfy サーバーでも可） |
| Discord | Webhook の URL（`https://discord.com/api/webhooks/...`） |
| Slack | Incoming Webhook の URL（`https://hooks.slack.com/services/...`） |
| そのほか | 任意の URL に JSON（`title`・`message`・`level`・`event`）を POST |

```bash
docker compose up -d                                           # .env を変えたら api を作り直す
docker compose exec api docseed notify --test                  # テストの通知を送る
```

- 通知には、ユーザー名と接続元の IP アドレスが入ります。ntfy.sh の公開サーバーを使う場合、トピック名を知っている人は誰でも読めるので、長く推測されにくい名前にしてください（気になる場合は自分の ntfy サーバーを使う）
- URL はトークンを含むので秘密にしてください（`.env` は Git に入れない）
- 通知は api が送ります（worker は外部と通信しないため）

## 依存関係とイメージの更新

- GitHub の Dependabot が、毎週 Python の依存・GitHub Actions・Dockerfile のベースのイメージの更新を PR で提案します（`.github/dependabot.yml`）
- 毎週月曜に、Python の依存（pip-audit）と docseed のイメージ（Trivy）の脆弱性を検査します（`.github/workflows/security.yml`）。修正版のある重大な脆弱性があると失敗し、GitHub から通知が届きます。caddy・cloudflared・llama.cpp のイメージの検査結果は、ワークフローの概要に表示します（上流の更新を待つしかないため、失敗にはしません）

## イメージを毎週取り直す

caddy・cloudflared・llama.cpp のイメージ（`caddy:2` などの浮動タグ）を週 1 回取り直し、新しくなったコンテナだけを入れ替えます。結果は上の通知で知らせます。

```bash
sudo scripts/ops/install-update.sh            # 毎週月曜 4:30 ごろに動くタイマーを入れる（初回だけ）
sudo systemctl start docseed-update.service   # 試しに今すぐ実行する
journalctl -u docseed-update -n 30
```

docseed 自身のイメージは、既定では自動で作り直しません（Python の依存の大きな更新で動かなくなることがあるため）。月に 1 度くらい、`git pull` のあと `docker compose build --pull && docker compose up -d` を手で行ってください。自動にしたい場合は `/etc/docseed/update.env` の `DOCSEED_UPDATE_REBUILD=true`。

## 関連

- [通知](notifications.md)
- [状態の確認](monitoring.md)
- [設計: 認証・権限・共有](../design/security.md) 11 章
