# backup.sh / restore-drill.sh / install-backup.sh の共通部分（source して使う）

# リポジトリ（compose.yaml と .env のある場所）
DOCSEED_DIR=${DOCSEED_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}

# 設定ファイル（restic の保存先とパスワードのファイル）
DOCSEED_BACKUP_CONF=${DOCSEED_BACKUP_CONF:-/etc/docseed/backup.env}
if [ -f "$DOCSEED_BACKUP_CONF" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$DOCSEED_BACKUP_CONF"
  set +a
fi

# ホスト側のデータディレクトリ（.env の DOCSEED_DATA_DIR。無ければ /srv/docseed）
if [ -z "${DOCSEED_DATA_DIR:-}" ] && [ -f "$DOCSEED_DIR/.env" ]; then
  DOCSEED_DATA_DIR=$(sed -n 's/^DOCSEED_DATA_DIR=//p' "$DOCSEED_DIR/.env" | tail -n 1)
fi
DOCSEED_DATA_DIR=${DOCSEED_DATA_DIR:-/srv/docseed}

# docseed のコマンドを動いている api コンテナで実行する（試験では DOCSEED_EXEC=docseed で置き換える）
DOCSEED_EXEC=${DOCSEED_EXEC:-"docker compose --project-directory $DOCSEED_DIR exec -T api docseed"}

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

require_restic() {
  command -v restic >/dev/null || { echo "restic がありません: sudo apt install -y restic" >&2; exit 2; }
  if [ -z "${RESTIC_REPOSITORY:-}" ] || { [ -z "${RESTIC_PASSWORD_FILE:-}" ] && [ -z "${RESTIC_PASSWORD:-}" ]; }; then
    echo "RESTIC_REPOSITORY と RESTIC_PASSWORD_FILE を $DOCSEED_BACKUP_CONF に設定してください" >&2
    exit 2
  fi
}
