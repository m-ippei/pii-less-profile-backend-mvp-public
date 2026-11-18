"""Identity スキーマの制約と原子性のテスト（一時ディレクトリの実 SQLite ファイル）。

migration（`migrations/001_initial.sql`）を適用した空DBに対して、T-04-01 / T-04-02 の
Repository が想定する制約と失敗時の振舞いを固定する。このファイルが守る設計上の約束は
次の6点。

1. 一意制約: `login_id_hash` / `recovery_key_hash` / `token_hash` の重複は
   `sqlite3.IntegrityError` のまま呼び出し元へ出る（M06 の一意衝突再生成 H-8 の支点）。
   主キー（`users.id` / `login_credentials.user_id` / `sessions.id`）の重複も拒否される。
2. CHECK: `users.id` の36文字、`*_hash` の64文字、`sessions.expires_at > created_at`、
   `profiles` の状態 CHECK が違反行を拒否する（design/08-database.md §9 スキーマ）。
3. CASCADE: `users` を直接削除すると子テーブル（資格情報・Session）が消える
   （`PRAGMA foreign_keys=ON` の接続で。design/08-database.md §9 共通規約）。
4. 部分一意索引 `ix_sessions_user_active` が存在し、未失効 Session の検索で使われる。
5. 原子性: `UnitOfWork` 内の書き込みが途中で失敗すると rollback され、DB に残らず、
   未コミットの変更は他接続から見えない（design/10-infra-ops.md §11）。
6. 全SQLが parameterized。SQL断片を値として渡しても文として実行されない。

根拠: design/08-database.md §9 / design/09-migrations.md §10 / design/11-testing.md §12。
"""

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.identity.domain.entities import SESSION_TTL, Account, Session
from src.identity.domain.value_objects import UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.identity.infrastructure.session_repository import SqlSessionRepository
from src.shared.clock.clock import format_timestamp
from src.shared.db import UnitOfWork, connect
from tests.support.sqlite_identity import fake_hash, make_account, make_session, migrated_db_path

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
OTHER_USER_ID = UserId("018f3c2a-7b1c-7abd-8def-0123456789ac")
SESSION_ID = UserId("018f3c2a-7b1c-7abe-8def-0123456789ad")
OTHER_SESSION_ID = UserId("018f3c2a-7b1c-7abf-8def-0123456789ae")
PROFILE_ID = UserId("018f3c2a-7b1c-7ac1-8def-0123456789b0")

TOKEN_HASH = fake_hash("session:token")
OTHER_TOKEN_HASH = fake_hash("session:other_token")

# マイクロ秒が丸められる formatter を見逃さないよう、意図的に 0 以外を使う。
MOMENT = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=UTC)

CHECK_TABLES = ("users", "login_credentials", "recovery_credentials", "sessions", "profiles")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return migrated_db_path(tmp_path)


def save_account(db_path: Path, account: Account) -> None:
    """ユースケースと同じ形で `UnitOfWork` の中で保存する。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlAccountRepository(uow.connection).save(account)


def seed_user(db_path: Path, user_id: UserId) -> None:
    """`users` と資格情報の行を1件用意する（子テーブルの FK 用）。"""
    save_account(db_path, make_account(user_id=user_id, now=MOMENT))


def save_session(db_path: Path, session: Session) -> None:
    with UnitOfWork(connect(db_path)) as uow:
        SqlSessionRepository(uow.connection).save(session)


def find_by_login_id_hash(db_path: Path, login_id_hash: str) -> Account | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlAccountRepository(uow.connection).find_by_login_id_hash(login_id_hash)


def make_active_session(session_id: UserId, token_hash: str) -> Session:
    return make_session(id=session_id, user_id=USER_ID, token_hash=token_hash, now=MOMENT)


def count_rows(db_path: Path) -> dict[str, int]:
    """各テーブルの行数を、Entity を経由せず生の SQL で数える。"""
    reader = connect(db_path)
    try:
        return {
            "users": reader.execute("SELECT count(*) FROM users").fetchone()[0],
            "login_credentials": reader.execute(
                "SELECT count(*) FROM login_credentials"
            ).fetchone()[0],
            "recovery_credentials": reader.execute(
                "SELECT count(*) FROM recovery_credentials"
            ).fetchone()[0],
            "sessions": reader.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "profiles": reader.execute("SELECT count(*) FROM profiles").fetchone()[0],
        }
    finally:
        reader.close()


def test_duplicate_credential_hashes_are_rejected(db_path: Path) -> None:
    """`login_id_hash` / `recovery_key_hash` の重複が `IntegrityError` のまま出ること。

    一意衝突は Repository で握りつぶさず呼び出し元へ渡す。M06 はこの例外を捕まえて
    秘密値を再生成する（H-8）。別 user の `save` が途中の `users` 行ごと rollback され、
    DB が半端な状態にならないこともあわせて見る。
    """
    shared_login = fake_hash("shared_login_id")
    shared_recovery = fake_hash("shared_recovery_key")
    save_account(
        db_path,
        make_account(
            user_id=USER_ID,
            now=MOMENT,
            login_id_hash=shared_login,
            recovery_key_hash=shared_recovery,
        ),
    )

    with pytest.raises(sqlite3.IntegrityError, match="login_id_hash"):
        save_account(
            db_path,
            make_account(user_id=OTHER_USER_ID, now=MOMENT, login_id_hash=shared_login),
        )
    with pytest.raises(sqlite3.IntegrityError, match="recovery_key_hash"):
        save_account(
            db_path,
            make_account(user_id=OTHER_USER_ID, now=MOMENT, recovery_key_hash=shared_recovery),
        )

    counts = count_rows(db_path)
    assert counts["users"] == 1
    assert counts["login_credentials"] == 1
    assert counts["recovery_credentials"] == 1


def test_duplicate_token_hash_is_rejected(db_path: Path) -> None:
    """`sessions.token_hash` の UNIQUE 衝突が `IntegrityError` になること。

    `id` が違っても `token_hash` が同じなら拒否される。Session 認証は `token_hash` の
    一意性に依存するため、重複行ができると本人特定が曖昧になる。
    """
    seed_user(db_path, USER_ID)
    save_session(db_path, make_active_session(SESSION_ID, TOKEN_HASH))

    with pytest.raises(sqlite3.IntegrityError, match="token_hash"):
        save_session(db_path, make_active_session(OTHER_SESSION_ID, TOKEN_HASH))

    assert count_rows(db_path)["sessions"] == 1


def test_primary_key_constraints_reject_raw_duplicates(db_path: Path) -> None:
    """Repository を経由しない重複行も主キーで拒否されること。

    Repository の UPSERT は `id` / `user_id` の衝突を「同じ行の更新」として吸収する。
    ここでは生の INSERT で主キーの実体（`users.id` / `login_credentials.user_id` /
    `sessions.id`）が定義どおり効いていることを確認する。
    """
    seed_user(db_path, USER_ID)
    save_session(db_path, make_active_session(SESSION_ID, TOKEN_HASH))
    stamp = format_timestamp(MOMENT)

    connection = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="users.id"):
            connection.execute(
                "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
                (USER_ID.value, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="login_credentials.user_id"):
            connection.execute(
                "INSERT INTO login_credentials "
                "(user_id, login_id_hash, password_hash, password_changed_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (USER_ID.value, fake_hash("distinct"), "h", stamp, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="sessions.id"):
            connection.execute(
                "INSERT INTO sessions "
                "(id, user_id, token_hash, created_at, last_seen_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    SESSION_ID.value,
                    USER_ID.value,
                    OTHER_TOKEN_HASH,
                    stamp,
                    stamp,
                    format_timestamp(MOMENT + SESSION_TTL),
                    None,
                ),
            )
    finally:
        connection.close()


def test_check_constraints_reject_invalid_rows(db_path: Path) -> None:
    """スキーマの CHECK 制約が違反行を拒否すること（design/08-database.md §9）。

    - `users.id` は36文字（UUIDv7）
    - `*_hash` は64文字（HEX64 / Argon2エンコード）
    - `sessions.expires_at > created_at`（絶対期限の意味）
    - `profiles.status` は `draft` / `published` のみ

    Repository を通せば Value Object が守るが、DB 自体の防波堤が効いていることを
    直接 INSERT / UPDATE で確認する。CHECK は違反した式をメッセージに含む。
    """
    seed_user(db_path, USER_ID)
    stamp = format_timestamp(MOMENT)
    short_hash = "0" * 63

    connection = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("length(id) = 36")):
            connection.execute(
                "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
                ("too-short-id", stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("length(login_id_hash) = 64")):
            connection.execute(
                "UPDATE login_credentials SET login_id_hash = ? WHERE user_id = ?",
                (short_hash, USER_ID.value),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match=re.escape("length(recovery_key_hash) = 64")
        ):
            connection.execute(
                "UPDATE recovery_credentials SET recovery_key_hash = ? WHERE user_id = ?",
                (short_hash, USER_ID.value),
            )
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("length(token_hash) = 64")):
            connection.execute(
                "INSERT INTO sessions "
                "(id, user_id, token_hash, created_at, last_seen_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    SESSION_ID.value,
                    USER_ID.value,
                    short_hash,
                    stamp,
                    stamp,
                    format_timestamp(MOMENT + SESSION_TTL),
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("expires_at > created_at")):
            connection.execute(
                "INSERT INTO sessions "
                "(id, user_id, token_hash, created_at, last_seen_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (OTHER_SESSION_ID.value, USER_ID.value, TOKEN_HASH, stamp, stamp, stamp, None),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match=re.escape("status IN ('draft', 'published')")
        ):
            connection.execute(
                "INSERT INTO profiles "
                "(id, user_id, public_id, status, content, published_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    PROFILE_ID.value,
                    USER_ID.value,
                    "p_" + "1" * 16,
                    "archived",
                    "",
                    None,
                    stamp,
                    stamp,
                ),
            )
    finally:
        connection.close()


def test_deleting_a_user_cascades_to_the_child_tables(db_path: Path) -> None:
    """`users` の直接削除で3つの子テーブルが CASCADE 削除されること。

    管理者の直接DB削除（design/08-database.md §9 共通規約）が、資格情報や Session を
    孤児として残さないことが前提。`foreign_keys=ON` の接続で削除する。
    """
    seed_user(db_path, USER_ID)
    seed_user(db_path, OTHER_USER_ID)
    save_session(db_path, make_active_session(SESSION_ID, TOKEN_HASH))

    writer = connect(db_path)
    try:
        writer.execute("DELETE FROM users WHERE id = ?", (USER_ID.value,))
    finally:
        writer.close()

    counts = count_rows(db_path)
    assert counts["users"] == 1
    assert counts["login_credentials"] == 1
    assert counts["recovery_credentials"] == 1
    assert counts["sessions"] == 0


def test_partial_index_serves_the_active_session_lookup(db_path: Path) -> None:
    """部分一意索引が存在し、未失効 Session の検索で使われること。

    `ix_sessions_user_active ON sessions(user_id) WHERE revoked_at IS NULL` は
    `WHERE user_id = ? AND revoked_at IS NULL` と一致するときだけ使われる。索引が
    無ければ `revoke_all_for_user` 前後の検索が毎回全表走査になる。
    """
    seed_user(db_path, USER_ID)
    save_session(db_path, make_active_session(SESSION_ID, TOKEN_HASH))

    connection = connect(db_path)
    try:
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ix_sessions_user_active",),
        ).fetchone()
        assert index is not None
        assert "WHERE revoked_at IS NULL" in index["sql"]

        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM sessions WHERE user_id = ? AND revoked_at IS NULL",
            (USER_ID.value,),
        ).fetchall()
    finally:
        connection.close()

    details = " ".join(row["detail"] for row in plan)
    assert "ix_sessions_user_active" in details


def test_a_write_that_fails_mid_transaction_is_rolled_back(db_path: Path) -> None:
    """`UnitOfWork` 内で途中失敗すると、先行する書き込みごと rollback されること。

    1回目の `save` は成功し、2回目が一意制約で失敗する。`UnitOfWork` が ROLLBACK する
    ため、1回目が書いた `users` / 資格情報も残ってはならない（原子性）。
    """
    shared_login = fake_hash("shared_login_id")

    with pytest.raises(sqlite3.IntegrityError):
        with UnitOfWork(connect(db_path)) as uow:
            repository = SqlAccountRepository(uow.connection)
            repository.save(make_account(user_id=USER_ID, now=MOMENT, login_id_hash=shared_login))
            repository.save(
                make_account(user_id=OTHER_USER_ID, now=MOMENT, login_id_hash=shared_login)
            )

    assert count_rows(db_path) == {name: 0 for name in CHECK_TABLES}


def test_uncommitted_writes_are_invisible_to_another_connection(db_path: Path) -> None:
    """未コミットの書き込みが他接続から見えず、COMMIT 後に見えること。

    接続はリクエスト単位で、書き込みは `BEGIN IMMEDIATE` から COMMIT まで
    他接続から見えない（design/10-infra-ops.md §11）。
    """
    with UnitOfWork(connect(db_path)) as uow:
        SqlAccountRepository(uow.connection).save(make_account(user_id=USER_ID, now=MOMENT))
        assert count_rows(db_path)["users"] == 0
        assert uow.connection.in_transaction is True

    assert count_rows(db_path)["users"] == 1


def test_sql_fragments_are_treated_as_values(db_path: Path) -> None:
    """SQL断片を値として渡しても文として実行されないこと（全SQL parameterized）。

    引用符を含む64文字の `login_id_hash` を保存し、断片そのものでは一致しないことを
    見る。文字列連結でSQLを組み立てていれば `' OR '1'='1` が条件を真にして行を返す。
    """
    fragment = "' OR '1'='1"
    weird_hash = (fragment + "0" * 64)[:64]
    save_account(db_path, make_account(user_id=USER_ID, now=MOMENT, login_id_hash=weird_hash))

    found = find_by_login_id_hash(db_path, weird_hash)
    assert found is not None
    assert found.login_credential.login_id_hash == weird_hash
    assert find_by_login_id_hash(db_path, fragment) is None
    assert count_rows(db_path)["login_credentials"] == 1
