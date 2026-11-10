#!/usr/bin/env bash
# 要約に使う LLM を、自分の文書で比べる（docs/design/extensions.md 7.4）。Pi の上で、リポジトリの中から実行する。
#
#   scripts/bench_llm.sh                               # models/llm/ にある GGUF を全部比べる
#   scripts/bench_llm.sh sarashina2.2-1b qwen3-1.7b    # 名前で選ぶ（未取得なら取得する）
#
# 1 回で次を行う:
#   1. 比べる文書を docseed から書き出す（初回だけ。<データ>/bench/docs に新しい文書 30 件）
#   2. 動いている worker と llm を止める（CPU とメモリを取り合わないように。終わったら起動し直す）
#   3. モデルごとに llama-server を本番と同じ設定（スレッド・コンテキスト・メモリ 2GB）で起動し、
#      全文書を要約して時間を測る。その間のメモリを docker stats で記録する
#   4. 比較表 <データ>/bench/llm-<日時>/report.md を作る（速さ・メモリ・文書ごとの要約の並び）
#
# オプション:
#   --docs DIR       比べる文書（.txt）のディレクトリ（既定 <データ>/bench/docs）
#   --refresh-docs   文書を書き出し直す
#   --limit N        書き出す文書の数（既定 30）
#   --username NAME  そのユーザーの文書だけを書き出す
#   --threads N      llama-server のスレッド数（既定 .env の DOCSEED_LLM_THREADS か 3）
#   --timeout S      1 件の上限（既定 .env の DOCSEED_LLM_TIMEOUT_S か 180）
#   --mem SIZE       llama-server のメモリの上限（既定 2g。compose.yaml の llm と同じ）
#   --port N         llama-server を待ち受けるポート（既定 8081。127.0.0.1 だけ）
#   --keep-services  worker と llm を止めない（測る値は不正確になる）
#   --out DIR        結果のディレクトリ（既定 <データ>/bench/llm-<日時>）。同じ DIR を渡すとモデルを足せる
#
# 要約には文書の内容が入るので、結果はリポジトリの外（データディレクトリ）に置く。
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO"

env_get() {   # .env の値（無ければ $2）。.env は source しない（空白を含む値があるため）
  local v=""
  [ -f .env ] && v=$(sed -n "s/^$1=//p" .env | tail -n 1)
  v=${v%\"}; v=${v#\"}
  printf '%s' "${v:-$2}"
}

DATA=$(env_get DOCSEED_DATA_DIR /srv/docseed)
IMAGE=$(env_get DOCSEED_LLM_IMAGE ghcr.io/ggml-org/llama.cpp:server)
THREADS=$(env_get DOCSEED_LLM_THREADS 3)
TIMEOUT=$(env_get DOCSEED_LLM_TIMEOUT_S 180)
MAX_CHARS=$(env_get DOCSEED_LLM_MAX_INPUT_CHARS 3000)
DOCS="$DATA/bench/docs"
OUT=""
LIMIT=30
USERNAME=""
REFRESH=0
KEEP=0
MEM=2g
PORT=8081
MODELS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --docs) DOCS=$(realpath -m "$2"); shift 2 ;;
    --refresh-docs) REFRESH=1; shift ;;
    --limit) LIMIT=$2; shift 2 ;;
    --username) USERNAME=$2; shift 2 ;;
    --threads) THREADS=$2; shift 2 ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    --mem) MEM=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --keep-services) KEEP=1; shift ;;
    --out) OUT=$(realpath -m "$2"); shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    -*) echo "不明なオプションです: $1（--help で使い方）" >&2; exit 2 ;;
    *) MODELS+=("$1"); shift ;;
  esac
done

OUT=${OUT:-"$DATA/bench/llm-$(date +%Y%m%d-%H%M%S)"}
NAME=docseed-bench-llm
COMPOSE=(docker compose --project-directory "$REPO")

log() { printf '\n== %s\n' "$*"; }
api() { "${COMPOSE[@]}" exec -T api "$@"; }

command -v docker >/dev/null || { echo "docker がありません" >&2; exit 2; }
docker image inspect docseed:latest >/dev/null 2>&1 \
  || { echo "docseed:latest のイメージがありません（先に docker compose build）" >&2; exit 2; }

# 1. 文書 ---------------------------------------------------------------------
if [ "$REFRESH" = 1 ] || ! compgen -G "$DOCS/*.txt" >/dev/null; then
  case "$DOCS" in
    "$DATA"/*) ;;
    *) echo "$DOCS に文書（.txt）がありません。docseed からの書き出しはデータディレクトリの中だけに行えます" >&2
       exit 2 ;;
  esac
  log "比べる文書を書き出しています（$DOCS）"
  [ "$REFRESH" = 1 ] && rm -f "$DOCS"/*.txt
  args=(docseed export-texts --out "/srv/docseed/${DOCS#"$DATA"/}" --limit "$LIMIT")
  [ -n "$USERNAME" ] && args+=(--username "$USERNAME")
  api "${args[@]}"
fi
echo "文書: $(compgen -G "$DOCS/*.txt" | wc -l) 件（$DOCS）"

# 2. モデル -------------------------------------------------------------------
resolve() {   # 名前か GGUF のファイル名 → "ラベル ファイル名"
  if [ -f "$DATA/models/llm/$1" ]; then
    echo "$(basename "$1" .gguf) $1"
  elif [ -f "$DATA/models/llm/$1.gguf" ]; then
    echo "$1 $1.gguf"
  else
    api docseed fetch-models --llm "$1" >&2 || return 1
    echo "$1 $(api python -c 'import sys; from docseed.models import LLM_MODELS; print(LLM_MODELS[sys.argv[1]][1])' "$1")"
  fi
}

PLAN=()
if [ ${#MODELS[@]} -eq 0 ]; then
  for f in "$DATA"/models/llm/*.gguf; do
    [ -e "$f" ] && PLAN+=("$(basename "$f" .gguf) $(basename "$f")")
  done
  [ ${#PLAN[@]} -gt 0 ] || {
    echo "$DATA/models/llm/ に GGUF がありません。名前を渡すと取得します（例: scripts/bench_llm.sh sarashina2.2-1b qwen3-0.6b）" >&2
    exit 2
  }
else
  for m in "${MODELS[@]}"; do
    e=$(resolve "$m") || { echo "モデル「$m」を用意できません" >&2; exit 2; }
    PLAN+=("$e")
  done
fi

# 3. 計測 ---------------------------------------------------------------------
STOPPED=()
SAMPLER=""
cleanup() {
  [ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  if [ ${#STOPPED[@]} -gt 0 ]; then
    log "止めていたサービスを起動し直します: ${STOPPED[*]}"
    "${COMPOSE[@]}" start "${STOPPED[@]}" >/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ "$KEEP" = 0 ]; then
  for s in worker llm; do
    if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx "$s"; then
      STOPPED+=("$s")
    fi
  done
  if [ ${#STOPPED[@]} -gt 0 ]; then
    log "計測の間、止めます: ${STOPPED[*]}"
    "${COMPOSE[@]}" stop "${STOPPED[@]}" >/dev/null
  fi
fi

mkdir -p "$OUT"
docker rm -f "$NAME" >/dev/null 2>&1 || true

for entry in "${PLAN[@]}"; do
  label=${entry%% *}
  file=${entry#* }
  log "$label（$file）"
  rm -f "$OUT/$label.mem"
  docker run -d --name "$NAME" --memory "$MEM" -p "127.0.0.1:$PORT:8080" \
    -v "$DATA/models:/models:ro" "$IMAGE" \
    -m "/models/llm/$file" --host 0.0.0.0 --port 8080 -t "$THREADS" -c 4096 --parallel 1 >/dev/null

  # メモリ（docker stats の 1 回は 1〜2 秒かかる）
  ( while docker stats --no-stream --format '{{.MemUsage}}' "$NAME" >>"$OUT/$label.mem" 2>/dev/null; do
      sleep 1
    done ) &
  SAMPLER=$!

  docker run --rm --network host --user "$(stat -c %u "$OUT"):$(stat -c %g "$OUT")" \
    -v "$REPO:/w:ro" -v "$DOCS:/docs:ro" -v "$OUT:/out" docseed:latest \
    python /w/scripts/bench_llm.py run /docs --url "http://127.0.0.1:$PORT" --label "$label" --model "$file" \
      --out /out --timeout "$TIMEOUT" --max-chars "$MAX_CHARS" \
    || echo "（$label の計測は途中で止まりました。llama-server のログ: docker logs $NAME）"

  kill "$SAMPLER" 2>/dev/null || true
  wait "$SAMPLER" 2>/dev/null || true
  SAMPLER=""
  if [ "$(docker inspect -f '{{.State.OOMKilled}}' "$NAME" 2>/dev/null)" = true ]; then
    echo "メモリの上限（$MEM）を超えて止まりました"
    echo OOMKilled >>"$OUT/$label.mem"
  fi
  docker rm -f "$NAME" >/dev/null
done

# 4. 比較表 -------------------------------------------------------------------
log "比較表"
docker run --rm --user "$(stat -c %u "$OUT"):$(stat -c %g "$OUT")" -v "$REPO:/w:ro" -v "$OUT:/out" docseed:latest \
  python /w/scripts/bench_llm.py report /out --display-dir "$OUT"
