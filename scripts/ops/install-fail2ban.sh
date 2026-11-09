#!/usr/bin/env bash
# fail2ban で、docseed へのログインの失敗などが続いた接続元をファイアウォールで遮断する（初回だけ）。
# docs/design/security.md 9 章。インターネットに直接公開する場合に、sudo で実行する。
#   sudo scripts/ops/install-fail2ban.sh
# 設定を変えたら、もう一度実行すれば上書きする。
set -euo pipefail
. "$(dirname "$0")/common.sh"

[ "$(id -u)" = 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
SRC="$DOCSEED_DIR/deploy/fail2ban"
LOGPATH="$DOCSEED_DATA_DIR/logs/security.log"

if ! command -v fail2ban-client >/dev/null; then
  log "fail2ban を入れます"
  apt-get update -q && apt-get install -y -q fail2ban
fi

# api が書くログ（コンテナの中は UID 1000）。まだ無ければ作っておく（fail2ban は無いファイルを見張れない）
install -d -o 1000 -g 1000 "$DOCSEED_DATA_DIR/logs"
[ -e "$LOGPATH" ] || install -o 1000 -g 1000 -m 0640 /dev/null "$LOGPATH"

install -m 0644 "$SRC/filter.d/docseed.conf" /etc/fail2ban/filter.d/docseed.conf
sed "s#@LOGPATH@#$LOGPATH#" "$SRC/jail.d/docseed.local" > /etc/fail2ban/jail.d/docseed.local
chmod 0644 /etc/fail2ban/jail.d/docseed.local
log "設定を置きました: /etc/fail2ban/filter.d/docseed.conf, /etc/fail2ban/jail.d/docseed.local（ログ: $LOGPATH）"

if ! iptables -n -L DOCKER-USER >/dev/null 2>&1; then
  echo "注意: DOCKER-USER チェーンがありません。Docker を起動してから、もう一度実行してください" >&2
fi

systemctl enable --now fail2ban >/dev/null
fail2ban-client reload
sleep 1
fail2ban-client status docseed

cat <<MSG

確認:
  sudo fail2ban-client status docseed               # 遮断中の IP（Banned IP list）
  sudo fail2ban-regex $LOGPATH /etc/fail2ban/filter.d/docseed.conf   # ログが読めているか
  sudo fail2ban-client set docseed unbanip <IP>     # 誤って遮断したときに解除する
MSG
