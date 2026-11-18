"""`SqlAccountRepository` のテスト（一時ディレクトリの実 SQLite ファイル）。

このファイルが守るべき設計上の約束は次の6点。

1. `AccountRepository` の3経路（`user_id` / `login_id_hash` / `recovery_key_hash`）が
   同じ Aggregate を返す（design/03-domain-model.md §4 Port 表）。
2. `save` は新規作成と、既存行の更新（資格情報の差し替え）の両方に対応する。
   `recovery_credentials` は常に1行だけを維持する。
3. `created_at` は作り直さない。`updated_at` は Entity が持つ値だけを書く
   （design/03-domain-model.md §4 時刻の意味）。
4. 時刻は27文字の固定長ISO 8601で保存し、マイクロ秒まで含めて aware datetime として
   読み戻せる（design/08-database.md §9 共通規約）。
5. 全SQLは parameterized。SQL断片を値として渡してもSQLとして実行されない。
6. 生の秘密値は保存されない。`login_id_hash` / `recovery_key_hash` / `password_hash` だけ。

根拠: design/03-domain-model.md §4 / design/08-database.md §9 / design/11-testing.md §12。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.identity.domain.entities import Account
from src.identity.domain.ports import AccountRepository
from src.identity.domain.value_objects import UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.shared.clock.clock import format_timestamp
from src.shared.db import UnitOfWork, connect
from tests.support.sqlite_identity import (
    FAKE_PASSWORD_HASH,
    fake_hash,
    make_account,
    migrated_db_path,
)

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
OTHER_USER_ID = UserId("018f3c2a-7b1c-7abd-8def-0123456789ac")

# マイクロ秒が丸められる formatter を見逃さないよう、意図的に 0 以外を使う。
MOMENT = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=1)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return migrated_db_path(tmp_path)


@pytest.fixture()
def account() -> Account:
    return make_account(user_id=USER_ID, now=MOMENT)


def save(db_path: Path, account: Account) -> None:
    """ユースケースと同じ形で `UnitOfWork` の中で保存する。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlAccountRepository(uow.connection).save(account)


def find_by_id(db_path: Path, user_id: UserId) -> Account | None:
    """取得も `UnitOfWork` 1回で行う（接続はリクエスト単位、design/10-infra-ops.md §11）。"""
    with UnitOfWork(connect(db_path)) as uow:
        return SqlAccountRepository(uow.connection).find_by_id(user_id)


def find_by_login_id_hash(db_path: Path, login_id_hash: str) -> Account | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlAccountRepository(uow.connection).find_by_login_id_hash(login_id_hash)


def find_by_recovery_key_hash(db_path: Path, recovery_key_hash: str) -> Account | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlAccountRepository(uow.connection).find_by_recovery_key_hash(recovery_key_hash)


def test_save_writes_the_three_tables_and_find_by_id_reads_them_back(
    db_path: Path, account: Account
) -> None:
    """新規 Account が3テーブルに保存され、Aggregate として読み戻せること。

    `Account` は User + LoginCredential + RecoveryCredential の3点セット
    （design/03-domain-model.md §4）なので、行が欠けたまま「保存できた」にしない。
    """
    save(db_path, account)

    loaded = find_by_id(db_path, USER_ID)

    assert loaded is not None
    assert loaded.user.id == USER_ID
    assert loaded.user.created_at == MOMENT
    assert loaded.login_credential.login_id_hash == fake_hash(f"login_id:{USER_ID.value}")
    assert loaded.login_credential.password_hash == FAKE_PASSWORD_HASH
    assert loaded.login_credential.password_changed_at == MOMENT
    assert loaded.recovery_credential.recovery_key_hash == fake_hash(
        f"recovery_key:{USER_ID.value}"
    )

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert reader.execute("SELECT count(*) FROM login_credentials").fetchone()[0] == 1
        assert reader.execute("SELECT count(*) FROM recovery_credentials").fetchone()[0] == 1
    finally:
        reader.close()


def test_all_three_lookups_return_the_same_account(db_path: Path, account: Account) -> None:
    """3経路のどれで引いても同じ Aggregate が返ること。

    ログインは `login_id_hash`、回復は `recovery_key_hash`、本人操作は `user_id` で引く
    （design/08-database.md §9 主要クエリ）。どれか1つが別の Aggregate や `None` を返す
    と認証・回復が成立しなくなる。
    """
    save(db_path, account)
    expected = make_account(user_id=USER_ID, now=MOMENT)
    login_id_hash = expected.login_credential.login_id_hash
    recovery_key_hash = expected.recovery_credential.recovery_key_hash

    assert find_by_id(db_path, USER_ID) == expected
    assert find_by_login_id_hash(db_path, login_id_hash) == expected
    assert find_by_recovery_key_hash(db_path, recovery_key_hash) == expected


def test_all_three_lookups_return_none_when_nothing_matches(db_path: Path) -> None:
    """該当なしは3経路とも `None`。0行や例外を「不在」として返さないこと。

    0行を例外にすると `login_id` 不在の分岐（`CreateSession` でダミーArgon2検証を1回
    実行する、H-3）が動かなくなる。
    """
    missing = fake_hash("nothing")

    assert find_by_id(db_path, OTHER_USER_ID) is None
    assert find_by_login_id_hash(db_path, missing) is None
    assert find_by_recovery_key_hash(db_path, missing) is None


def test_save_updates_the_same_rows_and_keeps_created_at(db_path: Path, account: Account) -> None:
    """資格情報を変更して `save` すると同じ行が差し替わり、行が増えないこと。

    `recovery_credentials` は1ユーザー1行で、作成・パスワード変更・回復のたびに同じ行を
    UPDATE する（design/08-database.md §9 設計メモ）。行が増えると
    「常に最新の1件だけが有効」（design/03-domain-model.md §4）が壊れる。
    """
    save(db_path, account)

    account.login_credential.change_password("$argon2id$rotated", LATER)
    account.recovery_credential.rotate(fake_hash("rotated_recovery_key"), LATER)
    save(db_path, account)

    loaded = find_by_id(db_path, USER_ID)

    assert loaded is not None
    assert loaded.login_credential.password_hash == "$argon2id$rotated"
    assert loaded.login_credential.updated_at == LATER
    assert loaded.recovery_credential.recovery_key_hash == fake_hash("rotated_recovery_key")
    # `created_at` は作成時に一度だけ（design/03-domain-model.md §4 時刻の意味）。
    assert loaded.login_credential.created_at == MOMENT
    assert loaded.recovery_credential.created_at == MOMENT

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM login_credentials").fetchone()[0] == 1
        assert reader.execute("SELECT count(*) FROM recovery_credentials").fetchone()[0] == 1
    finally:
        reader.close()


def test_repeated_save_of_an_unchanged_account_leaves_the_row_identical(
    db_path: Path, account: Account
) -> None:
    """変更のない Account を再度 `save` しても値が変わらないこと。

    `updated_at` の更新は Entity の責務（design/03-domain-model.md §4 時刻の意味）。
    Repository は Entity が持つ値だけを書くので、`updated_at` は変わらない。
    """
    save(db_path, account)
    save(db_path, account)

    reader = connect(db_path)
    try:
        rows = reader.execute(
            "SELECT login_id_hash, password_hash, created_at, updated_at FROM login_credentials"
        ).fetchall()
    finally:
        reader.close()

    assert len(rows) == 1
    assert rows[0]["updated_at"] == format_timestamp(MOMENT)
    assert rows[0]["created_at"] == format_timestamp(MOMENT)
    assert rows[0]["password_hash"] == FAKE_PASSWORD_HASH


def test_timestamps_are_stored_as_27_characters_and_round_trip(
    db_path: Path, account: Account
) -> None:
    """時刻が27文字の固定長ISO 8601で保存され、マイクロ秒まで一致すること。

    固定長なので文字列比較が時刻順と一致する（design/08-database.md §9 共通規約）。
    naive な時刻やマイクロ秒の切り捨てが入ると、並び順と等価比較が壊れる。
    """
    save(db_path, account)

    reader = connect(db_path)
    try:
        users = reader.execute("SELECT created_at, updated_at FROM users").fetchone()
        credentials = reader.execute(
            "SELECT password_changed_at, created_at, updated_at FROM login_credentials"
        ).fetchone()
    finally:
        reader.close()

    for text in (users["created_at"], users["updated_at"], credentials["created_at"]):
        assert len(text) == 27
        assert text == format_timestamp(MOMENT)
        assert text.endswith("Z")
    assert credentials["password_changed_at"] == format_timestamp(MOMENT)

    loaded = find_by_id(db_path, USER_ID)

    assert loaded is not None
    assert loaded.user.created_at == MOMENT
    assert loaded.user.created_at.tzinfo is not None
    assert loaded.user.created_at.microsecond == MOMENT.microsecond
    assert loaded.login_credential.password_changed_at == MOMENT


def test_lookups_treat_sql_fragments_as_values(db_path: Path, account: Account) -> None:
    """SQL断片を値として渡してもSQLとして実行されないこと（全SQL parameterized）。

    引数はハッシュなので通常は引用符を含まないが、SQL文字列へ埋め込まれていないことは
    値として扱われていることで確認する。
    """
    save(db_path, account)

    fragment = "' OR '1'='1"
    assert find_by_login_id_hash(db_path, fragment) is None
    assert find_by_recovery_key_hash(db_path, fragment) is None

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        reader.close()


def test_account_missing_a_credential_row_is_not_returned(db_path: Path, account: Account) -> None:
    """資格情報の行が欠けたユーザーは `Account` として返さない（INNER JOIN の帰結）。

    `Account` は3点セットの Aggregate（design/03-domain-model.md §4）なので、片方だけの
    行を返すと下流が `None` 参照になる。`save` は常に3点セットを書くため実際に起こらな
    いが、マッピングが INNER JOIN であることを固定しておく。
    """
    save(db_path, account)

    writer = connect(db_path)
    try:
        writer.execute("DELETE FROM recovery_credentials")
    finally:
        writer.close()

    assert find_by_id(db_path, USER_ID) is None
    assert find_by_login_id_hash(db_path, fake_hash(f"login_id:{USER_ID.value}")) is None


def test_repository_satisfies_the_account_repository_port() -> None:
    """`design/03-domain-model.md` §4 の Port を満たしていること。

    Port は `runtime_checkable` なのでメソッドの形を検証できる。欠けがあっても
    M03 の application テスト（InMemory ダブル）では気づけない。
    """
    repository = SqlAccountRepository(sqlite3.connect(":memory:"))

    assert isinstance(repository, AccountRepository)
