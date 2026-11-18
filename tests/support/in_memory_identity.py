"""Identity application 層テスト用の InMemory ダブル（design/04-use-cases.md §5 / §12）。

- Repository は `design/03-domain-model.md` §4 の Port を満たす InMemory 実装。
- `FakePasswordHasher` は Argon2 を使わず、`dummy_verify` の呼び出し回数を数える。
- `FakeUnitOfWork` は `BEGIN IMMEDIATE` の代わりに、成功で commit、失敗で
  enter 時のスナップショットへ rollback する（原子性テストのため）。

外へ出るのはテストダブルなので、`snapshot` / `restore` など Port に無い補助メソッドを
持ってよい。
"""

import copy
import unicodedata
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any

from src.identity.domain.entities import Account, Session
from src.identity.domain.value_objects import UserId
from src.shared.security.security import PasswordHasher


class FakePasswordHasher(PasswordHasher):
    """決定的・高速な `PasswordHasher` ダブル。`dummy_verify` の呼び出しを数える。

    `events` を渡すと、各呼び出しをその共有リストへ追記する（`FakeUnitOfWork` の
    `uow.enter` と並べることで、Argon2 が transaction の外で実行されることを観測する）。
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.dummy_calls = 0
        self._events = events

    def _record(self, name: str) -> None:
        if self._events is not None:
            self._events.append(name)

    def hash(self, password: str) -> str:
        self._record("hasher.hash")
        return self._encode(password)

    def verify(self, hashed: str, password: str) -> bool:
        self._record("hasher.verify")
        return hashed == self._encode(password)

    def dummy_verify(self) -> bool:
        self.dummy_calls += 1
        self._record("hasher.dummy_verify")
        return False

    @staticmethod
    def _encode(password: str) -> str:
        return "fake$" + unicodedata.normalize("NFKC", password)


class InMemoryAccountRepository:
    """`AccountRepository` の InMemory 実装。呼び出しを `log` に記録する。"""

    def __init__(self, log: list[str] | None = None) -> None:
        self._accounts: dict[str, Account] = {}
        self.log: list[str] = log if log is not None else []

    def add(self, account: Account) -> None:
        self._accounts[account.user.id.value] = account

    def find_by_id(self, user_id: UserId) -> Account | None:
        self.log.append("account.find_by_id")
        return self._accounts.get(user_id.value)

    def find_by_login_id_hash(self, login_id_hash: str) -> Account | None:
        self.log.append("account.find_by_login_id_hash")
        return self._find(lambda account: account.login_credential.login_id_hash == login_id_hash)

    def find_by_recovery_key_hash(self, recovery_key_hash: str) -> Account | None:
        self.log.append("account.find_by_recovery_key_hash")
        return self._find(
            lambda account: account.recovery_credential.recovery_key_hash == recovery_key_hash
        )

    def save(self, account: Account) -> None:
        self.log.append("account.save")
        self._accounts[account.user.id.value] = account

    def _find(self, predicate: Callable[[Account], bool]) -> Account | None:
        for account in self._accounts.values():
            if predicate(account):
                return account
        return None

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def snapshot(self) -> dict[str, Account]:
        return copy.deepcopy(self._accounts)

    def restore(self, state: Any) -> None:
        self._accounts = state


class InMemorySessionRepository:
    """`SessionRepository` の InMemory 実装。呼び出しを `log` に記録する。"""

    def __init__(self, log: list[str] | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self.log: list[str] = log if log is not None else []

    def add(self, session: Session) -> None:
        self._sessions[session.token_hash] = session

    def find_by_token_hash(self, token_hash: str) -> Session | None:
        self.log.append("sessions.find_by_token_hash")
        return self._sessions.get(token_hash)

    def save(self, session: Session) -> None:
        self.log.append("sessions.save")
        self._sessions[session.token_hash] = session

    def touch(self, session: Session, now: datetime) -> None:
        self.log.append("sessions.touch")
        if session.touch(now):
            self._sessions[session.token_hash] = session

    def revoke_all_for_user(self, user_id: UserId, now: datetime) -> None:
        self.log.append("sessions.revoke_all_for_user")
        for session in self._sessions.values():
            if session.user_id == user_id and session.revoked_at is None:
                session.revoked_at = now

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def snapshot(self) -> dict[str, Session]:
        return copy.deepcopy(self._sessions)

    def restore(self, state: Any) -> None:
        self._sessions = state


class FakeUnitOfWork:
    """`UnitOfWork` のダブル。失敗時に enter 時のスナップショットへ rollback する。

    `events` を渡すと transaction の開始を共有リストへ追記する（`FakePasswordHasher` の
    `hasher.*` と並べ、Argon2 が `BEGIN IMMEDIATE` の外で実行されることを観測する）。
    """

    def __init__(self, *repositories: Any, events: list[str] | None = None) -> None:
        self._repositories = repositories
        self._events = events
        self._snapshots: list[Any] = []
        self.entered = 0
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self) -> FakeUnitOfWork:
        self._snapshots = [repository.snapshot() for repository in self._repositories]
        self.entered += 1
        if self._events is not None:
            self._events.append("uow.enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            self.committed += 1
        else:
            self.rolled_back += 1
            for repository, snapshot in zip(self._repositories, self._snapshots, strict=True):
                repository.restore(snapshot)
        return False
