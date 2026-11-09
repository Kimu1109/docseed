-- Phase 9: 整理の補助（フォルダ・タグの候補、要約）。docs/design/extensions.md 7.7。

CREATE TABLE blob_summaries (
  blob_id         INTEGER PRIMARY KEY REFERENCES blobs(id) ON DELETE CASCADE,
  summary         TEXT NOT NULL,
  tags            TEXT NOT NULL,             -- LLM が出したタグの JSON 配列（置き換える前）
  model           TEXT NOT NULL,             -- GGUF のファイル名
  summary_version INTEGER NOT NULL,
  elapsed_ms      INTEGER,
  created_at      TEXT NOT NULL
);

CREATE TABLE document_suggestions (
  id          INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('folder', 'tag')),
  folder_id   INTEGER REFERENCES folders(id) ON DELETE CASCADE,  -- kind = 'folder'
  tag_name    TEXT,                                               -- kind = 'tag'
  source      TEXT NOT NULL CHECK (source IN ('embedding', 'llm')),
  score       REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open'
              CHECK (status IN ('open', 'accepted', 'rejected', 'auto_applied')),
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);
CREATE INDEX idx_suggestions_document ON document_suggestions(document_id, status);

-- フォルダのパスのベクトル（7.2 の 6）。rowid = folders.id。
-- path は埋め込んだときのパス表記。今のパスと違えば作り直す（フォルダの作成・名前の変更・移動に追従する）
CREATE VIRTUAL TABLE folder_vectors USING vec0(
  embedding float[384] distance_metric=cosine,
  +path TEXT
);

CREATE TABLE user_prefs (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

-- suggest_document は blob_id が NULL（文書ごと）なので、同じ文書のジョブを重複して積まないよう payload で防ぐ
CREATE UNIQUE INDEX uq_jobs_suggest ON jobs(json_extract(payload, '$.document_id'))
  WHERE type = 'suggest_document' AND status IN ('pending', 'running');
