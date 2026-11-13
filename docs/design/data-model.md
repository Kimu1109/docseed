# データモデル詳細設計

[設計書](../design.md) の「5. データモデル」の詳細。DB は SQLite（WAL モード）1 ファイル `/srv/docseed/db/docseed.sqlite`。

## 1. 方針

- **ファイルの実体（blob）と文書（document）を分ける。** 同じ内容のファイルが複数回アップロードされても、保存・テキスト抽出・OCR・埋め込みは 1 回だけ行う。
  - `blobs`: 内容（SHA-256）単位。抽出結果・チャンク・ベクトルは blob にぶら下がる
  - `documents`: ユーザーから見えるファイル。フォルダ・ファイル名・タグを持ち、blob を参照する
- 日時はすべて UTC の ISO 8601 文字列（`2026-11-07T12:34:56Z`）。
- 削除は論理削除（`deleted_at`）→ 保持期間後に物理削除。
- 外部キー制約を有効にする（`PRAGMA foreign_keys = ON`）。
- スキーマ変更は `migrations/NNNN_*.sql` を番号順に適用し、`schema_migrations` に記録する。

### 接続時の PRAGMA

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size = -65536;   -- 64MB
PRAGMA mmap_size = 268435456; -- 256MB
PRAGMA temp_store = MEMORY;
```

拡張: `sqlite-vec` を接続ごとにロードする。必要な SQLite のバージョンは 3.35 以上（FTS5 trigram と `RETURNING` を使うため）。

## 2. ER 図

```
users ─┬─< sessions
       ├─< api_tokens
       ├─< folders (owner) ─┬─< folders (子フォルダ)
       │                    ├─< folder_members >── users
       │                    └─< documents >── blobs ─┬─< pages ─< ocr_lines
       │                           │                 ├─< chunks ─── chunks_fts (全文検索)
       │                           │                 │        └─── chunk_vectors (ベクトル)
       │                           │                 └─< jobs
       │                           └─< document_tags >── tags
       └─< shares (document または folder を指す)

audit_log, settings, schema_migrations は独立
```

## 3. テーブル定義

### 3.1 ユーザー・認証

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,                 -- argon2id
  role          TEXT NOT NULL DEFAULT 'user'
                CHECK (role IN ('admin', 'user')),
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,               -- SHA-256(セッションID)。Cookie には元の値
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  user_agent   TEXT,
  ip           TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE api_tokens (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  token_prefix TEXT NOT NULL,                  -- 表示用の先頭 8 文字
  token_hash   TEXT NOT NULL UNIQUE,           -- SHA-256
  scopes       TEXT NOT NULL DEFAULT 'read,write',
  last_used_at TEXT,
  expires_at   TEXT,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT
);
```

### 3.2 フォルダ・権限

```sql
CREATE TABLE folders (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER REFERENCES folders(id),
  owner_id   INTEGER NOT NULL REFERENCES users(id),
  name       TEXT NOT NULL,                    -- ルートフォルダは ''
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);
-- 同じ親の下で名前の重複を禁止（ゴミ箱内は除く）
CREATE UNIQUE INDEX uq_folders_name ON folders(parent_id, name COLLATE NOCASE)
  WHERE deleted_at IS NULL AND parent_id IS NOT NULL;
-- ユーザーごとにルートフォルダは 1 つ
CREATE UNIQUE INDEX uq_folders_root ON folders(owner_id) WHERE parent_id IS NULL;

-- 他ユーザーへのフォルダ共有（サブフォルダに継承される）
CREATE TABLE folder_members (
  folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('viewer', 'editor')),
  granted_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (folder_id, user_id)
);
CREATE INDEX idx_folder_members_user ON folder_members(user_id);
```

- フォルダの所有者 = ルートフォルダの所有者。サブフォルダの `owner_id` は親と同じ値を入れる（権限判定を簡単にするため）。
- 権限の判定方法は [セキュリティ設計](security.md) を参照。

### 3.3 ファイル実体・文書

```sql
CREATE TABLE blobs (
  id               INTEGER PRIMARY KEY,
  sha256           TEXT NOT NULL UNIQUE,
  size_bytes       INTEGER NOT NULL,
  mime_type        TEXT NOT NULL,              -- サーバー側で判定した値
  storage_path     TEXT NOT NULL,              -- files/ab/cd/<sha256>
  page_count       INTEGER,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'unsupported')),
  progress_done    INTEGER NOT NULL DEFAULT 0, -- 処理済みページ数
  progress_total   INTEGER NOT NULL DEFAULT 0,
  pipeline_version INTEGER,                    -- 処理したパイプラインの版
  embedding_model  TEXT,                       -- 例: 'cl-nagoya/ruri-v3-70m'
  error            TEXT,                       -- 失敗時のエラー（code と message の JSON）
  warnings         TEXT,                       -- 警告の JSON 配列（例: [{"code": "ocr_timeout", "page": 12}]）
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,              -- 処理状況の変化を UI へ通知するために使う
  processed_at     TEXT
);
CREATE INDEX idx_blobs_status ON blobs(status);
CREATE INDEX idx_blobs_updated ON blobs(updated_at);

CREATE TABLE documents (
  id          INTEGER PRIMARY KEY,
  folder_id   INTEGER NOT NULL REFERENCES folders(id),
  blob_id     INTEGER NOT NULL REFERENCES blobs(id),
  filename    TEXT NOT NULL,
  created_by  INTEGER NOT NULL REFERENCES users(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  deleted_at  TEXT,
  deleted_by  INTEGER REFERENCES users(id),
  uploaded_via_share_id INTEGER REFERENCES shares(id)  -- 共有リンク経由で受け取った場合
);
CREATE UNIQUE INDEX uq_documents_name ON documents(folder_id, filename COLLATE NOCASE)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_documents_blob ON documents(blob_id);
CREATE INDEX idx_documents_updated ON documents(updated_at);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE document_tags (
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (document_id, tag_id)
);
```

- 同じフォルダに同名ファイルをアップロードした場合は、API の指定に従い「別名で保存（`name (2).pdf`）」「置き換え」「エラー」のいずれかにする。既定は「別名で保存」。
- 「置き換え」は `documents.blob_id` を差し替え、それまでの中身を **過去の版**（`document_versions`、0008）として残す。中身が同じ（同じ blob）なら版は増やさない。Web クリップの再取得も同じ。

```sql
CREATE TABLE document_versions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 消した版の ID を使い回さない
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  blob_id     INTEGER NOT NULL REFERENCES blobs(id),
  filename    TEXT NOT NULL,                  -- その版だったときのファイル名
  created_by  INTEGER REFERENCES users(id),   -- その版を追加した人
  created_at  TEXT NOT NULL,                  -- その版が今の版になった日時
  archived_by INTEGER REFERENCES users(id),   -- 次の版で置き換えた人
  archived_at TEXT NOT NULL                   -- 過去の版になった日時
);
-- documents に content_updated_at / content_updated_by（今の版を追加した日時と人。NULL なら created_at / created_by）
```

- 1 文書あたり新しいものから `DOCSEED_VERSIONS_KEEP`（既定 10）件だけ残し、古いものから消す（置き換えのたびと、毎日の片付け（`purge_trash`）で）。0 なら版を残さない。
- 過去の版の blob は、どの文書からも参照されていなくても、版がある間は消さない（`gc_blobs`）。版を消す・文書を完全に削除する（カスケード）と、参照がなくなった blob を消す。
- 過去の版は検索の対象にしない（チャンクは blob に付くが、検索は今の版の文書からたどる）。使用量（`usage_bytes`）にも数えない。
- 過去の版に戻すと、今の版を過去の版として残し、戻した版の行を消す（戻したことも取り消せる）。

### 3.4 抽出結果

```sql
CREATE TABLE pages (
  id             INTEGER PRIMARY KEY,
  blob_id        INTEGER NOT NULL REFERENCES blobs(id) ON DELETE CASCADE,
  page_no        INTEGER NOT NULL,             -- 1 始まり。画像やテキストファイルは 1 ページ
  source         TEXT NOT NULL CHECK (source IN ('text', 'text_layer', 'ocr', 'office')),
  text           TEXT NOT NULL,
  ocr_confidence REAL,                         -- OCR の場合、行の信頼度の平均
  width          INTEGER,                      -- OCR に使った画像の px
  height         INTEGER,
  UNIQUE (blob_id, page_no)
);

CREATE TABLE ocr_lines (
  id         INTEGER PRIMARY KEY,
  page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  line_no    INTEGER NOT NULL,                 -- 読み順
  text       TEXT NOT NULL,
  confidence REAL NOT NULL,
  bbox       TEXT NOT NULL,                    -- JSON [[x,y]x4]（画像 px）
  vertical   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_ocr_lines_page ON ocr_lines(page_id);
```

### 3.5 チャンク・検索インデックス

```sql
CREATE TABLE chunks (
  id         INTEGER PRIMARY KEY,
  blob_id    INTEGER NOT NULL REFERENCES blobs(id) ON DELETE CASCADE,
  page_no    INTEGER,
  seq        INTEGER NOT NULL,                 -- blob 内での通し番号
  char_start INTEGER NOT NULL,                 -- ページ本文内の位置
  char_end   INTEGER NOT NULL,
  text       TEXT NOT NULL,                    -- NFKC 正規化済み
  UNIQUE (blob_id, seq)
);

-- 全文検索（外部コンテンツ方式。chunks と同期させるトリガーを作る）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text,
  content = 'chunks',
  content_rowid = 'id',
  tokenize = 'trigram'
);

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  DELETE FROM chunk_vectors WHERE rowid = old.id;
END;
CREATE TRIGGER chunks_au AFTER UPDATE OF text ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ベクトル（sqlite-vec）。rowid = chunks.id
CREATE VIRTUAL TABLE chunk_vectors USING vec0(
  embedding float[384] distance_metric=cosine
);
```

- 埋め込みモデルは全チャンクで 1 種類に統一する。使用中のモデルは `settings` の `embedding.model` に保存し、各 blob の `embedding_model` と一致しないものは再埋め込みの対象とする。
- モデルの次元が変わる場合は、マイグレーションで `chunk_vectors` を作り直す。
- 保存量の目安: 100万チャンク × 384次元 × float32 ≒ 1.5GB。容量や速度が問題になったら `int8[384]` に量子化する。

### 3.6 共有リンク

```sql
CREATE TABLE shares (
  id             INTEGER PRIMARY KEY,
  token          TEXT NOT NULL UNIQUE,         -- 32 バイトの乱数（base64url）
  target_type    TEXT NOT NULL CHECK (target_type IN ('document', 'folder')),
  target_id      INTEGER NOT NULL,
  permission     TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read', 'upload')),
  password_hash  TEXT,                         -- argon2id。任意
  expires_at     TEXT,
  max_downloads  INTEGER,
  download_count INTEGER NOT NULL DEFAULT 0,
  created_by     INTEGER NOT NULL REFERENCES users(id),
  created_at     TEXT NOT NULL,
  revoked_at     TEXT
);
CREATE INDEX idx_shares_target ON shares(target_type, target_id);
```

- トークンは平文で保存する（後からリンクを再表示できるようにするため）。DB を読める攻撃者は同じディスク上の原本も読めるため、ハッシュ化しても防げる範囲が小さいと判断した。一方、API トークンはアカウントの全権限を持つためハッシュで保存する。

### 3.7 ジョブ

```sql
CREATE TABLE jobs (
  id         INTEGER PRIMARY KEY,
  type       TEXT NOT NULL CHECK (type IN ('process_blob', 'reembed_blob', 'purge_trash')),
  blob_id    INTEGER REFERENCES blobs(id) ON DELETE CASCADE,
  payload    TEXT,                             -- JSON（オプション）
  priority   INTEGER NOT NULL DEFAULT 0,       -- 小さいほど優先
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
  attempts   INTEGER NOT NULL DEFAULT 0,
  run_after  TEXT NOT NULL,
  locked_at  TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_jobs_ready ON jobs(status, priority, run_after);
-- 同じ blob に未完了の同種ジョブを重複して積まない
CREATE UNIQUE INDEX uq_jobs_active ON jobs(type, blob_id)
  WHERE status IN ('pending', 'running');
```

完了したジョブは 30 日後に削除する。

### 3.8 その他

```sql
CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 例: ('embedding.model', 'cl-nagoya/ruri-v3-70m'), ('pipeline.version', '1')

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL,
  user_id     INTEGER REFERENCES users(id),
  action      TEXT NOT NULL,   -- login, login_failed, upload, download, delete, restore,
                               -- share_create, share_revoke, share_access, member_grant など
  target_type TEXT,
  target_id   INTEGER,
  ip          TEXT,
  detail      TEXT             -- JSON
);
CREATE INDEX idx_audit_at ON audit_log(at);

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
```

利用者ごとの記録（0010、[画面設計](ui.md) S11）:

| 表 | 内容 |
|---|---|
| `favorites(user_id, target_type, target_id, created_at)` | お気に入り（`document` / `folder`）。対象への外部キーは持たず、読むときに今の権限とゴミ箱を確かめ、消えた対象の行は消す |
| `recent_documents(user_id, document_id, opened_at)` | 最近開いたファイル。開き直すと消して入れ直す（rowid の順＝開いた順）。利用者ごとに 50 件 |
| `search_history(id, user_id, params, searched_at)` | 検索の履歴。`params` は条件の JSON（キーの順をそろえる）で、同じ条件は 1 件。利用者ごとに 30 件 |
| `saved_searches(id, user_id, name, params, created_at)` | 保存した検索。利用者ごとに 100 件まで |

## 4. 削除とゴミ箱

| 操作 | 処理 |
|---|---|
| 文書を削除 | `documents.deleted_at` を設定。検索・一覧から除外される |
| フォルダを削除 | フォルダと配下のフォルダ・文書すべてに同じ `deleted_at` を設定（一括で復元できるように同じ値にする） |
| 復元 | `deleted_at` を NULL に戻す。元のフォルダに同名がある場合は別名にする。元のフォルダも削除済みならルートに戻す |
| 完全削除（ゴミ箱を空にする / 保持期間 30 日経過） | `documents` 行を削除 → どの文書からも参照されなくなった blob を削除（`pages`/`chunks`/ベクトルはカスケード、ファイル実体も削除） |

完全削除は `purge_trash` ジョブが 1 日 1 回実行する。

## 5. 容量の見積もり（目安）

| 項目 | 1万文書・10万チャンク | 10万文書・100万チャンク |
|---|---|---|
| chunks（本文） | 〜100MB | 〜1GB |
| chunks_fts（trigram 索引） | 〜300MB | 〜3GB |
| chunk_vectors（float32） | 〜150MB | 〜1.5GB |
| ocr_lines | 〜50MB | 〜500MB |
| 合計（DB） | 〜0.6GB | 〜6GB |

trigram 索引は本文の数倍の大きさになる。SSD 256GB でも十分収まるが、原本ファイルの容量を別に見込むこと。
