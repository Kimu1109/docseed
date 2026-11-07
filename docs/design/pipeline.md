# 取り込みパイプライン詳細設計

[設計書](../design.md) の「4. 処理フロー」の詳細。対象: アップロード後のテキスト抽出・OCR・チャンク分割・埋め込みと、それを動かすジョブキュー・worker。

## 1. 全体の流れ

```
[api] アップロード受付
  ├ 一時ファイルへストリーム書き込み（同時に SHA-256 を計算）
  ├ MIME 判定（内容から判定。拡張子は補助）
  ├ blobs を sha256 で検索
  │   ├ 既にある   → 一時ファイルを削除し、その blob を参照する document を作成（再処理しない）
  │   └ 無い       → files/ab/cd/<sha256> へ rename、blob 作成、process_blob ジョブ登録
  └ document を作成して 201 を返す

[worker] process_blob
  ① 抽出 (extract) ─ ページごと ─→ pages
  ② OCR が必要なページのみ OCR ───→ pages, ocr_lines
  ③ チャンク分割 (chunk) ─────────→ chunks（+ chunks_fts はトリガーで同期）
  ④ 埋め込み (embed) ─────────────→ chunk_vectors
  ⑤ blob.status = ready
```

- 一時ファイルは `files/.tmp/` に置き、本番と同じファイルシステム上で `rename` する（途中で落ちても壊れたファイルが残らない）。
- サムネイル（WebP, 長辺 256px）は ① のついでに生成して `derived/thumbs/<sha256>.webp` に保存する。

## 2. ジョブキュー

### 2.1 ジョブの取得

worker は 1 プロセス・1 スレッドでジョブを順に処理する。取得はアトミックに行う。

```sql
UPDATE jobs
   SET status = 'running', locked_at = :now, attempts = attempts + 1
 WHERE id = (
   SELECT id FROM jobs
    WHERE status = 'pending' AND run_after <= :now
    ORDER BY priority, id
    LIMIT 1)
RETURNING *;
```

- ジョブが無ければ 2 秒待って再取得する（api はジョブ登録時に `/srv/docseed/run/wakeup` を更新し、worker はその変更も見て早めに起きる）。
- worker 起動時に、`running` のまま 30 分以上経ったジョブを `pending` に戻す（異常終了からの復帰）。

### 2.2 優先度

| priority | 用途 |
|---|---|
| 0 | ユーザーのアップロード（`process_blob`） |
| 5 | 手動の再処理 |
| 10 | 一括の再埋め込み（`reembed_blob`）、移行作業 |
| 20 | `purge_trash` |

### 2.3 リトライ

- 失敗時: `attempts < 3` なら `run_after = now + 30秒 × 4^(attempts-1)`（30秒 → 2分 → 8分）で `pending` に戻す。
- 3 回失敗したら `failed`。`blobs.status = 'failed'`、`blobs.error` にメッセージを保存して UI に表示する。
- リトライしても直らない失敗（パスワード付き PDF、未対応形式、壊れたファイル）は即 `failed`（または `unsupported`）にする。

### 2.4 冪等性と途中再開

- 各段階は「blob 単位で削除してから作り直す」か「処理済みを飛ばす」ことで、何度実行しても同じ結果になるようにする。
- 抽出・OCR はページ単位でコミットする。再実行時は `pages` に存在するページを飛ばすので、500 ページの PDF が途中で落ちても続きから再開できる。
- チャンク分割は全ページそろってから実行し、`DELETE FROM chunks WHERE blob_id = ?` の後に作り直す（トリガーで FTS とベクトルも消える）。
- 埋め込みはベクトルの無いチャンクだけを対象にする。
- `progress_done / progress_total` を更新し、UI で進捗を表示する（抽出・OCR はページ数、埋め込みはチャンク数）。

### 2.5 パイプラインの版

- コード中の定数 `PIPELINE_VERSION`（整数）を処理ロジックの変更時に上げる。
- 管理画面から「古い版で処理された blob を再処理」を実行できる（`pages`/`chunks` を消して `process_blob` を priority 5 で登録）。
- 埋め込みモデルだけを変えた場合は `reembed_blob` のみで済む（抽出・OCR はやり直さない）。

## 3. 抽出

### 3.1 形式ごとの処理

| 形式 | 判定 | 処理 | 対応フェーズ |
|---|---|---|---|
| テキスト（txt, md, csv, tsv, json, log） | `text/*` 等 | 文字コード判定（charset-normalizer）→ UTF-8 で 1 ページとして保存 | Phase 2 |
| HTML | `text/html` | タグを除去して本文のみ（selectolax 等） | Phase 2 |
| PDF | `application/pdf` | pypdfium2 でページごとにテキスト層を抽出 → 必要なら OCR（3.2） | Phase 2 |
| 画像（jpeg, png, webp, tiff, gif, bmp, heic） | `image/*` | 前処理 → OCR。複数ページ TIFF はページごと | Phase 2（HEIC は検証後） |
| Word（docx） | OOXML | python-docx で段落・表を抽出。見出しで区切った単位を「ページ」とみなす | Phase 5 |
| Excel（xlsx） | OOXML | openpyxl でシートごとに 1 ページ（セルをタブ区切り） | Phase 5 |
| PowerPoint（pptx） | OOXML | python-pptx でスライドごとに 1 ページ（ノートも含める） | Phase 5 |
| その他 | — | 保存・共有のみ。`blobs.status = 'unsupported'` | — |

- パスワード付き・暗号化 PDF は抽出せず `unsupported`（エラーメッセージで理由を表示）。
- 上限: 1 ファイル 2000 ページまで。超えた分は処理しない（警告を表示）。

### 3.2 PDF ページで OCR するかの判定

次のどれかに当てはまるページは OCR する。

1. テキスト層の文字数（空白除く）が 20 文字未満
2. テキスト層の文字のうち、置換文字（U+FFFD）や私用領域の文字が 10% 以上（文字化けしたテキスト層）
3. ユーザーが「OCR を強制」を指定して再処理した

テキスト層と OCR の結果は混ぜない（ページ単位でどちらか一方を採用し、`pages.source` に記録する）。

### 3.3 正規化

すべてのページ本文に次を適用してから保存する。

- Unicode NFKC 正規化（全角英数 → 半角、半角カナ → 全角 など）
- 改行コードを `\n` に統一、行末の空白を除去、3 行以上の空行を 2 行に
- PDF のテキスト層で、日本語の行の途中で切れた改行（行末が句読点でなく、次の行が日本語で始まる）は結合する
- 制御文字（`\n` と `\t` 以外）を除去

検索クエリにも同じ NFKC 正規化をかける。

## 4. OCR（PP-OCRv6）

### 4.1 構成

```
ページ画像
  → 前処理
  → 文字領域の検出 (PP-OCRv6_small_det)
  → 各領域を切り出し・向き補正 (文字方向分類 0°/180°)
  → 文字認識 (PP-OCRv6_small_rec, 日本語を含む多言語統合モデル)
  → 読み順の並べ替え
  → 行テキスト + 座標 + 信頼度
```

- 推論は RapidOCR（ONNX Runtime, CPUExecutionProvider）。RapidOCR の設定は `Det/Rec.ocr_version = PP-OCRv6`、`model_type = small`、`lang_type = japan`。文字方向分類は PP-OCRv5 の textline_ori_cls（mobile）を使う。
- モデルは `small`（7.7M パラメータ）を既定とする。`medium`（34.5M）は精度が必要な文書の再処理用。`tiny` は日本語に対応していないため使わない。
- アプリ側は次のインタフェースだけに依存させ、エンジンを差し替えられるようにする。

```python
class OcrLine(NamedTuple):
    text: str
    confidence: float
    bbox: list[tuple[float, float]]   # 4 点
    vertical: bool

class OcrEngine(Protocol):
    name: str
    def recognize(self, image: PIL.Image.Image) -> list[OcrLine]: ...
```

### 4.2 前処理

| 処理 | 内容 |
|---|---|
| PDF のページ画像化 | pypdfium2 で 200 dpi（A4 で約 1654×2339px）。Phase 0 で 150/200/300 dpi の精度と速度を比較して決める |
| 回転 | 画像の EXIF Orientation を適用。ページ全体が 90°/270° 回転している場合に備え、検出された領域の縦横比から推定し、必要なら回転して再実行（初版では任意の機能） |
| サイズ | 長辺が 4000px を超える画像は 4000px に縮小 |
| 色 | RGB に変換（アルファチャンネルは白背景で合成） |

### 4.3 主なパラメータ（初期値。Phase 0 で調整）

| パラメータ | 初期値 | 説明 |
|---|---|---|
| 検出の入力サイズ上限（長辺） | 1600px | 大きいほど小さい文字を拾えるが遅い |
| 検出の二値化しきい値 | 0.3 | |
| 検出ボックスのしきい値 | 0.6 | |
| 認識バッチサイズ | 8 | |
| ONNX Runtime intra-op スレッド数 | 3 | 1 コアは api 用に残す |
| 採用する行の最低信頼度 | 0.5 | 未満の行は `ocr_lines` には保存するが本文には含めない |

### 4.4 読み順の組み立て

1. 各ボックスを横書き / 縦書きに分類（高さが幅の 1.5 倍以上かつ 2 文字以上 → 縦書き）。
2. ページ内で縦書きボックスが多数派なら縦書きページとして扱う。
3. 横書き: ボックスの中心 y で行をまとめ（y の差が行の高さの半分以下なら同じ行）、上 → 下、行内は左 → 右。
4. 縦書き: 中心 x で列をまとめ、右 → 左、列内は上 → 下。
5. 同じ行内のボックスは空白 1 つでつなぐ（日本語どうしは空白なしでつなぐ）。行の間は改行。

段組み・表の構造認識は初版では行わない（必要になったら PP-StructureV3 のレイアウト解析を追加検討）。

### 4.5 性能対策

- OCR はページ単位で実行し、1 ページごとにコミットする。
- 1 ページの OCR は 120 秒でタイムアウト（そのページは空として続行し、警告を残す）。
- CPU 温度が 80℃ を超えたら、温度が 70℃ を下回るまでページ間で待機する（`/sys/class/thermal/thermal_zone0/temp` を読む）。

## 5. チャンク分割

### 5.1 アルゴリズム

```
入力: ページ本文のリスト
パラメータ: target = 600 文字, max = 800 文字, overlap = 100 文字

for 各ページ:
    段落 = 空行で分割
    文   = 各段落を文末記号（。！？!? と「. 」）と改行で分割
    max を超える文は max で強制的に切る
    文を順に詰めていき、target を超えたらチャンクを確定
    次のチャンクの先頭には、直前チャンクの末尾から overlap 文字以内に収まる文を重ねる
    見出しらしい行（短く、句点で終わらず、次に段落が続く行）は次のチャンクの先頭に付ける
```

- チャンクはページをまたがない（検索結果からページへ移動できるようにするため）。短いページ（100 文字未満）は次のページとまとめ、`page_no` には最初のページを入れる。
- 表形式（xlsx, csv）は行の途中で切らない。先頭行（見出し行）を各チャンクの先頭に付ける。
- ruri-v3-70m の最大長は 8192 トークンなので、800 文字なら切り捨ては起きない。

### 5.2 チャンクに付ける文脈

埋め込みの入力は、本文の前にファイル名を付ける。

```
検索文書: {filename}
{chunk.text}
```

DB の `chunks.text` には本文だけを保存する（全文検索とハイライトに使うため）。

## 6. 埋め込み（ruri-v3-70m）

| 項目 | 内容 |
|---|---|
| モデル | `cl-nagoya/ruri-v3-70m`（384 次元） |
| 入力プレフィックス | 文書: `検索文書: `、クエリ: `検索クエリ: ` |
| 正規化 | L2 正規化（`normalize_embeddings=True`） |
| バッチ | 長さ順に並べ替えて 16 件ずつ（パディングの無駄を減らす） |
| 実行 | 初期: sentence-transformers + PyTorch CPU（`torch.set_num_threads(3)`）。Phase 0 の結果次第で ONNX Runtime（int8 動的量子化）に切り替える |
| 書き込み | バッチごとに `chunk_vectors` へ INSERT してコミット |

- api プロセスも検索クエリの埋め込み用に同じモデルを読み込む（`torch.set_num_threads(1)`）。モデル読み込みは起動時に行い、最初の検索が遅くならないようにする。
- モデルファイルは `/srv/docseed/models/` に事前にダウンロードしておき、実行時はオフライン（`HF_HUB_OFFLINE=1`）で読み込む。

```python
class Embedder(Protocol):
    model_name: str
    dim: int
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...  # (n, dim), float32, L2 正規化済み
    def embed_query(self, text: str) -> np.ndarray: ...             # (dim,)
```

## 7. inbox フォルダからの取り込み（任意機能）

- Samba で `/srv/docseed/inbox/<username>/` を各ユーザーに公開する。
- worker が 30 秒ごとに走査し、サイズと更新時刻が 10 秒以上変わっていないファイルを取り込む（書き込み中のファイルを避ける）。
- 取り込み先: そのユーザーのルート直下の `Inbox` フォルダ（無ければ作成）。inbox 内のサブフォルダ構造は保つ。
- 取り込み後、元ファイルは削除する（設定で `inbox/<username>/.imported/` への移動にも変更可）。
- `.` で始まるファイルと、`~$` で始まる Office の一時ファイルは無視する。

## 8. エラー一覧

| コード | 状況 | 結果 |
|---|---|---|
| `encrypted_pdf` | パスワード付き PDF | `unsupported` |
| `unsupported_type` | 対応していない形式 | `unsupported`（保存・共有は可能） |
| `corrupt_file` | ファイルが読めない | `failed`（リトライしない） |
| `too_many_pages` | 2000 ページ超 | 2000 ページまで処理して `ready`（警告つき） |
| `ocr_timeout` | 1 ページの OCR が 120 秒超 | そのページを空にして続行（警告つき） |
| `out_of_memory` | メモリ不足 | リトライ。3 回失敗で `failed` |
| `internal_error` | その他の例外 | リトライ。3 回失敗で `failed` |

警告は `blobs.warnings` に JSON 配列（`[{"code": "ocr_timeout", "page": 12}]`）で保存し、`status = ready` のまま UI に表示する。失敗の内容は `blobs.error` に保存する。
