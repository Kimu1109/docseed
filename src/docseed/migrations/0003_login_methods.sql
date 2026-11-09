-- Phase 7: ログイン方法（パスキー・確認コード・復旧コード・OIDC）。docs/design/extensions.md 5.8。
-- 0002 は Phase 6（画像の派生ファイルとジョブの種類）用に空けてある。マイグレーションは番号順に
-- 未適用のものを適用するので、0003 を先に適用しても後から 0002 を適用できる。

ALTER TABLE users ADD COLUMN webauthn_handle TEXT;                         -- 32 バイトの乱数（base64url）
ALTER TABLE users ADD COLUMN password_login  INTEGER NOT NULL DEFAULT 1;   -- 0 = パスワードでのログインを使わない
ALTER TABLE sessions ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'password';
ALTER TABLE sessions ADD COLUMN reauth_at   TEXT;

CREATE TABLE webauthn_credentials (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id BLOB NOT NULL UNIQUE,
  public_key    BLOB NOT NULL,                  -- COSE 形式
  sign_count    INTEGER NOT NULL DEFAULT 0,
  transports    TEXT,                           -- JSON 配列
  backed_up     INTEGER NOT NULL DEFAULT 0,     -- 同期されるパスキーか（BS フラグ）
  aaguid        TEXT,
  name          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT
);
CREATE INDEX idx_webauthn_user ON webauthn_credentials(user_id);

CREATE TABLE user_totp (
  user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  secret       TEXT NOT NULL,                   -- base32
  last_step    INTEGER,                         -- 最後に使ったコードの時刻の番号
  enabled_at   TEXT NOT NULL
);

CREATE TABLE user_recovery_codes (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash  TEXT NOT NULL,                     -- SHA-256
  used_at    TEXT
);
CREATE INDEX idx_recovery_user ON user_recovery_codes(user_id);

CREATE TABLE user_identities (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL,                  -- 'google' など
  subject       TEXT NOT NULL,                  -- ID トークンの sub
  email         TEXT,                           -- 表示用
  created_at    TEXT NOT NULL,
  last_login_at TEXT,
  UNIQUE (provider, subject)
);
CREATE INDEX idx_identities_user ON user_identities(user_id);

-- 短い間だけ使う値。passkey_* は WebAuthn の challenge、second_factor はパスワードが合った後の
-- 2 段階目の待ち（challenge に {remember, next} の JSON）、totp_enroll は確認前の TOTP の秘密の値
CREATE TABLE auth_challenges (
  id         TEXT PRIMARY KEY,                  -- 乱数
  purpose    TEXT NOT NULL CHECK (purpose IN ('passkey_register', 'passkey_login', 'second_factor',
                                              'totp_enroll')),
  user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
  challenge  BLOB,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_auth_challenges_expires ON auth_challenges(expires_at);
