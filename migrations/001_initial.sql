CREATE TABLE users (
  id          TEXT PRIMARY KEY CHECK (length(id) = 36),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE login_credentials (
  user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  login_id_hash       TEXT NOT NULL UNIQUE CHECK (length(login_id_hash) = 64),
  password_hash       TEXT NOT NULL,
  password_changed_at TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
) STRICT;

CREATE TABLE recovery_credentials (
  user_id            TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  recovery_key_hash  TEXT NOT NULL UNIQUE CHECK (length(recovery_key_hash) = 64),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
) STRICT;

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  CHECK (expires_at > created_at)
) STRICT;
CREATE INDEX ix_sessions_user_active ON sessions(user_id) WHERE revoked_at IS NULL;

CREATE TABLE profiles (
  id             TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id        TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  public_id      TEXT NOT NULL UNIQUE CHECK (length(public_id) = 18),
  status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
  content        TEXT NOT NULL DEFAULT '' CHECK (length(content) <= 100000),
  published_at   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  CHECK ((status = 'published') = (published_at IS NOT NULL)),
  CHECK (status = 'draft' OR length(content) > 0)
) STRICT;
