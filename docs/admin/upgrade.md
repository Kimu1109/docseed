# 更新

## いつもの手順

1. バックアップを取る

   ```bash
   sudo systemctl start docseed-backup.service && journalctl -u docseed-backup -n 30
   ```

2. 新しい版を取ってきて、作り直して起動する

   ```bash
   git pull
   ```

   ```bash
   docker compose up -d --build
   ```

   api が起動時にデータベースのマイグレーションを適用します。worker はそれが終わるのを待ってから処理を始めます。

3. 下の「版ごとの追加の作業」に当てはまるものがあれば行う
4. 管理画面の「確認が必要な項目」に警告が出ていないかを見る（[状態の確認](monitoring.md)）

`.env.example` に新しい設定が増えていることがあります。`git diff HEAD@{1} -- .env.example` で増えた分を見て、必要なら `.env` に写してください（書かなければ既定の値で動きます）。

## 版ごとの追加の作業

新しいものが上です。

| 更新前の状態 | 追加の作業 |
|---|---|
| 0.9.0 まで（Docker のネットワークと「内側」の既定を変える前） | `docker compose up -d` でコンテナを作り直す（caddy・api は新しい `front`、fetcher・webclip-browser は `webclip` のネットワークに移る）。`front` のサブネット（`10.213.47.16/28`）が手元のネットワークと重なって起動しないときは、`.env` の `DOCSEED_FRONT_SUBNET` を空いている範囲にする。`DOCSEED_TRUSTED_NETS` を書いていない場合は「内側」の既定が `192.168.0.0/16 10.0.0.0/8 127.0.0.1/8 fd00::/8 ::1 100.64.0.0/10` に変わり、`172.16.0.0/12` が外側になる。LAN が `172.16`〜`172.31` で始まるアドレスなら、その LAN の範囲（例 `172.20.10.0/24`）を `.env` の `DOCSEED_TRUSTED_NETS` に既定の値と一緒に書く。`docseed serve` を compose を使わずに動かしているなら、`--forwarded-allow-ips` に手前のプロキシの IP を渡す（既定が `*` から `127.0.0.1` に変わった） |
| Phase 7 まで（写真・サムネイルの前） | `docker compose exec api docseed reprocess unsupported`（以前に保存した HEIC / AVIF の文字を読む）と `docker compose exec api docseed derive missing`（保存済みの画像・PDF のサムネイルとプレビューを作る。文字の読み取りはやり直さない） |
| Phase 4 まで（Office 文書・バックアップの前） | `docker compose exec api docseed reprocess unsupported`（以前に保存した Word / Excel / PowerPoint を処理する）。続けて [バックアップと復旧](backup.md) を設定する |
| Phase 1 まで（文字の読み取りの前） | `docker compose --profile tools run --rm --build tools`（モデルの取得）のあと `docker compose up -d --build`。保存済みのファイルは worker が起動すると順に処理します |

- PDF に貼られた画像の読み取りが入る前に取り込んだ PDF は、管理画面の「古い版で処理したファイルを再処理」で読み直せます（PDF だけが対象）
- 音声・動画の文字起こしを後から有効にしたときは「形式が未対応だったファイルを再処理」（[文字起こし](asr.md)）

## うまく行かないとき

- 起動しない: [困ったとき](troubleshooting.md)
- 前の版に戻す: `git checkout <前のコミット>` → `docker compose up -d --build`。マイグレーションは戻らないので、データベースを更新前のスナップショットに戻す必要があることがあります（`docseed restore-db`。[バックアップと復旧](backup.md)）

## 開発者へ

更新で利用者や管理者がすることが増えたら、この表に行を足してください（[文書の書き方と更新の手順](../dev/docs-guide.md)）。
