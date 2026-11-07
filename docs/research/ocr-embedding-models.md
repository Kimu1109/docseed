# Raspberry Pi 5 向けローカル OCR / Embedding モデル調査

調査日: 2026-11-07
前提: Raspberry Pi 5（Cortex-A76 4コア / RAM 8GB 以上を想定、GPU なし）、ストレージは SSD、日本語ドキュメントが中心。

> **更新（2026-11-07）:** その後の予備計測をふまえ、OCR は PP-OCRv5 の後継である **PP-OCRv6 small**（2026-06 公開, Apache-2.0）を採用した。詳細は [設計書](../design.md) 1.3。本文は調査時点の内容のまま残している。

> 注意: Raspberry Pi 5 上で日本語 OCR / 埋め込みを測った公開ベンチマークはほぼ見つからなかった。
> 下記の速度感は他環境の数値とモデル規模からの推定であり、**実機での計測が必須**（末尾の検証計画を参照）。

---

## 1. OCR

### 1.1 候補一覧

| 方式 | モデル / ツール | 規模 | 日本語 | Pi 5 (CPU) での現実性 | ライセンス | コメント |
|---|---|---|---|---|---|---|
| 従来型 | **Tesseract 5** (`jpn`, `jpn_vert`) | 小 | △ | ◎ apt で即導入、最軽量 | Apache-2.0 | 速いが実文書での誤認識が多い。縦書き/表に弱い |
| 検出+認識 | **PaddleOCR 3.x / PP-OCRv5 (mobile)** | 数十MB | ○ | ○ ONNX Runtime 経由が楽 | Apache-2.0 | 1モデルで日中英など5種の文字に対応。精度と速度のバランスが最良 |
| 検出+認識 | PP-OCRv5 (server) | 大 | ○+ | △ 遅い | Apache-2.0 | mobile で不足する場合のみ |
| 日本語特化 | **YomiToku** | 中 | ◎ | △ CPU 動作可だが重い。PyTorch 必要 | CC BY-NC-SA 4.0（商用は別ライセンス、要確認） | 日本語文書の精度は最上位クラス。レイアウト・表・縦書きにも対応 |
| 小型VLM | Granite-Docling (258M) | 0.26B | △（多言語は実験的） | △ | Apache-2.0 | 表・レイアウトを Markdown/DocTags に変換。日本語は要検証 |
| 小型VLM | PaddleOCR-VL (0.9B) | 0.9B | ○ | ✕〜△ 1ページ数十秒〜分の見込み | Apache-2.0 | llama.cpp に認識部分のみ対応済(b8110〜)。レイアウト検出は PaddlePaddle 必須 |
| 大型VLM | dots.ocr (~1.7B), olmOCR, MinerU 等 | 1B〜 | ○ | ✕ | 各種 | GPU 前提。Pi では非現実的 |

### 1.2 推奨

1. **第一候補: PP-OCRv5 mobile を ONNX Runtime で動かす**
   - PaddlePaddle 本体（ARM64 での導入が面倒）を避け、ONNX 化済みモデル（例: `onnxocr-ppocrv5` / OnnxOCR）か RapidOCR 系を使う。
   - 日本語精度は Tesseract より明確に上。ある日本語医療文書の比較では編集距離(誤り率)が Tesseract 78% / PaddleOCR 27% / YomiToku 9% 程度（ハードウェア不明、参考値）。
2. **精度重視のオプション: YomiToku**
   - 個人利用・非商用なら最有力。ただしライセンス（非商用）と Pi 上での処理時間を要確認。夜間バッチ処理などに回す運用なら許容できる可能性あり。
3. **フォールバック/最軽量: Tesseract**
   - 英語中心・速度最優先のファイル、または PDF にテキスト層があるか判定する前段に使う。
4. **前処理で OCR 自体を減らす**
   - テキスト層を持つ PDF / Office 文書は OCR せずに抽出（`pdftotext`, `pymupdf`, Apache Tika, Docling のテキスト抽出など）。
   - 画像のみの PDF / 画像ファイルだけ OCR キューへ。`ocrmypdf` で「検索可能 PDF」を生成して保存する構成も相性が良い（ocrmypdf のエンジンは Tesseract、プラグインで他エンジンも可）。

## 2. Embedding（ベクトル化）

### 2.1 候補一覧

| モデル | パラメータ | 次元 | 最大長 | 日本語性能 (JMTEB 平均) | ライセンス | Pi 5 現実性 |
|---|---|---|---|---|---|---|
| **cl-nagoya/ruri-v3-30m** | 37M | 256 | 8192 | 74.5 | Apache-2.0 | ◎ 非常に軽い |
| **cl-nagoya/ruri-v3-70m** | 70M | 384 | 8192 | 75.5 | Apache-2.0 | ◎ |
| cl-nagoya/ruri-v3-130m | 130M | 512 | 8192 | 76.6 | Apache-2.0 | ○ |
| cl-nagoya/ruri-v3-310m | 310M | 768 | 8192 | 77.2 前後 | Apache-2.0 | △ 初回の大量投入は時間がかかる |
| intfloat/multilingual-e5-small | 118M | 384 | 512 | 69.5 | MIT | ○ |
| google/EmbeddingGemma-300m | 300M | 768 (MRL で 512/256/128 に縮小可) | 2048 | 未確認 (MMTEB 多言語 61.2) | Gemma 利用規約 | ○ GGUF/ONNX あり |
| Qwen3-Embedding-0.6B | 600M | 1024 | 32K | 未確認 (MMTEB 多言語 64.3) | Apache-2.0 | △ EmbeddingGemma の数分の1の速度という報告 |
| BAAI/bge-m3 | 568M | 1024 (dense+sparse) | 8192 | 高め | MIT | △ 重い。ハイブリッド検索を1モデルで済ませたい場合 |

### 2.2 推奨

- **日本語中心なら ruri-v3 系一択に近い。** 30m でも multilingual-e5-small/base を上回る。
  - まず **ruri-v3-70m**（品質/速度のバランス）で始め、速度が足りなければ 30m、精度が足りなければ 130m。
  - クエリには `検索クエリ: `、文書には `検索文書: ` のプレフィックスを付ける（ruri-v3 の仕様）。
  - SentencePiece のみで動き MeCab 等の形態素解析器が不要なので ARM でも導入が簡単。
- **英語など他言語も多い場合:** EmbeddingGemma-300m（MRL で次元を落としてストレージ節約可）か multilingual-e5-small。
- 実行形式: `sentence-transformers`（PyTorch CPU）で始め、遅ければ ONNX Runtime（int8 量子化）や llama.cpp（GGUF、`llama-server --embedding`）に切り替える。
- **ハイブリッド検索推奨:** ベクトル検索だけでなく全文検索（SQLite FTS5 + 日本語 tokenizer、または Meilisearch 等）を併用すると、型番・固有名詞・数字の検索漏れを防げる。

## 3. （参考）アクセラレータについて

- **Raspberry Pi AI HAT+ 2 (Hailo-10H, 8GB, 40 TOPS INT4)**: LLM/VLM (Llama-3.2-3B, Qwen2.5-VL-3B 等) を載せられるが、対応モデルは Hailo 側が用意したものに限られ、OCR/Embedding モデルの公式サポートは確認できなかった。
- **注意:** Pi 5 の PCIe は1レーンのみ。NVMe SSD と AI HAT を同時に使うには PCIe スイッチ付きの HAT 等が必要。**SSD 運用の場合はまず CPU のみで構成するのが無難。**

## 4. 当面の構成案（OCR / Embedding 部分）

```
アップロード / 共有フォルダ
   │
   ├─ テキスト層あり (PDF, docx 等) ──→ テキスト抽出
   └─ 画像 / スキャン PDF ──────────→ OCR キュー (PP-OCRv5 mobile / ONNX、精度重視なら YomiToku)
                                         │
                       チャンク分割 (500〜1000字程度, 重なり 10〜20%)
                                         │
                      ruri-v3-70m で埋め込み ─→ ベクトル DB (sqlite-vec / Qdrant など)
                                         └──→ 全文検索インデックス (FTS5 など)
```

- OCR / 埋め込みは重いので **非同期ジョブキュー** で処理し、アップロード応答を待たせない。
- RAM 8GB なら OCR と埋め込みを同時に常駐させても収まる見込みだが、YomiToku / VLM 系を使う場合はワーカーの並列数を 1 に制限する。
- ファイル保存・共有（Nextcloud / Seafile / Samba など）とベクトル DB の選定は別途調査。

## 5. 実機検証計画

Pi 5 上で以下を計測して最終決定する。

| 項目 | 対象 | 指標 |
|---|---|---|
| OCR 速度 | Tesseract / PP-OCRv5 mobile (ONNX) / YomiToku | A4 1ページあたり秒数、ピーク RAM |
| OCR 精度 | 手元の代表的な文書 10〜20 ページ（横書き・縦書き・表・スマホ撮影） | 文字誤り率 (CER)、目視評価 |
| 埋め込み速度 | ruri-v3-30m / 70m / 130m、EmbeddingGemma | chunks/秒（PyTorch vs ONNX int8） |
| 検索品質 | 自分の文書に対する質問 20〜30 件 | Recall@5 / MRR |
| 熱 | 連続処理 30 分 | CPU 温度・スロットリング有無（アクティブクーラー推奨） |

## 参考

- PaddleOCR / Tesseract / YomiToku 日本語比較（スライド）: https://docs.google.com/presentation/d/1gKepFYDkuWqhP9c7H7pGOUPNIhrCA3ix/htmlpresent
- PaddleOCR vs Tesseract: https://codesota.com/ocr/paddleocr-vs-tesseract
- PaddleOCR-Lite on RPi5 (Q-engineering): https://github.com/Qengineering/PaddleOCR-Lite-License/blob/main/README.md
- OnnxOCR (PP-OCRv5 ONNX): https://github.com/elecmonkey/OnnxOCR / https://pypi.org/project/onnxocr-ppocrv5/0.0.2/
- PaddleOCR 3.0 技術報告: https://web3.arxiv.org/pdf/2507.05595
- PaddleOCR-VL の llama.cpp 対応: https://insiderllm.com/guides/paddleocr-vl-local-document-ocr/
- OCR モデル比較: https://blog.roboflow.com/best-open-source-ocr-models/ , https://www.spheron.network/blog/best-open-source-ocr-vlm-self-host-gpu-cloud-2026/
- Linux OCR ツールの比較（日本語）: https://speakerdeck.com/matoken/recently-tried-linux-ocr-tools
- ruri-v3: https://huggingface.co/cl-nagoya/ruri-v3-70m , https://huggingface.co/cl-nagoya/ruri-v3-130m , https://huggingface.co/cl-nagoya/ruri-v3-310m
- ruri-v3-30m 採用事例 (ADR): https://glama.ai/mcp/servers/@kzmshx/frontmatter-mcp/blob/085c956e538bf4675568c4a957f98e5619764ce1/docs/adr/0009-use-ruri-v3-30m-for-japanese-text-embedding.md
- EmbeddingGemma 論文: https://arxiv.org/abs/2509.20354v2 / 比較: https://huggingface.co/janihal/embeddinggemma-300m-8bit-mlx
- Raspberry Pi AI HAT+ 2: https://www.raspberrypi.com/products/ai-hat-plus-2/ , https://www.cnx-software.com/2026/01/15/raspberry-pi-ai-hat-2-targets-generative-ai-llm-vlm-with-hailo-10h-accelerator/
