-- JavaScript で描くページ（docs/design/extensions.md 6.10）。描画サービスで描いた HTML を保存したか。
ALTER TABLE web_clips ADD COLUMN rendered INTEGER NOT NULL DEFAULT 0 CHECK (rendered IN (0, 1));
