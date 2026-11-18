# 設計 08: データベース

> 関連: [ドメインモデル](03-domain-model.md) / [migration運用](09-migrations.md) / [インフラ・運用](10-infra-ops.md) / [セキュリティ](07-security.md)

---

## 9. データベース

### 共通規約

- SQLite（WAL）、全テーブル`STRICT`（3.37以上）。接続ごとに`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`
- ID: UUIDv7を36文字のTEXTで保存（外部に出さない）
- 時刻: UTC固定長ISO 8601 `YYYY-MM-DDTHH:MM:SS.ffffffZ`（27文字）のTEXT。固定長なので文字列比較が時刻順と一致する。生成は`Clock`アダプターのみ
- 秘密値は`*_hash`のみ保存（HMACはHEX64文字、Argon2idはエンコード文字列）
- 本文の空は`''`（NULLにしない）。APIも`""`を返す
- 親`users`削除で子はすべてCASCADE削除（管理者の直接DB削除用）。アプリに削除機能は作らない

### スキーマ

```sql
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
```

**設計メモ**:
- `content`のドメイン上限は6,000コードポイント。DBのCHECKは**意図的にこれより緩い**（100,000）。将来ドメイン側の上限だけを引き上げる場合に、SQLiteのテーブル再作成を伴うmigrationを避けるため
- `recovery_credentials`は1ユーザー1行。パスワード変更・回復・作成のたびに**同じ行をUPDATE**して`recovery_key_hash`を差し替える（3件保持・スロット管理・有効期限は採用しない）
- `sessions.last_seen_at`は作成時に設定し、更新は前回から5分以上経過した時だけ
- `updated_at`は`users` / `profiles` / `login_credentials` / `recovery_credentials`のみ
- rate limit用テーブルは作らない（MVPはインメモリ）
- IPアドレス・セキュリティイベントログ用のテーブルは作らない

### 主要クエリ（すべてparameterized）

| 用途 | 概要 |
|---|---|
| 公開取得 | `SELECT public_id, content FROM profiles WHERE public_id = ? AND status = 'published'` |
| Session認証 | `token_hash`で`sessions`を検索 → 有効性はドメインで判定 |
| touch | `UPDATE sessions SET last_seen_at = ? WHERE id = ? AND last_seen_at <= ?`（閾値＝現在−5分） |
| 全Session失効 | `UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL` |
| ログイン照合 | `login_id_hash`で`login_credentials`を検索（不在でもダミーArgon2検証を実行） |
| 回復用アカウント検索 | `SELECT user_id FROM recovery_credentials WHERE recovery_key_hash = ?` |