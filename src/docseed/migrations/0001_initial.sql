-- 0001: 初期スキーマ（docs/design/data-model.md 3 章と同じ内容）
-- sqlite-vec 拡張をロードした接続で実行すること（chunk_vectors のため）

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

