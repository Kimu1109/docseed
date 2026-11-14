# バックアップと復旧

毎日 3:00 に、DB のスナップショットを作ってから、原本（`files/`）・DB のスナップショット・`.env` を restic で別の機器（外付け USB ドライブや NAS）へ送ります。Pi 本体の systemd タイマーで動きます。SSD は 1 台なので、**必ず別の機器に**取ってください。

1. 保存先を用意する（例: 外付け USB ドライブを ext4 でフォーマットし、`/etc/fstab` で `/mnt/backup` にマウント）
2. 設定する
   ```bash
   sudo apt install -y restic
   sudo scripts/ops/install-backup.sh        # タイマーの登録、/etc/docseed/backup.env とパスワードの生成
   sudo nano /etc/docseed/backup.env         # RESTIC_REPOSITORY を保存先に合わせる（NAS なら sftp: / rest:）
   sudo bash -c 'set -a; . /etc/docseed/backup.env; restic init'
   sudo cat /etc/docseed/restic-password     # 紙やパスワード管理ツールに控える（失うと復元できません）
   ```
3. 試しに実行して、復旧できることを確かめる
   ```bash
   sudo systemctl start docseed-backup.service && journalctl -u docseed-backup -n 30
   sudo scripts/ops/restore-drill.sh         # 最新のバックアップを一時ディレクトリに復元し、原本と DB の対応を確認
   ```
   「成功: … から復旧できることを確認しました」と出れば完了です。管理画面の「バックアップ」に最終結果が表示されます。

- 毎週日曜 4:30 に `restic check` でバックアップの一部を読み出して確かめます
- 毎月第 1 日曜 5:30 に **復旧の練習** を自動で行い、最新のバックアップから本当に戻せるかを確かめます（一時ディレクトリに復元して、DB と原本の対応を確認し、消す）。復元する大きさ分の空きが `/var/tmp`（`DOCSEED_DRILL_DIR` で変更）に無い月は飛ばして警告を出します。結果は管理画面の「バックアップ」に出ます
- 残す世代は日次 7・週次 4・月次 6（`/etc/docseed/backup.env` で変更）
- バックアップが失敗したり 36 時間以上成功していなかったりすると、管理画面に警告が出ます

## Pi を作り直して復旧する

1. 新しい SSD に OS・Docker・restic を入れ、このリポジトリを clone する（最初の手順と同じ場所に）
2. 保存先をつなぎ、控えておいたパスワードを `/etc/docseed/restic-password` に置いて復元する
   ```bash
   export RESTIC_REPOSITORY=/mnt/backup/docseed-restic RESTIC_PASSWORD_FILE=/etc/docseed/restic-password
   sudo -E restic restore latest --tag docseed --target /   # /srv/docseed と .env が元の場所に戻る
   ```
3. DB を戻し、モデルを取得して起動する
   ```bash
   docker compose run --rm --no-deps api docseed restore-db
   docker compose --profile tools run --rm --build tools
   docker compose up -d --build
   docker compose exec api docseed verify-files               # 「問題はありません」なら完了
   sudo scripts/ops/install-backup.sh                         # バックアップのタイマーを入れ直す
   ```

最後のバックアップより後にアップロードしたファイルは戻りません。詳しくは [配置・運用](../design/deployment.md) の 5 章。

## 関連

- 設定: `/etc/docseed/backup.env`（`scripts/ops/backup.env.example`）と [設定の一覧](configuration.md#バックアップのスクリプト)
- コマンド: [コマンドの一覧](cli.md)（`backup-db`・`restore-db`・`verify-files`）
