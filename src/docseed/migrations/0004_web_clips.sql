-- Phase 8: URL の取り込み（Web クリップ）。docs/design/extensions.md 6.7。

ALTER TABLE documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'
  CHECK (kind IN ('file', 'webclip'));

CREATE TABLE web_clips (
  id            INTEGER PRIMARY KEY,
  -- フォルダを完全に削除したら、取得待ち・失敗したままのクリップも消す
  folder_id     INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
  document_id   INTEGER REFERENCES documents(id) ON DELETE CASCADE,  -- 取得が済むまで NULL
  url           TEXT NOT NULL,                 -- 利用者が入れた URL
  final_url     TEXT,                          -- リダイレクトの後
  canonical_url TEXT,
  title         TEXT,
  description   TEXT,
  site_name     TEXT,
  author        TEXT,
  published_at  TEXT,
  image_blob_id INTEGER REFERENCES blobs(id),  -- og:image
  meta          TEXT,                          -- og:* と twitter:* と oEmbed の JSON
  http_status   INTEGER,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'fetching', 'done', 'failed')),
  error         TEXT,                          -- {"code": ..., "message": ...}
  created_by    INTEGER NOT NULL REFERENCES users(id),
  created_at    TEXT NOT NULL,
  fetched_at    TEXT
);
CREATE INDEX idx_web_clips_document ON web_clips(document_id);
CREATE INDEX idx_web_clips_canonical ON web_clips(canonical_url);
CREATE INDEX idx_web_clips_folder ON web_clips(folder_id);

-- fetch_url は blob_id が NULL なので、同じクリップの取得を重複して積まないよう payload の clip_id で防ぐ
CREATE UNIQUE INDEX uq_jobs_clip ON jobs(json_extract(payload, '$.clip_id'))
  WHERE type = 'fetch_url' AND status IN ('pending', 'running');
