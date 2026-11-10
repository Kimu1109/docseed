#!/usr/bin/env bash
# イメージの取り直しの systemd タイマーを配置する（初回だけ）。sudo で実行する。
#   sudo scripts/ops/install-update.sh
# 毎週月曜 4:30 ごろに scripts/ops/update-images.sh を動かす（docs/design/deployment.md 7.1）。
set -euo pipefail
. "$(dirname "$0")/common.sh"

[ "$(id -u)" = 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
install -d -m 0700 /etc/docseed
if [ ! -f /etc/docseed/update.env ]; then
  cat >/etc/docseed/update.env <<'CONF'
# scripts/ops/update-images.sh の設定
# true にすると docseed のイメージも毎週作り直す（ベースの python イメージと Python の依存が最新になる）。
# 依存の大きな更新で動かなくなることがあるので、既定は false（手で docker compose build --pull する）
DOCSEED_UPDATE_REBUILD=false
CONF
  chmod 0600 /etc/docseed/update.env
  echo "作成しました: /etc/docseed/update.env"
fi
for u in docseed-update.service docseed-update.timer; do
  sed "s#@DOCSEED_DIR@#$DOCSEED_DIR#g" "$DOCSEED_DIR/deploy/systemd/$u" >"/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now docseed-update.timer
systemctl list-timers 'docseed-*' --no-pager

cat <<MSG

確認:
  sudo systemctl start docseed-update.service && journalctl -u docseed-update -n 30   # 試しに実行する
MSG
