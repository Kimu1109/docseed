-- 要約を手で直す・消す（docs/design/extensions.md 7.4）。
-- 要約（blob_summaries）は同じ内容のファイルで共有し、別の利用者の文書と共有することもあるので、
-- 手で直した内容は文書ごとに持つ（ほかの文書の要約は変えない）。summary が '' なら「この文書では要約を出さない」。
CREATE TABLE document_summaries (
  document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  summary     TEXT NOT NULL,
  edited_by   INTEGER REFERENCES users(id),
  edited_at   TEXT NOT NULL
);

-- 手で直した要約も検索に載せる: その文書だけのチャンク（source = 'summary'、document_id を持つ）にする。
-- document_id が NULL のチャンクは、今まで通り blob のすべての文書に共通。
ALTER TABLE chunks ADD COLUMN document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE;
CREATE INDEX idx_chunks_document ON chunks(document_id) WHERE document_id IS NOT NULL;
