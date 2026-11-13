#!/usr/bin/env bash
# 復旧の練習。docs/design/deployment.md 5.2・5.3。
# restic のスナップショットを一時ディレクトリへ復元し、DB を戻して、原本ファイルとの対応を確かめる。
# 動いている docseed には触れない（データディレクトリとは別の場所に復元する）。
#
#   sudo scripts/ops/restore-drill.sh [スナップショット ID（既定 latest）] [--keep] [--auto]
#
# 復元先は DOCSEED_DRILL_DIR（既定 /var/tmp）の下に作る。復元するデータと同じだけの空き容量が要る。
# --keep を付けると、終わった後も復元したファイルを残す（中身を目で確かめる場合）。
# --auto は定期実行（docseed-restore-drill.timer）用: 空き容量が足りなければ練習を飛ばし、その記録だけを残す。
#
# 結果は <データディレクトリ>/backup/restore-drill-status.json に書き、管理画面・警告・通知・指標に出る。
set -euo pipefail
. "$(dirname "$0")/common.sh"

SNAP=latest
KEEP=0
AUTO=0
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    --auto) AUTO=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) SNAP=$a ;;
  esac
done

STATUS="$DOCSEED_DATA_DIR/backup/restore-drill-status.json"
DRILL_DIR=${DOCSEED_DRILL_DIR:-/var/tmp}
MARGIN=${DOCSEED_DRILL_MARGIN:-1.2}      # 復元する大きさの何倍の空きがあれば練習するか
STARTED=$(date +%s)
SNAP_ID=""
SNAP_TIME=""
RESTORE_SIZE=0

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r'; }

write_status() {  # result（ok / failed / skipped） message
  mkdir -p "$(dirname "$STATUS")"
  local tmp="$STATUS.tmp" ok=false now last
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # 最後に成功した日時は、飛ばした・失敗したときも前の記録から引き継ぐ
  last=$(grep -o '"last_success": "[^"]*"' "$STATUS" 2>/dev/null | cut -d'"' -f4 || true)
  if [ "$1" = ok ]; then ok=true; last=$now; fi
  printf '{"time": "%s", "ok": %s, "result": "%s", "last_success": "%s", "snapshot": "%s", "snapshot_time": "%s", "restore_size": %s, "duration_s": %s, "auto": %s, "message": "%s", "host": "%s"}\n' \
    "$now" "$ok" "$1" "$last" "$(json_str "$SNAP_ID")" "$(json_str "$SNAP_TIME")" \
    "${RESTORE_SIZE:-0}" "$(( $(date +%s) - STARTED ))" "$([ "$AUTO" = 1 ] && echo true || echo false)" \
    "$(json_str "$2")" "$(json_str "$(hostname)")" >"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$STATUS"
}

STEP="準備"
WORK=""
on_error() {
  local code=$?
  log "失敗: $STEP（終了コード $code）"
  write_status failed "$STEP で失敗しました（終了コード $code）"
  exit "$code"
}
trap on_error ERR
cleanup() {
  [ -n "$WORK" ] || return 0
  if [ "$KEEP" = 1 ]; then log "復元したファイルを残しました: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

STEP="restic の設定の確認"
if ! (require_restic); then false; fi

STEP="スナップショットの確認"
info=$(restic snapshots "$SNAP" --tag docseed --json)
SNAP_ID=$(printf '%s' "$info" | grep -o '"short_id":"[0-9a-f]*"' | tail -n 1 | cut -d'"' -f4 || true)
SNAP_TIME=$(printf '%s' "$info" | grep -o '"time":"[^"]*"' | tail -n 1 | cut -d'"' -f4 || true)
if [ -z "$SNAP_ID" ]; then
  echo "スナップショット $SNAP がありません（先に backup.sh を実行してください）" >&2
  false
fi
RESTORE_SIZE=$(restic stats "$SNAP_ID" --mode restore-size --json | grep -o '"total_size":[0-9]*' | cut -d: -f2)
RESTORE_SIZE=${RESTORE_SIZE:-0}
log "スナップショット $SNAP_ID（$SNAP_TIME）: 復元する大きさ $RESTORE_SIZE バイト"

STEP="空き容量の確認"
mkdir -p "$DRILL_DIR"
avail=$(df -P -B1 "$DRILL_DIR" | awk 'NR == 2 { print $4 }')
need=$(awk -v s="$RESTORE_SIZE" -v m="$MARGIN" 'BEGIN { printf "%d", s * m }')
if [ "$avail" -lt "$need" ]; then
  msg="$DRILL_DIR の空きが $avail バイトで、復元に要る $need バイトに足りません"
  if [ "$AUTO" = 1 ]; then
    log "飛ばしました: $msg"
    write_status skipped "$msg。DOCSEED_DRILL_DIR を空きのある場所にしてください"
    exit 0
  fi
  echo "$msg" >&2
  false
fi

WORK=$(mktemp -d "$DRILL_DIR/docseed-drill.XXXXXX")
chmod 0755 "$WORK"   # コンテナ（UID 1000）から中を読めるように

STEP="restic restore"
log "1. restic restore $SNAP_ID → $WORK"
restic restore "$SNAP_ID" --tag docseed --target "$WORK" --quiet
RESTORED="$WORK$DOCSEED_DATA_DIR"
if [ ! -d "$RESTORED/files" ]; then
  echo "復元結果に $DOCSEED_DATA_DIR/files がありません" >&2
  false
fi
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

STEP="DB をスナップショットから戻す"
log "2. $STEP"
run restore-db latest --force

STEP="原本ファイルとの対応の確認"
log "3. $STEP（大きさと SHA-256）"
if out=$(run verify-files 2>&1); then
  printf '%s\n' "$out"
  log "成功: スナップショット $SNAP_ID から復旧できることを確認しました"
  write_status ok "スナップショット $SNAP_ID から復旧できることを確認しました"
else
  printf '%s\n' "$out"
  log "失敗: 復元した原本と DB が一致しません（上の一覧を確認してください）"
  trap - ERR
  write_status failed "復元した原本と DB が一致しません: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
  exit 1
fi
