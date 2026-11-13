#!/usr/bin/env bash
# バックアップの systemd タイマーを配置する（初回だけ）。sudo で実行する。
#   sudo scripts/ops/install-backup.sh
# 1. /etc/docseed/backup.env（無ければ例から作る）と restic のパスワードのファイルを用意
# 2. deploy/systemd/ のユニットを /etc/systemd/system/ に置き、タイマーを有効にする
set -euo pipefail
. "$(dirname "$0")/common.sh"

[ "$(id -u)" = 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
command -v restic >/dev/null || { echo "先に restic を入れてください: sudo apt install -y restic" >&2; exit 1; }

install -d -m 0700 /etc/docseed
if [ ! -f "$DOCSEED_BACKUP_CONF" ]; then
  install -m 0600 "$DOCSEED_DIR/scripts/ops/backup.env.example" "$DOCSEED_BACKUP_CONF"
  echo "作成しました: $DOCSEED_BACKUP_CONF（RESTIC_REPOSITORY を保存先に合わせて編集してください）"
fi
if [ ! -f /etc/docseed/restic-password ]; then
  ( umask 077; head -c 32 /dev/urandom | base64 >/etc/docseed/restic-password )
  echo "restic のパスワードを生成しました: /etc/docseed/restic-password"
  echo "  ※ 失うと復元できません。中身を別の場所（紙・パスワード管理ツール）に控えてください"
fi

for u in docseed-backup.service docseed-backup.timer docseed-backup-check.service docseed-backup-check.timer \
         docseed-restore-drill.service docseed-restore-drill.timer; do
  sed "s#@DOCSEED_DIR@#$DOCSEED_DIR#g" "$DOCSEED_DIR/deploy/systemd/$u" >"/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now docseed-backup.timer docseed-backup-check.timer docseed-restore-drill.timer
systemctl list-timers 'docseed-*' --no-pager

cat <<MSG

次の手順:
  1. $DOCSEED_BACKUP_CONF の RESTIC_REPOSITORY を確認する
  2. リポジトリを作る:   sudo bash -c 'set -a; . $DOCSEED_BACKUP_CONF; restic init'
  3. 試しに実行する:     sudo systemctl start docseed-backup.service && journalctl -u docseed-backup -n 30
  4. 復旧を試す:         sudo $DOCSEED_DIR/scripts/ops/restore-drill.sh
     （以後は毎月第 1 日曜に自動で練習し、結果を管理画面に出す。空きが足りない月は飛ばして警告する）
MSG
