#!/usr/bin/env bash
# docseed のバックアップ。docs/design/deployment.md 5 章。
# ホストの systemd タイマー（docseed-backup.timer）から毎日実行する。手動でも実行できる:
#   sudo scripts/ops/backup.sh          バックアップ（DB スナップショット → restic → 古い世代の削除）
#   sudo scripts/ops/backup.sh check    restic のリポジトリの検査（データの一部を読んで確かめる。週 1 回）
#
# 設定は /etc/docseed/backup.env（scripts/ops/backup.env.example を参照）。
# 結果は <データディレクトリ>/backup/restic-status.json に書き、管理画面に表示される。
set -euo pipefail
. "$(dirname "$0")/common.sh"

STATUS="$DOCSEED_DATA_DIR/backup/restic-status.json"
KEEP_DAILY=${DOCSEED_KEEP_DAILY:-7}
KEEP_WEEKLY=${DOCSEED_KEEP_WEEKLY:-4}
KEEP_MONTHLY=${DOCSEED_KEEP_MONTHLY:-6}
CHECK_SUBSET=${DOCSEED_CHECK_SUBSET:-5%}

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r'; }

write_status() {  # ok snapshot message
  mkdir -p "$(dirname "$STATUS")"
  local tmp="$STATUS.tmp"
  printf '{"time": "%s", "ok": %s, "snapshot": "%s", "message": "%s", "host": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$(json_str "$2")" "$(json_str "$3")" "$(json_str "$(hostname)")" >"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$STATUS"
}

STEP="準備"
on_error() {
  local code=$?
  log "失敗: $STEP（終了コード $code）"
  [ "${MODE:-backup}" = backup ] && write_status false "" "$STEP で失敗しました（終了コード $code）"
  exit "$code"
}
trap on_error ERR

MODE=${1:-backup}
require_restic

case "$MODE" in
  backup)
    STEP="データディレクトリの確認"
    if [ ! -d "$DOCSEED_DATA_DIR/files" ]; then echo "$DOCSEED_DATA_DIR/files がありません" >&2; false; fi

    STEP="DB のスナップショット"
    log "$STEP"
    $DOCSEED_EXEC backup-db

    STEP="restic backup"
    log "$STEP → $RESTIC_REPOSITORY"
    paths=("$DOCSEED_DATA_DIR/files" "$DOCSEED_DATA_DIR/backup")
    if [ -d "$DOCSEED_DATA_DIR/caddy" ]; then
      paths+=("$DOCSEED_DATA_DIR/caddy")   # Caddy の内部 CA の鍵（失うと各端末に CA を入れ直すことになる）
    fi
    for f in .env compose.yaml; do
      if [ -f "$DOCSEED_DIR/$f" ]; then paths+=("$DOCSEED_DIR/$f"); fi
    done
    out=$(restic backup --tag docseed --json --exclude "$DOCSEED_DATA_DIR/files/.tmp" --exclude "$STATUS" \
          --exclude "$STATUS.tmp" "${paths[@]}" | tail -n 1)
    snap=$(printf '%s' "$out" | grep -o '"snapshot_id":"[0-9a-f]*"' | cut -d'"' -f4 | cut -c1-8 || true)
    log "スナップショット ${snap:-?}"

    STEP="古い世代の削除（restic forget --prune）"
    log "$STEP"
    restic forget --tag docseed --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" \
      --keep-monthly "$KEEP_MONTHLY" --prune --quiet

    write_status true "${snap:-}" "完了"
    log "完了"
    ;;
  check)
    STEP="restic check"
    log "$STEP（データの $CHECK_SUBSET を読んで確認）"
    restic check --read-data-subset="$CHECK_SUBSET"
    log "完了"
    ;;
  *)
    echo "使い方: $0 [backup|check]" >&2
    exit 2
    ;;
esac
