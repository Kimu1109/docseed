#!/usr/bin/env bash
# certbot が証明書を更新したときに、docseed の Caddy を再起動して新しい証明書を読み込ませる（初回だけ）。
# DOCSEED_TLS_MODE=file で certbot の証明書を使う場合に、sudo で実行する。
#   sudo scripts/ops/install-cert-hook.sh
# Caddy は指定した証明書ファイルの変更を監視しないため、更新のたびに読み込み直す必要がある。
set -euo pipefail
. "$(dirname "$0")/common.sh"

[ "$(id -u)" = 0 ] || { echo "sudo で実行してください" >&2; exit 1; }
HOOK_DIR=/etc/letsencrypt/renewal-hooks/deploy
[ -d "$HOOK_DIR" ] || { echo "$HOOK_DIR がありません（certbot は入っていますか）" >&2; exit 1; }

HOOK="$HOOK_DIR/docseed-caddy.sh"
cat >"$HOOK" <<HOOK_SCRIPT
#!/bin/sh
# docseed: 証明書の更新後に Caddy を再起動する（scripts/ops/install-cert-hook.sh が作成）
cd "$DOCSEED_DIR" && docker compose restart caddy
HOOK_SCRIPT
chmod 0755 "$HOOK"
echo "作成しました: $HOOK"

cat <<MSG

確認:
  sudo certbot renew --dry-run       # 更新の試行（実際には更新しない）。成功すれば次回から自動で Caddy に反映される
  sudo $HOOK                         # フックを手で動かして、Caddy が再起動することを確かめる
MSG
