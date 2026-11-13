-- お気に入り・最近開いたファイル・検索の履歴・保存した検索（docs/design/ui.md S11）。すべて利用者ごと。

-- お気に入り（文書かフォルダ）。対象が消えた・見られなくなったものは、読むときに除く（完全に削除したら消す）。
CREATE TABLE favorites (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL CHECK (target_type IN ('document', 'folder')),
  target_id   INTEGER NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, target_type, target_id)
);

-- 最近開いたファイル（文書の画面を開いたとき）。利用者ごとに新しい 50 件だけ残す。
CREATE TABLE recent_documents (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  opened_at   TEXT NOT NULL,
  PRIMARY KEY (user_id, document_id)
);
CREATE INDEX idx_recent_documents_user ON recent_documents(user_id, opened_at);

-- 検索の履歴（画面で検索したとき）。同じ条件は 1 件にまとめ、利用者ごとに新しい 30 件だけ残す。
-- params は検索の条件（q・mode・絞り込み）の JSON（キーの順をそろえた文字列なので、同じ条件は同じ値になる）。
CREATE TABLE search_history (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  params      TEXT NOT NULL,
  searched_at TEXT NOT NULL,
  UNIQUE (user_id, params)
);
CREATE INDEX idx_search_history_user ON search_history(user_id, searched_at);

-- 保存した検索（名前を付けて残す。上限 100 件）。
CREATE TABLE saved_searches (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  params     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_saved_searches_user ON saved_searches(user_id, id);
