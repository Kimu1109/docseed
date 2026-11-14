-- 分割・再開できるアップロード（tus 1.0。docs/design/api.md 3.3.2）。
-- 受け取り中のデータは data_dir/files/.tmp/tus-<id> に置き、受け取った大きさ（offset）はそのファイルの大きさで表す。
-- 最後まで届いたら文書として保存し、結果（result。results[] の 1 件と同じ形の JSON）を残す。
-- 応答が届かなかった利用者が、もう一度問い合わせて結果を受け取れるよう、行は期限（expires_at）まで残す。
CREATE TABLE resumable_uploads (
  id            TEXT PRIMARY KEY,                       -- URL に使う乱数（推測できない）
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  folder_id     INTEGER NOT NULL,
  filename      TEXT NOT NULL,
  relative_path TEXT,
  on_conflict   TEXT NOT NULL DEFAULT 'rename',
  length        INTEGER NOT NULL CHECK (length >= 0),
  result        TEXT,                                   -- 保存し終えたら JSON（失敗も含む）。NULL なら受け取り中
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL
);
CREATE INDEX resumable_uploads_user ON resumable_uploads(user_id);
CREATE INDEX resumable_uploads_expires ON resumable_uploads(expires_at);
