-- Phase 6: 派生ファイル（サムネイル・プレビュー）と画像の情報、ジョブの種類の追加。
-- docs/design/extensions.md 1.2・4.4。
-- 0003（Phase 7）を先に適用した DB にも、後から適用できる（互いに別の表・列だけを変える）。

ALTER TABLE blobs ADD COLUMN thumb_path TEXT;   -- derived/thumbs/<sha256>.webp（無ければ NULL）
ALTER TABLE blobs ADD COLUMN view_path  TEXT;   -- derived/view/<sha256>.webp（無ければ NULL）
ALTER TABLE blobs ADD COLUMN image_meta TEXT;   -- JSON: {width, height, taken_at, camera, has_gps}

-- SQLite では CHECK 制約を変えられないので jobs を作り直す。
-- Phase 6〜9 のジョブの種類（derive_blob, fetch_url, suggest_document, summarize_blob）をまとめて足す。
CREATE TABLE jobs_new (
  id         INTEGER PRIMARY KEY,
  type       TEXT NOT NULL CHECK (type IN ('process_blob', 'reembed_blob', 'purge_trash', 'derive_blob',
                                           'fetch_url', 'suggest_document', 'summarize_blob')),
  blob_id    INTEGER REFERENCES blobs(id) ON DELETE CASCADE,
  payload    TEXT,
  priority   INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
  attempts   INTEGER NOT NULL DEFAULT 0,
  run_after  TEXT NOT NULL,
  locked_at  TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
INSERT INTO jobs_new SELECT id, type, blob_id, payload, priority, status, attempts, run_after, locked_at, error,
                            created_at, finished_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX idx_jobs_ready ON jobs(status, priority, run_after);
CREATE UNIQUE INDEX uq_jobs_active ON jobs(type, blob_id)
  WHERE status IN ('pending', 'running');
