# Phase 0: 実機計測スクリプト

[設計書](../../docs/design.md) の Phase 0（PoC）用のスクリプトです。Raspberry Pi 5 の実機で OCR・埋め込み・検索の速度、精度、メモリ、温度を測ります。結果をもとに、設計書の「12. 未決事項」の次の 4 点を決めます。

| 決めること | 使うスクリプト | 判断材料 |
|---|---|---|
| PP-OCRv6 small で足りるか（medium との差） | `bench_ocr.py` | 速度、CER（文字誤り率）、メモリ |
| PDF を画像にする DPI と検出サイズ | `bench_ocr.py --dpi --det-side` | 速度と CER のバランス |
| 埋め込みの実行方式（PyTorch / ONNX / ONNX int8）とスレッド数 | `bench_embed.py` | chunks/s、クエリの応答時間、torch との差（コサイン類似度） |
| SQLite（sqlite-vec / FTS5）で目標の検索速度が出るか | `bench_vec.py` | 10 万件での検索時間 |

## ファイル

| ファイル | 内容 |
|---|---|
| `run_all.sh` | 標準の計測をまとめて実行する |
| `make_samples.py` | 合成のサンプル画像と正解テキストを作る（横書き・小さい文字・撮影風・縦書き） |
| `bench_ocr.py` | OCR の計測（エンジン、DPI、検出サイズ、連続処理） |
| `bench_embed.py` | 埋め込みの計測（モデル、実行方式、スレッド数） |
| `bench_vec.py` | sqlite-vec と FTS5 trigram の規模ごとの計測 |
| `common.py` | 温度・スロットリング・メモリの取得、結果の出力 |
| `requirements.txt` | 依存パッケージ |

## 1. 準備

### 1.1 Raspberry Pi

- アクティブクーラーを付ける（無いと数分で温度による速度低下が起きる）
- 実運用と同じ SSD から起動し、このリポジトリも SSD 上に置く
- 計測中は他の重い処理を止める

```bash
sudo apt update
sudo apt install -y git python3-venv python3-dev fonts-noto-cjk
# 参考比較用（任意）
sudo apt install -y tesseract-ocr tesseract-ocr-jpn tesseract-ocr-jpn-vert
```

### 1.2 Python 環境

```bash
git clone https://github.com/kimu1109/docseed.git
cd docseed/scripts/poc
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt     # torch を含むため 10〜20 分かかることがある
```

モデルは初回実行時に自動でダウンロードされます（RapidOCR は modelscope.cn、埋め込みは huggingface.co から）。保存先は `models/` です。

### 1.3 実文書（推奨）

合成サンプルだけでは判断できないので、普段扱う文書を `samples/real/` に置いてください（Git には入りません）。

- 10〜20 ページ程度: スキャン PDF、スマホで撮った書類、表を含む文書、縦書き、テキストを含む PDF など
- 正解テキストがあれば CER を計算できます。数ページ分だけでも用意すると判断しやすくなります
  - 画像 `foo.jpg` → `foo.gt.txt`
  - PDF `foo.pdf` → `foo.p1.gt.txt`, `foo.p2.gt.txt`, …（ページごと）
  - 正解テキストは改行・空白を無視して比較するので、行の区切りは気にしなくて構いません

## 2. 実行

### まとめて実行

```bash
QUICK=1 ./run_all.sh    # まず短縮版（10〜20 分）で動作確認
./run_all.sh            # 標準（1〜2 時間。30 分の連続処理を含む）
```

ログは `results/run_all-<日時>.log`、詳細な記録は `results/*.jsonl` に保存されます。

### 個別に実行

```bash
python make_samples.py

# OCR: エンジンの比較（認識結果のテキストも保存）
python bench_ocr.py samples/synthetic samples/real \
  --engines rapidocr-v6-small,rapidocr-v6-medium,rapidocr-v5-mobile --save-text out/text

# OCR: DPI と検出サイズ
python bench_ocr.py samples/real --engines rapidocr-v6-small --dpi 150,200,300 --det-side 960,1600,2400

# OCR: 30 分の連続処理
python bench_ocr.py samples/real --engines rapidocr-v6-small --duration 1800 --cool-to 100

# 埋め込み（OCR の結果をコーパスとして使う）
python bench_embed.py --corpus out/text --backends torch,onnx,onnx-int8
python bench_embed.py --models cl-nagoya/ruri-v3-70m --backends torch,onnx-int8 --threads 1,3,4

# ベクトル検索・全文検索（100 万件は DB が数 GB になる）
python bench_vec.py --sizes 10000,100000
python bench_vec.py --sizes 1000000
```

各スクリプトのオプションは `--help` で確認できます。

### OCR エンジン

| 名前 | 内容 |
|---|---|
| `rapidocr-v6-small` | PP-OCRv6 small（採用） |
| `rapidocr-v6-medium` | PP-OCRv6 medium（精度重視の再処理用。重い） |
| `rapidocr-v5-mobile` | 参考: 前世代の PP-OCRv5 mobile |
| `rapidocr-v5-server` | 参考: PP-OCRv5 server |
| `onnxocr-v5` | 参考: OnnxOCR で PP-OCRv5 mobile |

PP-OCRv6 の tiny は日本語に対応していないため含めていません。medium は x86 の予備計測でも small の約 20 倍の時間がかかったため、実文書が多い場合は `--max-pages` で枚数を絞るか、`--engines` から外してください。
| `tesseract` | 参考: 従来型の Tesseract |

## 3. 判定の目安

設計書「8.1 性能目標」との対応です。

| 項目 | 目安 | 見る値 |
|---|---|---|
| OCR の速度 | A4 1 ページ 10 秒以内 | `bench_ocr` の 平均・p95 (s/頁)（200 dpi の PDF で） |
| OCR の精度 | 実文書で CER 5% 以下を目標（文書の質に大きく左右される） | `bench_ocr` の CER と、`out/text` の目視確認 |
| OCR のメモリ | worker の上限 3GB に収まる | ピーク RSS |
| 温度 | 30 分の連続処理で 80℃ 未満、`throttled` が `0x0` | 最高温度、`throttled_after` |
| 埋め込みの速度 | テキスト PDF 1 ページ 0.5 秒以内（1 ページ ≒ 1〜2 チャンク）→ 4 chunks/s 以上 | chunks/s |
| クエリの埋め込み | 150ms 以内 | クエリ p95（スレッド 1 で） |
| int8 の劣化 | torch との cos が 0.98 以上なら許容 | torch 比 cos（最小） |
| 検索 | ベクトル + 全文で 10 万件 500ms 以内（残りを埋め込み・統合に使う） | `bench_vec` の検索 p95 と MATCH p95 |

`throttled` の値は `vcgencmd get_throttled` の結果です。`0x0` 以外なら電源不足（bit 0）や温度による制限（bit 2, 3）が起きています。

## 4. 結果の共有

`results/` の `*.jsonl` と `run_all-*.log` をコミット・プッシュしてもらえれば、それをもとに分析して設計書を更新します（`results/db/` と実文書は Git に入りません）。OCR の認識結果 `out/text/` には文書の内容が含まれるため、コミットしないでください。

## 注意

- 計測値は温度や他の処理の影響を受けます。各設定の開始前に CPU が 60℃ 以下になるまで待つようにしています（`--cool-to`）。
- 設定ごとに別プロセスで実行し、モデルごとのピークメモリを分けて測っています。
- `bench_embed.py` の `onnx` / `onnx-int8` は、初回だけ ONNX への変換と量子化を行います（`models/onnx/` に保存）。初回はその分だけ読込時間とピークメモリが大きく出るので、比較には 2 回目以降の値を使ってください。
- `bench_ocr.py` の読み順の組み立ては設計書 pipeline.md 4.4 の試作です。CER には読み順の誤りも含まれます。
