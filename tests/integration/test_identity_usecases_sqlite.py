"""実 Repository + 実 `UnitOfWork` + 実 SQLite で Identity の5ユースケースを通す結合テスト。

M03 の application テストは InMemory ダブル越しだったため、実 SQL 上で同じ結果になる保証が
無かった。ここでは一時ディレクトリの migration 適用済み実SQLiteファイルに、実際の
`SqlAccountRepository` / `SqlSessionRepository` / `UnitOfWork` を注入して5ユースケースを
成立させ、DB に実際に保存された内容を直接読む。

固定すること:

1. CreateSession / AuthenticateSession / DeleteCurrentSession / ChangePassword /
   RecoverPassword が実 SQL 上で成立する（design/04-use-cases.md §5）。
2. パスワード変更・回復で資格情報のハッシュが差し替わり、旧値では取得できない。
3. パスワード変更・回復で全 Session が失効し、変更後の新 Session だけが有効、回復は
   Session を発行しない。
4. DB ダンプ（全テーブル）に平文の `login_id`・パスワード・`recovery_key`・token が無く、
   `login_credentials.password_hash` は Argon2id エンコードのみ（design/07-security.md §8）。
5. 途中失敗した transaction が DB に痕跡を残さない（原子性）。

根拠: design/04-use-cases.md §5 / design/07-security.md §8 / design/08-database.md §9 /
design/11-testing.md §12。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.identity.application.errors import InvalidCredentialsError, InvalidSessionError
from src.identity.application.usecases import (
    AuthenticateSession,
    ChangePassword,
    CreateSession,
    DeleteCurrentSession,
    IdentityDependencies,
    RecoverPassword,
)
from src.identity.domain.entities import (
    Account,
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.value_objects import UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.identity.infrastructure.session_repository import SqlSessionRepository
from src.shared.db import UnitOfWork, connect
from src.shared.security.security import IdGenerator, PasswordHasher, SecretGenerator, SecretHasher
from tests.support.fake_clock import FakeClock
from tests.support.sqlite_identity import migrated_db_path

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
LOGIN_ID = "1" * 24
RECOVERY_KEY = "2" * 32
PASSWORD = "correct horse battery"
NEW_PASSWORD = "staple new battery"
PEPPER = b"test-pepper-" + b"x" * 32

START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
TABLES = ("users", "login_credentials", "recovery_credentials", "sessions", "profiles")


class FailingRevokeSessionRepository(SqlSessionRepository):
    """`revoke_all_for_user` だけを失敗させ、実 DB 上で rollback を観測するための差し替え。"""

    def revoke_all_for_user(self, user_id: UserId, now: datetime) -> None:
        raise RuntimeError("forced failure to observe rollback")


@dataclass
class Harness:
    """実 SQLite に実 Repository を結線してユースケースを実行するための足場。

    `deps()` は呼ぶたびに新しい接続を作って Repository と `UnitOfWork` を同じ接続へ束ねる
    （接続はリクエスト単位。design/10-infra-ops.md §11）。1ユースケース = 1 transaction。
    """

    db_path: Path
    clock: FakeClock
    secret_hasher: SecretHasher
    password_hasher: PasswordHasher
    secret_generator: SecretGenerator = field(default_factory=SecretGenerator)
    id_generator: IdGenerator = field(default_factory=IdGenerator)

    def deps(
        self,
        *,
        session_repository_cls: type[SqlSessionRepository] = SqlSessionRepository,
    ) -> IdentityDependencies:
        connection = connect(self.db_path)
        return IdentityDependencies(
            account_repository=SqlAccountRepository(connection),
            session_repository=session_repository_cls(connection),
            password_hasher=self.password_hasher,
            secret_hasher=self.secret_hasher,
            secret_generator=self.secret_generator,
            id_generator=self.id_generator,
            clock=self.clock,
            unit_of_work=UnitOfWork(connection),
        )

    def account(self, *, now: datetime = START) -> Account:
        """既知の平文に対応する実ハッシュを持つ Account を組み立てる。"""
        return Account(
            user=User(id=USER_ID, created_at=now, updated_at=now),
            login_credential=LoginCredential.issue(
                user_id=USER_ID,
                login_id_hash=self.secret_hasher.hash("login_id", LOGIN_ID),
                password_hash=self.password_hasher.hash(PASSWORD),
                now=now,
            ),
            recovery_credential=RecoveryCredential.issue(
                user_id=USER_ID,
                recovery_key_hash=self.secret_hasher.hash("recovery_key", RECOVERY_KEY),
                now=now,
            ),
        )

    def seed_account(self) -> Account:
        account = self.account()
        connection = connect(self.db_path)
        with UnitOfWork(connection) as uow:
            SqlAccountRepository(uow.connection).save(account)
        return account

    def seed_session(self, token: str, *, now: datetime = START) -> Session:
        session = Session.create(
            id=UserId(self.id_generator.generate()),
            user_id=USER_ID,
            token_hash=self.secret_hasher.hash("session", token),
            now=now,
        )
        connection = connect(self.db_path)
        with UnitOfWork(connection) as uow:
            SqlSessionRepository(uow.connection).save(session)
        return session

    def find_account_by_login(self, login_id: str) -> Account | None:
        connection = connect(self.db_path)
        try:
            repository = SqlAccountRepository(connection)
            return repository.find_by_login_id_hash(self.secret_hasher.hash("login_id", login_id))
        finally:
            connection.close()

    def find_account_by_recovery(self, recovery_key: str) -> Account | None:
        connection = connect(self.db_path)
        try:
            repository = SqlAccountRepository(connection)
            return repository.find_by_recovery_key_hash(
                self.secret_hasher.hash("recovery_key", recovery_key)
            )
        finally:
            connection.close()

    def find_session(self, token: str) -> Session | None:
        connection = connect(self.db_path)
        try:
            repository = SqlSessionRepository(connection)
            return repository.find_by_token_hash(self.secret_hasher.hash("session", token))
        finally:
            connection.close()

    def session_count(self) -> int:
        connection = connect(self.db_path)
        try:
            return connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
        finally:
            connection.close()


def dump_database(db_path: Path) -> str:
    """全テーブルの全列を1つの文字列へ（平文秘密値の走査用。design/07-security.md §8 検証）。"""
    connection = connect(db_path)
    try:
        lines: list[str] = []
        for table in TABLES:
            for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                cells = ("" if value is None else str(value) for value in tuple(row))
                lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        connection.close()


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    # 実 Argon2id のエンコードを保ちつつ、テスト用に軽量パラメータへ落とす。
    monkeypatch.setenv("ARGON2_TIME_COST", "1")
    monkeypatch.setenv("ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("ARGON2_PARALLELISM", "1")
    return Harness(
        db_path=migrated_db_path(tmp_path),
        clock=FakeClock(START),
        secret_hasher=SecretHasher(PEPPER),
        password_hasher=PasswordHasher(),
    )


def test_create_session_and_authenticate_round_trip(harness: Harness) -> None:
    """ログインで Session が出来、その token で認証でき、5分後に touch されること。"""
    harness.seed_account()

    issued = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)

    assert issued.principal.user_id == USER_ID.value
    assert issued.principal.method == "password"
    stored = harness.find_session(issued.token)
    assert stored is not None
    assert stored.token_hash == harness.secret_hasher.hash("session", issued.token)
    assert stored.user_id == USER_ID

    harness.clock.advance(timedelta(minutes=5))
    principal = AuthenticateSession(harness.deps()).execute(token=issued.token)

    assert principal.user_id == USER_ID.value
    refreshed = harness.find_session(issued.token)
    assert refreshed is not None
    assert refreshed.last_seen_at == START + timedelta(minutes=5)


def test_create_session_wrong_password_leaves_no_session(harness: Harness) -> None:
    """パスワード誤りは `InvalidCredentialsError` で、Session を作らず Account も不変。"""
    harness.seed_account()

    with pytest.raises(InvalidCredentialsError):
        CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password="wrong password here")

    assert harness.session_count() == 0
    account = harness.find_account_by_login(LOGIN_ID)
    assert account is not None
    assert harness.password_hasher.verify(account.login_credential.password_hash, PASSWORD)


def test_delete_current_session_revokes_once_and_is_idempotent(harness: Harness) -> None:
    """現 Session を一度だけ失効させ、2回目以降は失敗しない（冪等）。"""
    harness.seed_account()
    issued = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)

    DeleteCurrentSession(harness.deps()).execute(token=issued.token)
    revoked = harness.find_session(issued.token)
    assert revoked is not None
    assert revoked.revoked_at == START

    DeleteCurrentSession(harness.deps()).execute(token=issued.token)
    again = harness.find_session(issued.token)
    assert again is not None
    assert again.revoked_at == START

    with pytest.raises(InvalidSessionError):
        AuthenticateSession(harness.deps()).execute(token=issued.token)


def test_change_password_rotates_recovery_and_revokes_sessions(harness: Harness) -> None:
    """パスワード変更で recovery_key がローテーションし、旧 Session が失効すること。

    §5 の transaction 記載順（資格情報更新 → recovery 更新 → 全Session失効 → 新Session）。
    """
    harness.seed_account()
    old = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)
    harness.clock.advance(timedelta(days=1))
    now = START + timedelta(days=1)

    changed = ChangePassword(harness.deps()).execute(
        user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD
    )

    assert changed.recovery_key != RECOVERY_KEY
    assert harness.find_account_by_recovery(RECOVERY_KEY) is None
    account = harness.find_account_by_login(LOGIN_ID)
    assert account is not None
    assert account.recovery_credential.recovery_key_hash == harness.secret_hasher.hash(
        "recovery_key", changed.recovery_key
    )
    assert harness.password_hasher.verify(account.login_credential.password_hash, NEW_PASSWORD)

    old_stored = harness.find_session(old.token)
    assert old_stored is not None
    assert old_stored.revoked_at == now
    new_stored = harness.find_session(changed.token)
    assert new_stored is not None
    assert new_stored.is_valid(now)

    with pytest.raises(InvalidSessionError):
        AuthenticateSession(harness.deps()).execute(token=old.token)
    relogin = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=NEW_PASSWORD)
    assert relogin.principal.user_id == USER_ID.value


def test_recover_password_reissues_and_revokes_sessions(harness: Harness) -> None:
    """回復で login_id と recovery_key が再発行され、全 Session が失効し新 Session は出ない。"""
    harness.seed_account()
    old = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)
    harness.clock.advance(timedelta(days=1))
    now = START + timedelta(days=1)

    recovered = RecoverPassword(harness.deps()).execute(
        recovery_key=RECOVERY_KEY, new_password=NEW_PASSWORD
    )

    assert recovered.login_id != LOGIN_ID
    assert len(recovered.login_id) == 24
    assert recovered.recovery_key != RECOVERY_KEY
    assert len(recovered.recovery_key) == 32

    assert harness.find_account_by_login(LOGIN_ID) is None
    assert harness.find_account_by_recovery(RECOVERY_KEY) is None
    account = harness.find_account_by_login(recovered.login_id)
    assert account is not None
    assert account.recovery_credential.recovery_key_hash == harness.secret_hasher.hash(
        "recovery_key", recovered.recovery_key
    )

    old_stored = harness.find_session(old.token)
    assert old_stored is not None
    assert old_stored.revoked_at == now
    assert harness.session_count() == 1

    relogin = CreateSession(harness.deps()).execute(
        login_id=recovered.login_id, password=NEW_PASSWORD
    )
    assert relogin.principal.user_id == USER_ID.value


def test_plaintext_secrets_are_absent_from_the_database(harness: Harness) -> None:
    """DB ダンプに平文の秘密値が無く、パスワードは Argon2id エンコードのみであること。

    不変規則: 生 password・`login_id`・session token・`recovery_key` を DB に出さない
    （design/07-security.md §8 / AC-8）。保存されるのは HMAC-HEX64 と Argon2id だけ。
    """
    harness.seed_account()
    issued = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)

    dump = dump_database(harness.db_path)

    assert LOGIN_ID not in dump
    assert PASSWORD not in dump
    assert RECOVERY_KEY not in dump
    assert issued.token not in dump

    account = harness.find_account_by_login(LOGIN_ID)
    assert account is not None
    assert account.login_credential.login_id_hash == harness.secret_hasher.hash(
        "login_id", LOGIN_ID
    )
    assert account.login_credential.password_hash.startswith("$argon2id$")
    assert account.recovery_credential.recovery_key_hash == harness.secret_hasher.hash(
        "recovery_key", RECOVERY_KEY
    )


def test_a_failed_transaction_leaves_no_trace(harness: Harness) -> None:
    """途中失敗で資格情報・Session の変更が DB に残らないこと（原子性）。

    `ChangePassword` は Account を保存した後に全 Session を失効させる。失効で失敗しても
    `UnitOfWork` が ROLLBACK するため、変更途中の Account も新 Session も残ってはならない。
    """
    harness.seed_account()
    old = CreateSession(harness.deps()).execute(login_id=LOGIN_ID, password=PASSWORD)
    before = harness.find_account_by_login(LOGIN_ID)
    assert before is not None
    before_password_hash = before.login_credential.password_hash
    before_recovery_hash = before.recovery_credential.recovery_key_hash
    harness.clock.advance(timedelta(days=1))

    deps = harness.deps(session_repository_cls=FailingRevokeSessionRepository)
    with pytest.raises(RuntimeError):
        ChangePassword(deps).execute(
            user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD
        )

    after = harness.find_account_by_login(LOGIN_ID)
    assert after is not None
    assert after.login_credential.password_hash == before_password_hash
    assert after.recovery_credential.recovery_key_hash == before_recovery_hash
    stored = harness.find_session(old.token)
    assert stored is not None
    assert stored.revoked_at is None
    assert harness.session_count() == 1
