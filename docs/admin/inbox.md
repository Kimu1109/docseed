# 受け取り箱（LAN からファイルを放り込む）

`/srv/docseed/inbox/<ユーザー名>/` に置いたファイルが、そのユーザーの「Inbox」フォルダに自動で取り込まれます（30 秒ごと。サブフォルダの構造も保ちます）。スキャナーの保存先や PC からのドラッグ＆ドロップに使えます。外部とは通信しません。

## 有効にする

1. `.env` で有効にして worker を再起動する
   ```bash
   # .env
   DOCSEED_INBOX_ENABLED=true
   ```
   ```bash
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

利用者には、Inbox に入った文書に置き場所の候補が出ることを伝えてください（[利用者ガイド](../user/organize.md#置き場所とタグの候補)）。

## 関連

- 設定: `DOCSEED_INBOX_ENABLED`・`DOCSEED_INBOX_KEEP`（[設定の一覧](configuration.md#受け取り箱)）
- 取り込みの通知: [通知](notifications.md)
