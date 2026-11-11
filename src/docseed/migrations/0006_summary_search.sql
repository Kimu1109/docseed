-- 要約も検索の対象にする（docs/design/extensions.md 7.4）。
-- 要約は、その blob のチャンクの 1 つ（source = 'summary'、page_no は NULL、seq は -1）として持つ。
-- こうすると、キーワード検索（chunks_fts のトリガー）と意味検索（chunk_vectors）の両方にそのまま載る。
ALTER TABLE chunks ADD COLUMN source TEXT NOT NULL DEFAULT 'body' CHECK (source IN ('body', 'summary'));

-- 今ある要約を入れる（キーワード検索に載る。ベクトルは要約を作り直したとき・処理し直したときに作る）
INSERT INTO chunks(blob_id, page_no, seq, char_start, char_end, text, source)
  SELECT blob_id, NULL, -1, 0, length(summary), summary, 'summary' FROM blob_summaries;
