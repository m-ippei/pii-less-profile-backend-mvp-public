"""`CreateProfile`（Context 跨ぎユースケース）のテスト（design/04-use-cases.md §5 / §12）。

このファイルが守るべき設計上の約束は次の5点。

1. `users` → `login_credentials` → `recovery_credentials` → `profiles` → `sessions` を
   1 transaction で作る（design/04-use-cases.md §5 Transaction 表）。Application 層からは
   `account.save` → `profile.save` → `sessions.save` の順として観測できる
   （資格情報3テーブルの順序は `SqlAccountRepository` の責務、M04 でテスト済み）。
2. 初回秘密値（`public_id` / `login_id` / 初期パスワード / `recovery_key` / session token）を
   生値で一度だけ返し、永続化はハッシュのみ（design/07-security.md §8 秘密値と保存）。
3. User・Profile・Session は同じ `user_id` で結ばれる（design/02-architecture.md §3）。
4. 一意制約衝突は当該 transaction を rollback し、全値を再生成して最大5回再試行する。
   尽きたら H-8 相当の内部エラー（500 `INTERNAL_ERROR`）にし、部分行を残さない
   （design/06-errors.md §7 H-8）。
5. 各試行は新しい transaction（新しい `UnitOfWork`）で行う。

根拠: design/04-use-cases.md §5 / design/06-errors.md §7 / design/07-security.md §8。
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.app.create_profile import (
    MAX_UNIQUE_RETRIES,
    CreateProfile,
    CreateProfileDependencies,
)
from src.app.errors import ProfileCreationFailedError
from src.profile.domain.value_objects import ProfileStatus
from src.shared.security.security import IdGenerator, SecretGenerator, SecretHasher
from tests.support.fake_clock import FakeClock
from tests.support.in_memory_identity import (
    FakePasswordHasher,
    FakeUnitOfWork,
    InMemoryAccountRepository,
    InMemorySessionRepository,
)
from tests.support.in_memory_profile import InMemoryProfileRepository

NOW = datetime(2026, 10, 6, tzinfo=UTC)
PEPPER = b"0123456789abcdef0123456789abcdef"
SESSION_TTL = timedelta(days=14)


class CollidingAccountRepository(InMemoryAccountRepository):
    """`save` の最初の `collisions` 回を UNIQUE 衝突として失敗させるダブル。"""

    def __init__(self, collisions: int, log: list[str] | None = None) -> None:
        super().__init__(log)
        self.collisions = collisions

    def save(self, account) -> None:  # type: ignore[no-untyped-def]
        if self.collisions > 0:
            self.collisions -= 1
            raise sqlite3.IntegrityError("UNIQUE constraint failed: login_credentials")
        super().save(account)


class CollidingProfileRepository(InMemoryProfileRepository):
    """`save` の最初の `collisions` 回を UNIQUE 衝突として失敗させるダブル。

    Account の3テーブルは書けた後で失敗するため、rollback の検証に使える。
    """

    def __init__(self, collisions: int, log: list[str] | None = None) -> None:
        super().__init__(log)
        self.collisions = collisions

    def save(self, profile) -> None:  # type: ignore[no-untyped-def]
        if self.collisions > 0:
            self.collisions -= 1
            raise sqlite3.IntegrityError("UNIQUE constraint failed: profiles.public_id")
        super().save(profile)


def build_factory(
    *,
    account: InMemoryAccountRepository,
    profile: InMemoryProfileRepository,
    session: InMemorySessionRepository,
    uows: list[FakeUnitOfWork] | None = None,
) -> tuple[CreateProfile, SecretHasher, FakePasswordHasher]:
    """毎回新しい `UnitOfWork` を返す依存ファクトリを組み立てる。"""
    secret_hasher = SecretHasher(PEPPER)
    password_hasher = FakePasswordHasher()

    def factory() -> CreateProfileDependencies:
        unit_of_work = FakeUnitOfWork(account, profile, session)
        if uows is not None:
            uows.append(unit_of_work)
        return CreateProfileDependencies(
            account_repository=account,
            profile_repository=profile,
            session_repository=session,
            password_hasher=password_hasher,
            secret_hasher=secret_hasher,
            secret_generator=SecretGenerator(),
            id_generator=IdGenerator(),
            clock=FakeClock(NOW),
            unit_of_work=unit_of_work,
        )

    return CreateProfile(factory), secret_hasher, password_hasher


def test_execute_writes_account_profile_and_session_in_order() -> None:
    """3つの保存が account → profile → sessions の順で呼ばれること。"""
    log: list[str] = []
    account = InMemoryAccountRepository(log)
    profile = InMemoryProfileRepository(log)
    session = InMemorySessionRepository(log)
    usecase, _, _ = build_factory(account=account, profile=profile, session=session)

    usecase.execute()

    assert log == ["account.save", "profile.save", "sessions.save"]


def test_execute_returns_raw_secrets_and_stores_only_hashes() -> None:
    """生の秘密値を返し、永続化はハッシュだけであること。"""
    account = InMemoryAccountRepository()
    profile = InMemoryProfileRepository()
    session = InMemorySessionRepository()
    usecase, secret_hasher, password_hasher = build_factory(
        account=account, profile=profile, session=session
    )

    result = usecase.execute()

    assert result.public_id.startswith("p_") and len(result.public_id) == 18
    assert len(result.login_id) == 24
    assert len(result.initial_password) == 16
    assert len(result.recovery_key) == 32
    assert len(result.session_token) == 32

    saved_account = account.all()[0]
    saved_session = session.all()[0]
    assert saved_account.login_credential.login_id_hash == secret_hasher.hash(
        "login_id", result.login_id
    )
    assert saved_account.login_credential.password_hash == password_hasher.hash(
        result.initial_password
    )
    assert saved_account.recovery_credential.recovery_key_hash == secret_hasher.hash(
        "recovery_key", result.recovery_key
    )
    assert saved_session.token_hash == secret_hasher.hash("session", result.session_token)
    # 生値はハッシュへ残らない（64文字 HEX）。
    assert len(saved_account.login_credential.login_id_hash) == 64
    assert result.login_id not in saved_account.login_credential.login_id_hash


def test_profile_and_session_reference_the_same_user() -> None:
    """User・Profile・Session が同じ `user_id` で結ばれること。"""
    account = InMemoryAccountRepository()
    profile = InMemoryProfileRepository()
    session = InMemorySessionRepository()
    usecase, _, _ = build_factory(account=account, profile=profile, session=session)

    result = usecase.execute()

    saved_account = account.all()[0]
    saved_profile = profile.all()[0]
    saved_session = session.all()[0]
    assert saved_profile.user_id == saved_account.user.id.value
    assert saved_session.user_id == saved_account.user.id
    assert saved_profile.public_id.value == result.public_id


def test_execute_creates_an_empty_draft_profile() -> None:
    """作成直後の Profile は空本文の draft であること（公開は別操作）。"""
    account = InMemoryAccountRepository()
    profile = InMemoryProfileRepository()
    session = InMemorySessionRepository()
    usecase, _, _ = build_factory(account=account, profile=profile, session=session)

    usecase.execute()

    saved_profile = profile.all()[0]
    assert saved_profile.status is ProfileStatus.DRAFT
    assert saved_profile.content.value == ""
    assert saved_profile.published_at is None


def test_timestamps_use_the_clock_now() -> None:
    """時刻が `Clock.now()` で作られ、Session の絶対期限が作成+14日であること。"""
    account = InMemoryAccountRepository()
    profile = InMemoryProfileRepository()
    session = InMemorySessionRepository()
    usecase, _, _ = build_factory(account=account, profile=profile, session=session)

    usecase.execute()

    assert account.all()[0].user.created_at == NOW
    assert profile.all()[0].created_at == NOW
    assert session.all()[0].created_at == NOW
    assert session.all()[0].expires_at == NOW + SESSION_TTL


def test_unique_collision_rolls_back_and_retries_in_a_new_transaction() -> None:
    """1回目の衝突で rollback し、2回目で成功すること。

    1回目は Account を書いた後に Profile の `save` で衝突するため、rollback が効いて
    いなければ Account が残ってしまう（新 `user_id` で再生成されるので合計2件になる）。
    """
    log: list[str] = []
    account = InMemoryAccountRepository(log)
    profile = CollidingProfileRepository(collisions=1, log=log)
    session = InMemorySessionRepository(log)
    uows: list[FakeUnitOfWork] = []
    usecase, _, _ = build_factory(account=account, profile=profile, session=session, uows=uows)

    result = usecase.execute()

    assert result.public_id.startswith("p_")
    assert len(uows) == 2
    assert uows[0].rolled_back == 1
    assert uows[1].committed == 1
    assert len(account.all()) == 1
    assert len(profile.all()) == 1
    assert len(session.all()) == 1


def test_exhausted_retries_raise_internal_error_without_partial_rows() -> None:
    """5回衝突し続けると H-8 相当の 500 になり、部分行を残さないこと。"""
    account = InMemoryAccountRepository()
    profile = CollidingProfileRepository(collisions=99)
    session = InMemorySessionRepository()
    usecase, _, _ = build_factory(account=account, profile=profile, session=session)

    with pytest.raises(ProfileCreationFailedError) as excinfo:
        usecase.execute()

    assert excinfo.value.code.value == "INTERNAL_ERROR"
    # 最大5回だけ試行する（99 - 5 = 94 回分の衝突が残る）。
    assert profile.collisions == 99 - MAX_UNIQUE_RETRIES
    # 各試行の部分行は rollback されている。
    assert account.all() == []
    assert profile.all() == []
    assert session.all() == []
