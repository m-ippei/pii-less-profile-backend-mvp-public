"""Context 跨ぎユースケース `CreateProfile`（design/04-use-cases.md §5）。

`src/app/` だけが `identity` と `profile` の両方を import できる
（design/02-architecture.md §3）。User・Profile・資格情報（`login_id` / パスワード /
`recovery_key`）・初回 Session を 1 transaction で作成し、初回秘密値だけを返す。

- 保存順は `users` → `login_credentials` → `recovery_credentials` → `profiles` →
  `sessions`（design/04-use-cases.md §5 Transaction 表）。`AccountRepository.save` が
  先頭3テーブルをこの順で書き、続いて Profile、Session の順に保存する。
- 一意制約衝突（`login_id_hash` / `recovery_key_hash` / `public_id` / `token_hash`）は
  当該 transaction を rollback し、全値を再生成して最大5回再試行する。各試行は新しい
  transaction（新しい `UnitOfWork`）で行うため、依存は「毎回新しい一式を返す
  ファクトリ」で受け取る。尽きたら H-8 相当の 500（design/06-errors.md §7）。
- 生の秘密値は戻り値としてだけ外へ出し、Entity・DB には保持しない
  （design/07-security.md §8 秘密値と保存）。
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from src.identity.application.usecases import (
    LOGIN_ID_LABEL,
    RECOVERY_KEY_LABEL,
    SESSION_LABEL,
)
from src.identity.domain.entities import (
    Account,
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.ports import AccountRepository, SessionRepository, UnitOfWork
from src.identity.domain.value_objects import UserId
from src.profile.domain.entities import Profile
from src.profile.domain.ports import ProfileRepository
from src.profile.domain.value_objects import ProfileContent, ProfileId, PublicProfileId
from src.shared.clock.clock import Clock
from src.shared.security.security import (
    IdGenerator,
    PasswordHasher,
    SecretGenerator,
    SecretHasher,
)

from .errors import ProfileCreationFailedError

# H-8: 一意制約衝突の再試行上限（design/04-use-cases.md §5 / design/06-errors.md §7）。
MAX_UNIQUE_RETRIES = 5


@dataclass(frozen=True)
class CreateProfileDependencies:
    """`CreateProfile` が使うポート一式。`main.py` が毎試行ぶん組み立てる。"""

    account_repository: AccountRepository
    profile_repository: ProfileRepository
    session_repository: SessionRepository
    password_hasher: PasswordHasher
    secret_hasher: SecretHasher
    secret_generator: SecretGenerator
    id_generator: IdGenerator
    clock: Clock
    unit_of_work: UnitOfWork


@dataclass(frozen=True)
class CreatedProfile:
    """作成結果。生の秘密値はここから HTTP 層へ一度だけ渡す。

    `session_token` は cookie に載せ、応答本文には含めない（design/05-api-contract.md §6）。
    """

    public_id: str
    login_id: str
    initial_password: str
    recovery_key: str
    session_token: str


class CreateProfile:
    """`design/04-use-cases.md` §5 CreateProfile。

    依存は「新しい transaction の一式を返すファクトリ」で受け取る。一意衝突で
    rollback した後、同じファクトリから再生成して再試行するため。
    """

    def __init__(self, dependencies_factory: Callable[[], CreateProfileDependencies]) -> None:
        self._dependencies_factory = dependencies_factory

    def execute(self) -> CreatedProfile:
        """作成を最大 `MAX_UNIQUE_RETRIES` 回試み、成功した結果を返す。"""
        last_error: sqlite3.IntegrityError | None = None
        for _ in range(MAX_UNIQUE_RETRIES):
            dependencies = self._dependencies_factory()
            try:
                return self._attempt(dependencies)
            except sqlite3.IntegrityError as error:
                # 一意制約衝突。`UnitOfWork` が当該 transaction を rollback 済み。
                # 全値を再生成して次の試行へ（H-8）。
                last_error = error
        raise ProfileCreationFailedError() from last_error

    def _attempt(self, dependencies: CreateProfileDependencies) -> CreatedProfile:
        """1 transaction で5テーブルを作成する。"""
        now = dependencies.clock.now()
        user_id = UserId(dependencies.id_generator.generate())
        login_id = dependencies.secret_generator.login_id()
        initial_password = dependencies.secret_generator.initial_password()
        recovery_key = dependencies.secret_generator.recovery_key()
        public_id = dependencies.secret_generator.public_id()
        session_token = dependencies.secret_generator.session_token()

        account = Account(
            user=User(id=user_id, created_at=now, updated_at=now),
            login_credential=LoginCredential.issue(
                user_id=user_id,
                login_id_hash=dependencies.secret_hasher.hash(LOGIN_ID_LABEL, login_id),
                password_hash=dependencies.password_hasher.hash(initial_password),
                now=now,
            ),
            recovery_credential=RecoveryCredential.issue(
                user_id=user_id,
                recovery_key_hash=dependencies.secret_hasher.hash(RECOVERY_KEY_LABEL, recovery_key),
                now=now,
            ),
        )
        profile = Profile.create(
            id=ProfileId(dependencies.id_generator.generate()),
            user_id=user_id.value,
            public_id=PublicProfileId(public_id),
            content=ProfileContent(""),
            now=now,
        )
        session = Session.create(
            id=UserId(dependencies.id_generator.generate()),
            user_id=user_id,
            token_hash=dependencies.secret_hasher.hash(SESSION_LABEL, session_token),
            now=now,
        )

        with dependencies.unit_of_work:
            # users → login_credentials → recovery_credentials は save が順に行う。
            dependencies.account_repository.save(account)
            dependencies.profile_repository.save(profile)
            dependencies.session_repository.save(session)

        return CreatedProfile(
            public_id=public_id,
            login_id=login_id,
            initial_password=initial_password,
            recovery_key=recovery_key,
            session_token=session_token,
        )
