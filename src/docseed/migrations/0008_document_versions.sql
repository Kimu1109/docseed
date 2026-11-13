-- 文書の版の履歴（docs/design/data-model.md 3.3）。
-- 同じ名前で上書きしたとき・Web クリップを再取得したときに、それまでの中身（blob）を過去の版として残す。
-- 過去の版の blob は、どの文書からも参照されていなくても消さない（documents.gc_blobs）。
-- 文書を完全に削除すると、その文書の過去の版もカスケードで消える。
CREATE TABLE document_versions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 消した版の ID を使い回さない（古いリンクで別の版を開かない）
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  blob_id     INTEGER NOT NULL REFERENCES blobs(id),
  filename    TEXT NOT NULL,                  -- その版だったときのファイル名
  created_by  INTEGER REFERENCES users(id),   -- その版を追加した人
  created_at  TEXT NOT NULL,                  -- その版が今の版になった日時
  archived_by INTEGER REFERENCES users(id),   -- 次の版で置き換えた人（Web クリップの再取得では、クリップを作った人）
  archived_at TEXT NOT NULL                   -- 過去の版になった日時
);
CREATE INDEX idx_document_versions_doc ON document_versions(document_id, id);
CREATE INDEX idx_document_versions_blob ON document_versions(blob_id);

-- 今の版を追加した人と日時（名前の変更などでは変わらない）。NULL なら created_by / created_at と同じ。
ALTER TABLE documents ADD COLUMN content_updated_at TEXT;
ALTER TABLE documents ADD COLUMN content_updated_by INTEGER REFERENCES users(id);
