#!/usr/bin/env bash
# Phase 0 の標準的な計測を順に実行する。
#
#   ./run_all.sh            # 標準（約 1〜2 時間。連続処理 30 分を含む）
#   QUICK=1 ./run_all.sh    # 動作確認用の短縮版（約 10〜20 分）
#
# 実文書は samples/real/ に置く（無ければ合成サンプルだけで計測する）。
# ログは results/run_all-<日時>.log に保存する。
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python}
QUICK=${QUICK:-0}
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p results
LOG=results/run_all-$STAMP.log
exec > >(tee -a "$LOG") 2>&1

echo "=== docseed Phase 0 計測 ($STAMP) ==="
command -v vcgencmd >/dev/null && vcgencmd measure_temp && vcgencmd get_throttled || true

# 1. 合成サンプル
$PY make_samples.py

INPUTS=(samples/synthetic)
if [ -d samples/real ] && [ -n "$(ls -A samples/real 2>/dev/null)" ]; then
  INPUTS+=(samples/real)
else
  echo "※ samples/real/ が空のため、合成サンプルだけで OCR を計測します"
fi

if [ "$QUICK" = 1 ]; then
  MAXP=(--max-pages 6); SOAK=120; EMB_N=64; VEC_SIZES=10000
else
  MAXP=(); SOAK=1800; EMB_N=256; VEC_SIZES=10000,100000
fi

echo; echo "=== 2. OCR: エンジン比較 ==="
$PY bench_ocr.py "${INPUTS[@]}" "${MAXP[@]}" \
  --engines rapidocr-v6-small,rapidocr-v6-medium,rapidocr-v5-mobile \
  --models-dir models --save-text out/text

echo; echo "=== 3. OCR: DPI と検出サイズ（rapidocr-v6-small） ==="
$PY bench_ocr.py "${INPUTS[@]}" "${MAXP[@]}" \
  --engines rapidocr-v6-small --dpi 150,200,300 --det-side 960,1600,2400 --models-dir models

echo; echo "=== 4. OCR: 連続処理 ${SOAK} 秒（温度・スロットリング） ==="
$PY bench_ocr.py "${INPUTS[@]}" --engines rapidocr-v6-small --duration "$SOAK" \
  --warmup 0 --cool-to 100 --models-dir models
command -v vcgencmd >/dev/null && vcgencmd get_throttled || true

echo; echo "=== 5. 埋め込み ==="
CORPUS=()
[ -d out/text ] && CORPUS=(--corpus out/text)
$PY bench_embed.py --n "$EMB_N" "${CORPUS[@]}" \
  --models cl-nagoya/ruri-v3-30m,cl-nagoya/ruri-v3-70m,cl-nagoya/ruri-v3-130m \
  --backends torch,onnx,onnx-int8
$PY bench_embed.py --n "$EMB_N" "${CORPUS[@]}" \
  --models cl-nagoya/ruri-v3-70m --backends torch,onnx-int8 --threads 1,3,4

echo; echo "=== 6. ベクトル検索・全文検索 ==="
$PY bench_vec.py --sizes "$VEC_SIZES"

echo; echo "=== 完了: $LOG ==="
