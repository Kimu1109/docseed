-- ユーザーごとの容量の上限（docs/design/data-model.md 3.1）。
-- NULL なら全体の既定（settings の quota.default_bytes。無ければ上限なし）、0 なら上限なし、正の数ならその大きさ（バイト）。
-- ゴミ箱の保持日数（利用者ごと）は user_prefs の trash_retention_days に置く（表は変えない）。
ALTER TABLE users ADD COLUMN quota_bytes INTEGER;
