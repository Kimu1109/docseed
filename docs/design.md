# docseed 設計書

| 項目 | 内容 |
|---|---|
| 版 | 0.1（初版） |
| 日付 | 2026-11-07 |
| 状態 | ドラフト |
| 関連 | [ローカル OCR / Embedding モデル調査](research/ocr-embedding-models.md) |

---

## 1. 目的とスコープ

Raspberry Pi 5（SSD 起動）1台で動く、自宅/小規模向けのドキュメントサーバーを構築する。

### 1.1 提供する機能

1. **ファイルの保存と共有**: アップロード、フォルダ管理、ダウンロード、共有リンクの発行
2. **OCR**: 画像やスキャン PDF から文字を抽出し、検索できるようにする
3. **ベクトル化と検索**: 文書をチャンクに分けて埋め込み、意味検索とキーワード検索を組み合わせたハイブリッド検索を行う

### 1.2 スコープ外（初版）

- LLM による回答生成（RAG の生成部分）。検索 API までを提供し、生成は将来の拡張とする
- 複数ノード構成、高可用性
- 手書き文字の高精度認識
- 外部クラウドへの依存（すべてローカルで処理する）

### 1.3 決定済み事項

| 項目 | 決定 | 理由 |
|---|---|---|
| OCR | **PP-OCRv5 mobile（検出+認識, ONNX Runtime で実行）** | 日本語精度と Pi 5 での速度のバランスが最良。Apache-2.0 |
| Embedding | **cl-nagoya/ruri-v3-70m（384次元, 最大 8192 トークン）** | 日本語性能が高く軽量。Apache-2.0 |

## 2. 前提と制約

| 項目 | 前提 |
|---|---|
| ハードウェア | Raspberry Pi 5（RAM 8GB 以上推奨）、NVMe SSD（256GB 以上）、アクティブクーラー |
| OS | Raspberry Pi OS 64-bit（Debian 系, arm64） |
| 実行環境 | Docker + Docker Compose |
| 利用者 | 数人程度（家族・小さなチーム） |
| データ規模の目安 | ファイル 〜10万件、チャンク 〜100万件 |
| ネットワーク | LAN 内での利用が基本。外部からは VPN（Tailscale 等）経由を推奨 |
| GPU / NPU | 使わない（CPU のみ）。PCIe は SSD で使うため AI HAT は採用しない |

## 3. 全体構成

```
                    ┌───────────────── Raspberry Pi 5 ──────────────────┐
  ブラウザ / CLI     │                                                    │
  ──── HTTPS ──────▶│  Caddy (リバースプロキシ, TLS)                      │
                    │     │                                              │
                    │     ▼                                              │
                    │  docseed-api (FastAPI)                             │
                    │   ├─ 認証・ユーザー管理                              │
                    │   ├─ ファイル管理・共有リンク                          │
                    │   ├─ 検索 API（クエリ埋め込み + ハイブリッド検索）       │
                    │   └─ Web UI（静的ファイル配信）                       │
                    │     │ ジョブ登録            ▲ 結果参照                │
                    │     ▼                      │                        │
                    │  SQLite (WAL)  ◀───────────┘                        │
                    │   ├─ メタデータ / ジョブキュー                         │
                    │   ├─ FTS5 (trigram) 全文検索                         │
                    │   └─ sqlite-vec ベクトル検索                          │
                    │     ▲                                              │
                    │     │ ジョブ取得・結果書き込み                          │
                    │  docseed-worker                                    │
                    │   ├─ テキスト抽出 (pypdfium2 等)                      │
                    │   ├─ OCR (PP-OCRv5 mobile / ONNX Runtime)           │
                    │   ├─ チャンク分割                                     │
                    │   └─ 埋め込み (ruri-v3-70m)                          │
                    │                                                    │
                    │  SSD: /srv/docseed/{files,db,models,backup}         │
                    │  (任意) Samba: inbox フォルダ → 自動取り込み            │
                    └────────────────────────────────────────────────────┘
```

### 3.1 コンポーネント

| コンポーネント | 技術 | 役割 |
|---|---|---|
| reverse-proxy | Caddy | HTTPS 終端、静的ファイル、アップロードサイズ制限 |
| api | Python 3.12 / FastAPI / Uvicorn | REST API、Web UI、認証、検索 |
| worker | Python 3.12（api と同じコードベース） | テキスト抽出・OCR・埋め込みを非同期で処理 |
| db | SQLite（WAL モード）+ FTS5 + sqlite-vec | メタデータ、ジョブ、全文検索、ベクトル検索を 1 ファイルに集約 |
| storage | SSD 上のローカルファイルシステム | 原本ファイル、サムネイル、OCR 済み PDF |
| samba（任意） | Samba | LAN からの投入用フォルダ（inbox） |

### 3.2 主な技術選定の理由

- **SQLite + sqlite-vec + FTS5**: 単一ノード・少人数用途では PostgreSQL や Qdrant を別に動かすより軽く、バックアップも容易。100万チャンク × 384次元（float32 で約 1.5GB、int8 で約 0.4GB）なら総当たり検索でも実用的な速度が見込める。規模が超えたら Qdrant 等へ移行できるよう、ベクトル検索はリポジトリ層で抽象化する。
- **FTS5 trigram トークナイザ**: 形態素解析器なしで日本語の部分一致検索ができる。2文字以下のクエリは LIKE 検索にフォールバックする。
- **独自 API でファイル共有を実装**: Nextcloud は Pi 5 には重く、OCR / 検索との統合も難しい。必要な機能（保存・フォルダ・共有リンク）に絞って実装する。
- **ジョブキューは SQLite のテーブル**: Redis などの追加プロセスを持たない。worker はポーリングで処理する。
- **pypdfium2（Apache-2.0/BSD）**: PDF のテキスト抽出とページ画像化に使う。PyMuPDF は AGPL のため採用しない。

## 4. 処理フロー

### 4.1 取り込みパイプライン

```
アップロード / inbox 検知
  │
  ▼
① 保存: SHA-256 を計算 → 重複チェック → files/ に保存 → documents 登録 (status=queued)
  │
  ▼  ジョブ: extract
② 種別判定 (MIME / 拡張子)
  ├─ テキスト系 (txt, md, csv, html)        → そのまま抽出
  ├─ PDF                                   → ページごとにテキスト層を抽出
  │     └─ テキストが閾値未満のページ        → ページ画像化 (300dpi) → OCR
  ├─ 画像 (jpg, png, heic, tiff, webp)      → 前処理 → OCR
  └─ Office (docx, xlsx, pptx)              → python-docx 等で抽出（Phase 3 で LibreOffice 変換も検討）
  │
  ▼  ジョブ: chunk
③ 正規化 (NFKC, 空白整理) → チャンク分割
  │
  ▼  ジョブ: embed
④ ruri-v3-70m でバッチ埋め込み → chunks / chunk_vectors / chunks_fts に登録
  │
  ▼
⑤ documents.status = ready
```

### 4.2 OCR（PP-OCRv5）

| 項目 | 方針 |
|---|---|
| モデル | PP-OCRv5 mobile det + rec（日本語を含む多言語統合モデル）、文字方向分類器（0°/180°） |
| 実行 | ONNX Runtime（CPU, arm64）。PaddlePaddle 本体は使わない。ラッパーは RapidOCR / OnnxOCR を PoC で比較して選ぶ |
| 前処理 | EXIF 回転補正、長辺を最大 2000px 程度に縮小、グレースケール化は任意 |
| 出力 | 行ごとのテキスト・矩形座標・信頼度を `ocr_lines` に保存。座標から読み順に並べて本文を組み立てる |
| 低信頼度 | 信頼度が閾値（初期値 0.5）未満の行は保存するが、検索インデックスには含めない |
| 並列数 | worker 1 プロセス、ONNX Runtime の intra-op スレッド数 = 3（1コアは API 用に残す） |
| 将来 | 縦書き・表の精度が不足する場合、文書単位で YomiToku 等に切り替えられるよう `OcrEngine` インタフェースで抽象化する |

### 4.3 チャンク分割

- 単位: 段落 → 文の順に区切り、**目安 400〜800 文字、前後 100 文字の重なり**
- OCR 結果はページ境界でチャンクを分ける（ページ番号で原本の位置へ戻れるようにする）
- 各チャンクに `document_id`, `page`, `char_start`, `char_end` を持たせる

### 4.4 埋め込み（ruri-v3-70m）

| 項目 | 方針 |
|---|---|
| モデル | `cl-nagoya/ruri-v3-70m`（384次元） |
| プレフィックス | 文書: `検索文書: ` / クエリ: `検索クエリ: ` |
| 実行 | 初期は sentence-transformers（PyTorch CPU）。遅ければ ONNX Runtime（int8 量子化）へ切り替える |
| 正規化 | L2 正規化してコサイン類似度で比較する |
| バッチ | 16〜32 チャンク / バッチ |
| モデル版の管理 | `chunk_vectors.model` にモデル名を保存。モデル変更時は再埋め込みジョブを全件に投入する |

### 4.5 検索（ハイブリッド）

```
クエリ
 ├─ ベクトル検索: 「検索クエリ: 」+ クエリ → 埋め込み → sqlite-vec で上位 50 件
 └─ 全文検索:     FTS5 trigram で上位 50 件（2文字以下は LIKE）
        │
        ▼
 RRF (Reciprocal Rank Fusion, k=60) で統合
        │
        ▼
 アクセス権でフィルタ → 文書単位にまとめる → 上位 N 件
 （ヒットしたチャンク本文・ページ番号・ハイライトを返す）
```

- 絞り込み: フォルダ、ファイル種別、更新日、タグ
- 検索モード: `hybrid`（既定）/ `semantic` / `keyword`

## 5. データモデル（SQLite）

```sql
-- ユーザー
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,            -- argon2id
  role          TEXT NOT NULL DEFAULT 'user',  -- admin | user
  created_at    TEXT NOT NULL
);

-- フォルダ
CREATE TABLE folders (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER REFERENCES folders(id),
  owner_id   INTEGER NOT NULL REFERENCES users(id),
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (parent_id, name)
);

-- 文書（アップロードされたファイル）
CREATE TABLE documents (
  id          INTEGER PRIMARY KEY,
  folder_id   INTEGER REFERENCES folders(id),
  owner_id    INTEGER NOT NULL REFERENCES users(id),
  filename    TEXT NOT NULL,
  mime_type   TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  storage_path TEXT NOT NULL,             -- files/ab/cd/<sha256>
  page_count  INTEGER,
  status      TEXT NOT NULL,              -- queued | processing | ready | failed
  error       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  deleted_at  TEXT                        -- 論理削除（ゴミ箱）
);
CREATE INDEX idx_documents_sha256 ON documents(sha256);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE document_tags (
  document_id INTEGER NOT NULL REFERENCES documents(id),
  tag_id      INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (document_id, tag_id)
);

-- ページ単位の抽出結果
CREATE TABLE pages (
  id          INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  page_no     INTEGER NOT NULL,
  source      TEXT NOT NULL,              -- text_layer | ocr
  text        TEXT NOT NULL,
  UNIQUE (document_id, page_no)
);

-- OCR の行単位結果（座標つき）
CREATE TABLE ocr_lines (
  id         INTEGER PRIMARY KEY,
  page_id    INTEGER NOT NULL REFERENCES pages(id),
  line_no    INTEGER NOT NULL,
  text       TEXT NOT NULL,
  confidence REAL NOT NULL,
  bbox       TEXT NOT NULL                -- JSON: [[x,y],[x,y],[x,y],[x,y]]
);

-- チャンク
CREATE TABLE chunks (
  id          INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  page_no     INTEGER,
  seq         INTEGER NOT NULL,
  char_start  INTEGER NOT NULL,
  char_end    INTEGER NOT NULL,
  text        TEXT NOT NULL
);

-- 全文検索（trigram）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='trigram'
);

-- ベクトル（sqlite-vec）。rowid = chunks.id
CREATE VIRTUAL TABLE chunk_vectors USING vec0(
  embedding float[384] distance_metric=cosine
);
CREATE TABLE chunk_vector_meta (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
  model    TEXT NOT NULL                  -- 'cl-nagoya/ruri-v3-70m'
);

-- 共有リンク
CREATE TABLE shares (
  id            INTEGER PRIMARY KEY,
  token         TEXT NOT NULL UNIQUE,     -- 推測不能なランダム値（32バイト以上）
  target_type   TEXT NOT NULL,            -- document | folder
  target_id     INTEGER NOT NULL,
  created_by    INTEGER NOT NULL REFERENCES users(id),
  permission    TEXT NOT NULL DEFAULT 'read',  -- read | upload
  password_hash TEXT,                     -- 任意
  expires_at    TEXT,
  max_downloads INTEGER,
  download_count INTEGER NOT NULL DEFAULT 0,
  revoked_at    TEXT,
  created_at    TEXT NOT NULL
);

-- ジョブキュー
CREATE TABLE jobs (
  id           INTEGER PRIMARY KEY,
  type         TEXT NOT NULL,             -- extract | chunk | embed | reembed | thumbnail
  document_id  INTEGER REFERENCES documents(id),
  status       TEXT NOT NULL,             -- pending | running | done | failed
  attempts     INTEGER NOT NULL DEFAULT 0,
  run_after    TEXT NOT NULL,             -- リトライ時のバックオフ
  locked_at    TEXT,
  error        TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_jobs_pending ON jobs(status, run_after);
```

## 6. API 概要

ベースパス `/api/v1`。認証はセッション Cookie（Web UI）または API トークン（CLI・スクリプト）。

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/auth/login` / `/auth/logout` | ログイン / ログアウト |
| GET | `/folders/{id}` | フォルダの中身一覧 |
| POST | `/folders` | フォルダ作成 |
| POST | `/documents` | アップロード（multipart、複数可） |
| GET | `/documents/{id}` | メタデータ・処理状況 |
| GET | `/documents/{id}/content` | 原本ダウンロード（Range 対応） |
| GET | `/documents/{id}/text` | 抽出テキスト（ページ別） |
| PATCH | `/documents/{id}` | 名前変更・移動・タグ |
| DELETE | `/documents/{id}` | ゴミ箱へ移動 |
| POST | `/documents/{id}/reprocess` | 抽出・OCR・埋め込みのやり直し |
| GET | `/search?q=&mode=&folder=&type=&limit=` | ハイブリッド検索 |
| POST | `/shares` | 共有リンク発行 |
| DELETE | `/shares/{id}` | 共有リンク無効化 |
| GET | `/s/{token}` | 共有リンクでのアクセス（認証不要、パスワード任意） |
| GET | `/admin/jobs` | ジョブ状況（管理者） |
| GET | `/healthz` | ヘルスチェック |

## 7. ストレージ配置（SSD）

```
/srv/docseed/
├── files/            # 原本（内容アドレス: files/ab/cd/<sha256>）
├── derived/          # サムネイル、ページ画像キャッシュ
├── db/docseed.sqlite # SQLite（WAL）
├── models/           # PP-OCRv5 ONNX、ruri-v3-70m
├── inbox/            # Samba 共有の投入フォルダ（任意）
└── backup/           # ローカルスナップショット（外部への退避は restic）
```

- 同じ内容のファイルは実体を 1 つだけ持つ（SHA-256 で重複排除）
- 物理削除はゴミ箱の保持期間（既定 30 日）を過ぎてから、参照がなくなった実体のみ行う

## 8. 非機能要件

### 8.1 性能目標（実機の PoC で確認・調整する）

| 項目 | 目標 |
|---|---|
| 検索の応答時間 | p95 1 秒以内（チャンク 10万件時） |
| テキスト層つき PDF | 1ページあたり 0.5 秒以内（抽出から埋め込みまで） |
| OCR | A4 1ページあたり 10 秒以内 |
| アップロード | 受け付けは即時応答。処理は非同期 |
| 最大ファイルサイズ | 1GB（Caddy と API で制限） |

### 8.2 リソース配分（RAM 8GB の場合の目安）

| プロセス | 目安 |
|---|---|
| api（クエリ埋め込み用にモデルを常駐） | 〜1GB |
| worker（OCR + 埋め込み） | 〜2GB |
| Caddy / Samba / OS | 〜1GB |
| ページキャッシュ（SQLite・ベクトル） | 残り |

- worker の並列数は 1。重い処理は直列に流し、温度上昇とメモリ不足を防ぐ
- CPU 温度を監視し、80℃ を超えたら worker の取得間隔を延ばす

### 8.3 セキュリティ

- 外部公開は原則しない。外からは Tailscale 等の VPN 経由でアクセスする
- 公開する場合は Caddy で HTTPS（Let's Encrypt）、ログイン試行のレート制限を行う
- パスワードは argon2id でハッシュ化する
- 共有リンクのトークンは 32 バイト以上の乱数。期限・パスワード・ダウンロード回数制限・無効化に対応する
- アップロードファイルは実行しない。MIME をサーバー側で判定し、ダウンロード時は `Content-Disposition: attachment` と `X-Content-Type-Options: nosniff` を付ける
- パス操作はすべて ID 経由で行い、ユーザー入力をファイルパスに使わない

### 8.4 バックアップ

- SQLite は `sqlite3 .backup`（またはオンラインバックアップ API）で毎日スナップショットを取る
- `files/` と DB スナップショットを restic で外付けドライブまたは別マシンへ毎日退避する
- 埋め込みとOCR結果は原本から再生成できるため、復旧手順に「再処理」を含める

### 8.5 運用・監視

- 構造化ログ（JSON）を標準出力へ。Docker のログローテーションを設定する
- `/healthz` と `/admin/jobs` でジョブの滞留・失敗を確認する
- 失敗ジョブは指数バックオフで最大 3 回リトライし、その後は `failed` にして UI に表示する

## 9. リポジトリ構成（予定）

```
docseed/
├── docs/
│   ├── design.md
│   └── research/
├── src/docseed/
│   ├── api/          # FastAPI ルーター
│   ├── core/         # 設定、DB 接続、認証
│   ├── storage/      # ファイル保存・重複排除
│   ├── pipeline/     # extract / ocr / chunk / embed
│   ├── search/       # ハイブリッド検索、RRF
│   ├── jobs/         # ジョブキュー、worker
│   └── web/          # Web UI（静的ファイル）
├── migrations/
├── tests/
├── scripts/          # モデル取得、ベンチマーク
├── docker/
│   ├── Dockerfile
│   └── Caddyfile
├── compose.yaml
└── pyproject.toml
```

## 10. 開発フェーズ

| フェーズ | 内容 | 完了条件 |
|---|---|---|
| 0. PoC | Pi 5 上で PP-OCRv5（RapidOCR / OnnxOCR）と ruri-v3-70m を計測 | 速度・精度・RAM が性能目標を満たすことを確認。ラッパーを決定 |
| 1. 基盤 | DB スキーマ、認証、アップロード / ダウンロード、フォルダ | ブラウザからファイルを保存・取得できる |
| 2. パイプライン | ジョブキュー、テキスト抽出、OCR、チャンク分割、埋め込み | PDF と画像が自動で `ready` になる |
| 3. 検索 | ハイブリッド検索 API と UI、ハイライト | 自前の質問セットで Recall@5 を測定できる |
| 4. 共有 | 共有リンク（期限・パスワード・無効化）、Samba inbox | 外部の人にリンクで渡せる |
| 5. 運用 | バックアップ、監視、Office 文書対応 | 復旧手順を実際に試して成功する |

## 11. 未決事項・リスク

| 項目 | 内容 | 対応 |
|---|---|---|
| OCR ラッパーの選定 | RapidOCR と OnnxOCR のどちらで PP-OCRv5 を動かすか | Phase 0 で比較 |
| 縦書き・表の精度 | PP-OCRv5 mobile で不足する可能性 | `OcrEngine` で抽象化し、server モデルや YomiToku を差し替え可能にする |
| sqlite-vec の規模上限 | 100万チャンク超で総当たり検索が遅くなる可能性 | int8 量子化、または Qdrant へ移行 |
| FTS5 trigram | 2文字以下の語が検索できない | LIKE 検索へフォールバック |
| HEIC 対応 | arm64 でのデコード用ライブラリ | pillow-heif を検証 |
| 熱・SSD 寿命 | 長時間の OCR バッチ、SQLite の書き込み | アクティブクーラー必須、WAL チェックポイントの調整 |
