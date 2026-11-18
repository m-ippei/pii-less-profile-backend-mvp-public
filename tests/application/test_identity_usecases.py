"""Identity application 層のユースケースのテスト（design/04-use-cases.md §5）。

固定すること:

1. 5ユースケースが存在し、`design/04-use-cases.md` §5 の transaction 記載順で動く。
2. `login_id` 不在でも `PasswordHasher.dummy_verify` を1回実行する（処理時間の揃い。AC-5）。
3. `ChangePassword` / `RecoverPassword` で全Sessionが失効し、`recovery_key` が
   ローテーションする（旧値は無効）。
4. `DeleteCurrentSession` は冪等。
5. 途中失敗で transaction が rollback する（原子性）。
6. 時刻は `FakeClock` で固定する。
7. Argon2（`hash` / `verify` / `dummy_verify`）は transaction の外で実行し、検証失敗時は
   transaction を開かない（D-37）。
8. `ChangePassword` は検証〜更新の間の `password_hash` 変化を transaction 内で検知して
   拒否する（Q2=(a)・H-14）。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.identity.application.errors import (
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidRecoveryCredentialError,
    InvalidSessionError,
    ValidationError,
)
from src.identity.application.usecases import (
    AuthenticateSession,
    ChangePassword,
    CreateSession,
    DeleteCurrentSession,
    IdentityDependencies,
    RecoverPassword,
)
from src.identity.domain.entities import (
    SESSION_TTL,
    Account,
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.value_objects import UserId
from src.shared.security.security import BASE58_ALPHABET, IdGenerator, SecretGenerator, SecretHasher
from tests.support.fake_clock import FakeClock
from tests.support.in_memory_identity import (
    FakePasswordHasher,
    FakeUnitOfWork,
    InMemoryAccountRepository,
    InMemorySessionRepository,
)

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
LOGIN_ID = "1" * 24
RECOVERY_KEY = "2" * 32
PASSWORD = "correct horse battery"
NEW_PASSWORD = "staple new battery"
OLD_TOKEN = "A" * 32
PEPPER = b"test-pepper-" + b"x" * 32
SECRET_HASHER = SecretHasher(PEPPER)


class _FailingSessionRepository(InMemorySessionRepository):
    """全Session失効で失敗させ、原子性（rollback）を観測するためのダブル。"""

    def revoke_all_for_user(self, user_id: UserId, now: datetime) -> None:
        raise RuntimeError("forced failure to observe rollback")


class _ChangedPasswordRepository(InMemoryAccountRepository):
    """2回目の `find_by_id`（transaction 内の再読）直前に `password_hash` を差し替える。

    検証〜更新の間に対象が変化した状況を模し、`ChangePassword` の変化検知（Q2=(a)）を
    観測するためのダブル。
    """

    def __init__(self, changed_password_hash: str, log: list[str] | None = None) -> None:
        super().__init__(log)
        self._changed_password_hash = changed_password_hash
        self._find_by_id_calls = 0

    def find_by_id(self, user_id: UserId) -> Account | None:
        self._find_by_id_calls += 1
        if self._find_by_id_calls == 2:
            account = self._accounts.get(user_id.value)
            assert account is not None
            account.login_credential.password_hash = self._changed_password_hash
        return super().find_by_id(user_id)


@dataclass
class Harness:
    deps: IdentityDependencies
    accounts: InMemoryAccountRepository
    sessions: InMemorySessionRepository
    hasher: FakePasswordHasher
    clock: FakeClock
    uow: FakeUnitOfWork
    log: list[str]
    events: list[str]
    create_session: CreateSession
    delete_session: DeleteCurrentSession
    authenticate: AuthenticateSession
    change_password: ChangePassword
    recover_password: RecoverPassword


def _harness(
    *,
    log: list[str] | None = None,
    session_repository: InMemorySessionRepository | None = None,
    account_repository: InMemoryAccountRepository | None = None,
    events: list[str] | None = None,
) -> Harness:
    log = log if log is not None else []
    events = events if events is not None else []
    accounts = (
        account_repository if account_repository is not None else InMemoryAccountRepository(log)
    )
    sessions = (
        session_repository if session_repository is not None else InMemorySessionRepository(log)
    )
    hasher = FakePasswordHasher(events)
    clock = FakeClock(START)
    uow = FakeUnitOfWork(accounts, sessions, events=events)
    deps = IdentityDependencies(
        account_repository=accounts,
        session_repository=sessions,
        password_hasher=hasher,
        secret_hasher=SECRET_HASHER,
        secret_generator=SecretGenerator(),
        id_generator=IdGenerator(),
        clock=clock,
        unit_of_work=uow,
    )
    return Harness(
        deps=deps,
        accounts=accounts,
        sessions=sessions,
        hasher=hasher,
        clock=clock,
        uow=uow,
        log=log,
        events=events,
        create_session=CreateSession(deps),
        delete_session=DeleteCurrentSession(deps),
        authenticate=AuthenticateSession(deps),
        change_password=ChangePassword(deps),
        recover_password=RecoverPassword(deps),
    )


def _account(hasher: FakePasswordHasher, *, now: datetime = START) -> Account:
    return Account(
        user=User(id=USER_ID, created_at=now, updated_at=now),
        login_credential=LoginCredential.issue(
            user_id=USER_ID,
            login_id_hash=SECRET_HASHER.hash("login_id", LOGIN_ID),
            password_hash=hasher.hash(PASSWORD),
            now=now,
        ),
        recovery_credential=RecoveryCredential.issue(
            user_id=USER_ID,
            recovery_key_hash=SECRET_HASHER.hash("recovery_key", RECOVERY_KEY),
            now=now,
        ),
    )


def _session(token: str, *, now: datetime = START) -> Session:
    return Session.create(
        id=UserId(IdGenerator().generate()),
        user_id=USER_ID,
        token_hash=SECRET_HASHER.hash("session", token),
        now=now,
    )


def test_create_session_issues_token_and_principal() -> None:
    """正しい資格情報で新Sessionが作られ、生tokenと `Principal` が返ること。"""
    h = _harness()
    h.accounts.add(_account(h.hasher))

    issued = h.create_session.execute(login_id=LOGIN_ID, password=PASSWORD)

    assert issued.principal.user_id == USER_ID.value
    assert issued.principal.method == "password"
    stored = h.sessions.find_by_token_hash(SECRET_HASHER.hash("session", issued.token))
    assert stored is not None
    assert stored.user_id == USER_ID
    assert stored.created_at == START
    assert stored.expires_at == START + SESSION_TTL
    assert h.uow.committed == 1


def test_create_session_unknown_login_id_runs_dummy_verify_once() -> None:
    """`login_id` 不在ではダミーArgon2検証を1回だけ実行し、Sessionを作らないこと。"""
    h = _harness()

    with pytest.raises(InvalidCredentialsError):
        h.create_session.execute(login_id=LOGIN_ID, password=PASSWORD)

    assert h.hasher.dummy_calls == 1
    assert h.sessions.all() == []


def test_create_session_wrong_password_does_not_dummy_verify() -> None:
    """`login_id` は存在するが password 誤りの時はダミー検証をしないこと。

    どちらも H-3 / H-4 の同一応答だが、処理時間の揃えは「該当なし」の時だけ必要。
    """
    h = _harness()
    h.accounts.add(_account(h.hasher))

    with pytest.raises(InvalidCredentialsError):
        h.create_session.execute(login_id=LOGIN_ID, password="wrong password here")

    assert h.hasher.dummy_calls == 0
    assert h.sessions.all() == []


def test_create_session_revokes_presented_valid_session() -> None:
    """有効な旧Sessionが提示されたら失効させ、新Sessionを発行すること。"""
    h = _harness()
    h.accounts.add(_account(h.hasher))
    h.sessions.add(_session(OLD_TOKEN))

    issued = h.create_session.execute(
        login_id=LOGIN_ID, password=PASSWORD, current_session_token=OLD_TOKEN
    )

    old = h.sessions.find_by_token_hash(SECRET_HASHER.hash("session", OLD_TOKEN))
    assert old is not None
    assert old.revoked_at == START
    assert len(h.sessions.all()) == 2
    assert issued.token != OLD_TOKEN


def test_delete_current_session_revokes_once_and_is_idempotent() -> None:
    """現Sessionを一度だけ失効させ、2回目以降は失敗しない（冪等）こと。"""
    h = _harness()
    h.sessions.add(_session(OLD_TOKEN))

    h.delete_session.execute(token=OLD_TOKEN)
    revoked = h.sessions.find_by_token_hash(SECRET_HASHER.hash("session", OLD_TOKEN))
    assert revoked is not None
    assert revoked.revoked_at == START

    # 2回目・存在しないtoken・token無しでも失敗しない。
    h.delete_session.execute(token=OLD_TOKEN)
    h.delete_session.execute(token="B" * 32)
    h.delete_session.execute(token=None)
    assert revoked.revoked_at == START


def test_authenticate_session_returns_principal_and_touches_after_5_minutes() -> None:
    """有効Sessionから `Principal` を作り、5分経過後は `last_seen_at` を更新すること。"""
    h = _harness()
    h.sessions.add(_session(OLD_TOKEN))
    h.clock.advance(timedelta(minutes=5))

    principal = h.authenticate.execute(token=OLD_TOKEN)

    assert principal.user_id == USER_ID.value
    assert principal.method == "password"
    stored = h.sessions.find_by_token_hash(SECRET_HASHER.hash("session", OLD_TOKEN))
    assert stored is not None
    assert stored.last_seen_at == START + timedelta(minutes=5)
    assert "sessions.touch" in h.log


def test_authenticate_session_does_not_touch_within_5_minutes() -> None:
    """5分未満では `last_seen_at` を更新しないこと。"""
    h = _harness()
    h.sessions.add(_session(OLD_TOKEN))
    h.clock.advance(timedelta(minutes=4))

    h.authenticate.execute(token=OLD_TOKEN)

    stored = h.sessions.find_by_token_hash(SECRET_HASHER.hash("session", OLD_TOKEN))
    assert stored is not None
    assert stored.last_seen_at == START
    assert "sessions.save" not in h.log


@pytest.mark.parametrize("kind", ["unknown", "expired", "revoked"])
def test_authenticate_session_invalid_raises(kind: str) -> None:
    """不明token・期限切れ・失効済みはすべて `InvalidSessionError`（H-5/H-6/H-11）。"""
    h = _harness()
    token = OLD_TOKEN
    if kind != "unknown":
        session = _session(token)
        if kind == "expired":
            h.clock.advance(SESSION_TTL)
        else:
            session.revoke(START)
        h.sessions.add(session)

    with pytest.raises(InvalidSessionError):
        h.authenticate.execute(token=token)


def test_change_password_updates_credentials_rotates_recovery_and_revokes_sessions() -> None:
    """§5 の記載順（資格情報更新 → recovery_credentials更新 → 全Session失効 → 新Session）
    で動き、旧recovery_keyが無効になり新Sessionが有効になること。"""
    h = _harness()
    h.accounts.add(_account(h.hasher))
    h.sessions.add(_session(OLD_TOKEN))
    h.sessions.add(_session("B" * 32))
    h.clock.advance(timedelta(days=1))
    now = START + timedelta(days=1)

    changed = h.change_password.execute(
        user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD
    )

    assert h.log == [
        "account.find_by_id",
        "account.find_by_id",
        "account.save",
        "sessions.revoke_all_for_user",
        "sessions.save",
    ]
    account = h.accounts.all()[0]
    assert account.login_credential.password_changed_at == now
    assert h.hasher.verify(account.login_credential.password_hash, NEW_PASSWORD)
    assert account.recovery_credential.recovery_key_hash == SECRET_HASHER.hash(
        "recovery_key", changed.recovery_key
    )
    assert changed.recovery_key != RECOVERY_KEY
    # 旧recovery_keyはもう一致しない。
    old_recovery_hash = SECRET_HASHER.hash("recovery_key", RECOVERY_KEY)
    assert h.accounts.find_by_recovery_key_hash(old_recovery_hash) is None
    # 旧Sessionは全て失効し、新Sessionだけが有効。
    for session in h.sessions.all():
        if session.token_hash == SECRET_HASHER.hash("session", changed.token):
            assert session.is_valid(now)
        else:
            assert session.revoked_at == now
    assert changed.principal.user_id == USER_ID.value


def test_change_password_wrong_current_password_changes_nothing() -> None:
    """現パスワード誤りは `InvalidCurrentPasswordError`（H-14）で、何も書き換えないこと。

    検証は transaction の外なので、失敗時は `BEGIN IMMEDIATE` を開かない（D-37）。
    """
    h = _harness()
    account = _account(h.hasher)
    h.accounts.add(account)
    original_hash = account.login_credential.password_hash

    with pytest.raises(InvalidCurrentPasswordError):
        h.change_password.execute(
            user_id=USER_ID, current_password="wrong password here", new_password=NEW_PASSWORD
        )

    assert h.accounts.all()[0].login_credential.password_hash == original_hash
    assert h.sessions.all() == []
    assert h.uow.entered == 0
    assert h.uow.rolled_back == 0


def test_change_password_rejects_same_password() -> None:
    """現パスワードと同一の新パスワードを拒否すること（§4 Password 規則）。

    同一判定も transaction の外で行うため、失敗時は transaction を開かない。
    """
    h = _harness()
    h.accounts.add(_account(h.hasher))

    with pytest.raises(ValidationError):
        h.change_password.execute(user_id=USER_ID, current_password=PASSWORD, new_password=PASSWORD)

    assert h.uow.entered == 0


def test_recover_password_rotates_login_and_recovery_and_revokes_sessions() -> None:
    """§5 の記載順（特定 → login_credentials更新 → recovery_credentials更新 → 全Session失効）
    で動き、旧login_id・旧recovery_keyが無効になり、Sessionは発行しないこと。"""
    h = _harness()
    h.accounts.add(_account(h.hasher))
    h.sessions.add(_session(OLD_TOKEN))
    h.clock.advance(timedelta(days=1))
    now = START + timedelta(days=1)

    recovered = h.recover_password.execute(recovery_key=RECOVERY_KEY, new_password=NEW_PASSWORD)

    assert h.log == [
        "account.find_by_recovery_key_hash",
        "account.save",
        "sessions.revoke_all_for_user",
    ]
    assert len(recovered.login_id) == 24
    assert all(c in BASE58_ALPHABET for c in recovered.login_id)
    assert len(recovered.recovery_key) == 32
    assert all(c in BASE58_ALPHABET for c in recovered.recovery_key)
    account = h.accounts.all()[0]
    assert account.login_credential.login_id_hash == SECRET_HASHER.hash(
        "login_id", recovered.login_id
    )
    assert account.login_credential.password_changed_at == now
    assert h.hasher.verify(account.login_credential.password_hash, NEW_PASSWORD)
    assert (
        h.accounts.find_by_recovery_key_hash(SECRET_HASHER.hash("recovery_key", RECOVERY_KEY))
        is None
    )
    assert account.recovery_credential.recovery_key_hash == SECRET_HASHER.hash(
        "recovery_key", recovered.recovery_key
    )
    # Sessionは発行せず、既存Sessionは全て失効する。
    assert len(h.sessions.all()) == 1
    assert h.sessions.all()[0].revoked_at == now


def test_recover_password_unknown_key_raises_without_dummy_verify() -> None:
    """不明な `recovery_key` は `InvalidRecoveryCredentialError`（H-7）で、
    ダミーArgon2検証を行わないこと（高エントロピー値のため）。"""
    h = _harness()

    with pytest.raises(InvalidRecoveryCredentialError):
        h.recover_password.execute(recovery_key=RECOVERY_KEY, new_password=NEW_PASSWORD)

    assert h.hasher.dummy_calls == 0


def test_change_password_rolls_back_on_failure() -> None:
    """途中失敗で enter 時の状態へ rollback され、部分的な書き込みが残らないこと。"""
    log: list[str] = []
    failing = _FailingSessionRepository(log)
    h = _harness(log=log, session_repository=failing)
    account = _account(h.hasher)
    h.accounts.add(account)
    original_hash = account.login_credential.password_hash

    with pytest.raises(RuntimeError):
        h.change_password.execute(
            user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD
        )

    assert h.uow.rolled_back == 1
    restored = h.accounts.all()[0]
    assert restored.login_credential.password_hash == original_hash
    assert restored.recovery_credential.recovery_key_hash == SECRET_HASHER.hash(
        "recovery_key", RECOVERY_KEY
    )
    assert h.sessions.all() == []


def test_create_session_verifies_before_beginning_transaction() -> None:
    """Argon2 検証が `BEGIN IMMEDIATE`（transaction）を開く前に実行されること（D-37）。"""
    events: list[str] = []
    h = _harness(events=events)
    h.accounts.add(_account(h.hasher))

    h.create_session.execute(login_id=LOGIN_ID, password=PASSWORD)

    assert "hasher.verify" in events
    assert "uow.enter" in events
    last_hasher = max(i for i, event in enumerate(events) if event.startswith("hasher."))
    assert last_hasher < events.index("uow.enter")


def test_create_session_unknown_login_id_does_not_begin_transaction() -> None:
    """該当なしはダミー検証ののち transaction を開かないこと（検証は外・H-3）。"""
    events: list[str] = []
    h = _harness(events=events)

    with pytest.raises(InvalidCredentialsError):
        h.create_session.execute(login_id=LOGIN_ID, password=PASSWORD)

    assert h.hasher.dummy_calls == 1
    assert "hasher.dummy_verify" in events
    assert "uow.enter" not in events
    assert h.uow.entered == 0


def test_create_session_wrong_password_does_not_begin_transaction() -> None:
    """password 誤りでも transaction を開かないこと（検証は外・H-4）。"""
    h = _harness()
    h.accounts.add(_account(h.hasher))

    with pytest.raises(InvalidCredentialsError):
        h.create_session.execute(login_id=LOGIN_ID, password="wrong password here")

    assert h.uow.entered == 0


def test_change_password_verifies_and_hashes_before_beginning_transaction() -> None:
    """読み・検証・同一判定・ハッシュがすべて transaction の外であること（D-37・Q3=(a)）。"""
    events: list[str] = []
    h = _harness(events=events)
    h.accounts.add(_account(h.hasher))

    h.change_password.execute(user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD)

    enter = events.index("uow.enter")
    assert "hasher.verify" in events
    assert "hasher.hash" in events
    assert all(i < enter for i, event in enumerate(events) if event.startswith("hasher."))


def test_change_password_wrong_current_password_does_not_begin_transaction() -> None:
    """現パスワード誤りは transaction を開く前に `InvalidCurrentPasswordError`（H-14）。"""
    events: list[str] = []
    h = _harness(events=events)
    h.accounts.add(_account(h.hasher))

    with pytest.raises(InvalidCurrentPasswordError):
        h.change_password.execute(
            user_id=USER_ID, current_password="wrong password here", new_password=NEW_PASSWORD
        )

    assert h.uow.entered == 0
    assert "uow.enter" not in events


def test_change_password_rejects_password_changed_since_verification() -> None:
    """検証〜更新の間で `password_hash` が変化したら拒否すること（Q2=(a)・H-14）。

    別経路の更新を模して検証に使ったハッシュを差し替え、transaction 内の再読で
    一致しないことを検知して `InvalidCurrentPasswordError` になることを観測する。
    """
    changed = _ChangedPasswordRepository("fake$rotated elsewhere")
    h = _harness(account_repository=changed)
    h.accounts.add(_account(h.hasher))

    with pytest.raises(InvalidCurrentPasswordError):
        h.change_password.execute(
            user_id=USER_ID, current_password=PASSWORD, new_password=NEW_PASSWORD
        )

    assert h.uow.entered == 1
    assert h.uow.rolled_back == 1
    assert h.sessions.all() == []


def test_recover_password_hashes_before_beginning_transaction() -> None:
    """回復の Argon2 ハッシュが transaction の外であること（D-37）。"""
    events: list[str] = []
    h = _harness(events=events)
    h.accounts.add(_account(h.hasher))

    h.recover_password.execute(recovery_key=RECOVERY_KEY, new_password=NEW_PASSWORD)

    enter = events.index("uow.enter")
    assert "hasher.hash" in events
    assert all(i < enter for i, event in enumerate(events) if event.startswith("hasher."))
