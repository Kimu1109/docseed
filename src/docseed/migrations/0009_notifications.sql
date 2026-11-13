-- 通知の送信待ち（docs/design/deployment.md 6.1）。
-- 送るのは api の中のスレッドだけ（worker は外部と通信しないので、積むだけにする）。
-- group_key が同じ送信待ちは 1 通にまとめる（例: 共有リンクから続けて受け取った複数のファイル）。
CREATE TABLE notifications (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,                  -- alert / share_upload / inbox / test
  level      TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'error')),
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  group_key  TEXT,
  status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,                           -- 最後の失敗（一部の宛先だけ失敗したときも残す）
  created_at TEXT NOT NULL,
  sent_at    TEXT
);
CREATE INDEX idx_notifications_status ON notifications(status, id);
