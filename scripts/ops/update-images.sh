#!/usr/bin/env bash
# サードパーティのイメージ（caddy・cloudflared・llama.cpp）を取り直し、新しくなったコンテナを入れ替える。
# docseed-update.timer が週 1 回動かす（scripts/ops/install-update.sh で配置）。手でも実行できる:
#   sudo scripts/ops/update-images.sh
# /etc/docseed/update.env で DOCSEED_UPDATE_REBUILD=true にすると、docseed のイメージも作り直す
# （ベースの python イメージと Python の依存が最新になる。git pull はしない）。
# 結果は、DOCSEED_NOTIFY_URL を設定していれば通知する（docs/design/security.md 11 章）。
set -euo pipefail
. "$(dirname "$0")/common.sh"
CONF=${DOCSEED_UPDATE_CONF:-/etc/docseed/update.env}
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
cd "$DOCSEED_DIR"

notify() {  # $1 = level, $2 = 本文。通知を設定していなければ何もしない
  docker compose exec -T api docseed notify --test --title "イメージの更新" --level "$1" --message "$2" \
    >/dev/null 2>&1 || true
}
trap 'notify error "イメージの更新に失敗しました。journalctl -u docseed-update を確認してください"' ERR

before=$(docker compose images -q 2>/dev/null | sort -u)

# docseed:latest は手元で作るイメージなので取りに行かない（取りに行くと Docker Hub で見つからず失敗する）
for img in $(docker compose config --images | sort -u | grep -v '^docseed'); do
  log "取り直します: $img"
  docker pull -q "$img" >/dev/null
done
if [ "${DOCSEED_UPDATE_REBUILD:-false}" = true ]; then
  log "docseed のイメージを作り直します（ベースのイメージと Python の依存を最新に）"
  docker compose build --pull --quiet
fi

docker compose up -d
after=$(docker compose images -q | sort -u)

# api が動き出すのを待つ（Dockerfile の HEALTHCHECK）
state=""
for _ in $(seq 1 24); do
  state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$(docker compose ps -q api)")
  [ "$state" = healthy ] || [ "$state" = running ] && break
  sleep 5
done
if [ "$state" != healthy ] && [ "$state" != running ]; then
  log "api が起動しません（$state）"
  notify error "更新のあと api が起動しません（$state）。docker compose logs api を確認してください"
  trap - ERR
  exit 1
fi

docker image prune -f >/dev/null
if [ "$before" != "$after" ]; then
  log "新しいイメージに入れ替えました"
  notify info "新しいイメージに入れ替えました: $(docker compose config --images | sort -u | tr '\n' ' ')"
else
  log "新しいイメージはありませんでした"
fi
