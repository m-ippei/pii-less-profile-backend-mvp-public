"""実 SQLite を使う Repository テストの共通ヘルパー（design/11-testing.md §12 repository 層）。

- `migrated_db_path` は一時ディレクトリに `migrations/001_initial.sql` を適用した DB を返す。
  Repository テストが「別のスキーマ」で通らないように、実際の migration を必ず通す。
- `make_account` / `make_session` は design/03-domain-model.md §4 の Entity を固定値で
  組み立てる。ハッシュは DB の `length(*_hash) = 64` CHECK を通すダミー。

テスト以外から import してはいけない。
"""

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

from scripts import migrate
from src.identity.domain.entities import (
    Account,
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.value_objects import UserId

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

# Argon2 エンコードの見た目を保つダミー。検証はしない（hash だけ保存されることを見るため）。
FAKE_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=1,p=1$ZmFrZXNhbHQ$ZmFrZWhhc2g"


def fake_hash(seed: str) -> str:
    """64文字の HEX。DB の `CHECK (length(*_hash) = 64)` を通すためのダミーハッシュ。"""
    return hashlib.sha256(seed.encode()).hexdigest()


def migrated_db_path(tmp_path: Path) -> Path:
    """migration を適用済みの空DB（一時ディレクトリ）を作る。"""
    path = tmp_path / "app.db"
    result = migrate.run(path, MIGRATIONS_DIR, out=io.StringIO(), err=io.StringIO())
    if result != 0:
        raise RuntimeError("failed to apply migrations to the test database")
    return path


def make_account(
    *,
    user_id: UserId,
    now: datetime = START,
    login_id_hash: str | None = None,
    recovery_key_hash: str | None = None,
) -> Account:
    """`Account`（User + LoginCredential + RecoveryCredential）を固定値で組み立てる。"""
    return Account(
        user=User(id=user_id, created_at=now, updated_at=now),
        login_credential=LoginCredential.issue(
            user_id=user_id,
            login_id_hash=login_id_hash or fake_hash(f"login_id:{user_id.value}"),
            password_hash=FAKE_PASSWORD_HASH,
            now=now,
        ),
        recovery_credential=RecoveryCredential.issue(
            user_id=user_id,
            recovery_key_hash=recovery_key_hash or fake_hash(f"recovery_key:{user_id.value}"),
            now=now,
        ),
    )


def make_session(*, id: UserId, user_id: UserId, token_hash: str, now: datetime = START) -> Session:
    """`Session`（`expires_at` は作成時+14日）を固定値で組み立てる。"""
    return Session.create(id=id, user_id=user_id, token_hash=token_hash, now=now)
