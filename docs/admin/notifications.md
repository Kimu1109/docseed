# 通知

`.env` に宛先を書くと、次のことを知らせます。

- **監視の警告**（[状態の確認](monitoring.md) の「確認が必要な項目」の注意・異常）: 出たとき（一時的なもので鳴らないよう、5 分おきの確認で 2 回続けて出たとき）、重くなったとき、続いていれば 1 日ごと、解消したとき
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

## 関連

- 設定: [設定の一覧](configuration.md#通知)
- 設計: [配置・運用](../design/deployment.md) 6.1
