#!/usr/bin/env bash
# 復旧の練習。docs/design/deployment.md 5.2。
# restic のスナップショットを一時ディレクトリへ復元し、DB を戻して、原本ファイルとの対応を確かめる。
# 動いている docseed には触れない（データディレクトリとは別の場所に復元する）。
#
#   sudo scripts/ops/restore-drill.sh [スナップショット ID（既定 latest）] [--keep]
#
# 復元先は DOCSEED_DRILL_DIR（既定 /var/tmp）の下に作る。原本と同じだけの空き容量が必要。
# --keep を付けると、終わった後も復元したファイルを残す（中身を目で確かめる場合）。
set -euo pipefail
. "$(dirname "$0")/common.sh"

SNAP=latest
KEEP=0
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) SNAP=$a ;;
  esac
done
require_restic

WORK=$(mktemp -d "${DOCSEED_DRILL_DIR:-/var/tmp}/docseed-drill.XXXXXX")
chmod 0755 "$WORK"   # コンテナ（UID 1000）から中を読めるように
cleanup() { if [ "$KEEP" = 1 ]; then log "復元したファイルを残しました: $WORK"; else rm -rf "$WORK"; fi; }
trap cleanup EXIT

log "1. restic restore $SNAP → $WORK"
restic restore "$SNAP" --tag docseed --target "$WORK" --quiet
RESTORED="$WORK$DOCSEED_DATA_DIR"
[ -d "$RESTORED/files" ] || { echo "復元結果に $DOCSEED_DATA_DIR/files がありません" >&2; exit 1; }
if [ -f "$WORK$DOCSEED_DIR/.env" ]; then log "   .env も含まれています"; else log "   注意: .env がバックアップに含まれていません"; fi

# 復元した場所を対象に docseed のコマンドを実行する
if [ "${DOCSEED_LOCAL:-0}" = 1 ]; then
  run() { DOCSEED_DATA_DIR="$RESTORED" docseed "$@"; }
else
  run() {
    docker compose --project-directory "$DOCSEED_DIR" run --rm --no-deps -T \
      -e DOCSEED_DATA_DIR=/restore -v "$RESTORED:/restore" api docseed "$@"
  }
fi

log "2. DB をスナップショットから戻す"
run restore-db latest --force

log "3. 原本ファイルとの対応を確認（大きさと SHA-256）"
if run verify-files; then
  log "成功: スナップショット $SNAP から復旧できることを確認しました"
else
  log "失敗: 復元した原本と DB が一致しません（上の一覧を確認してください）"
  exit 1
fi
