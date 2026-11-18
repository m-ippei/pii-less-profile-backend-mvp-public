"""Identity の Port（design/03-domain-model.md §4 Port 表）。

Repository は取得・保存・失効の抽象で、SQL・HTTP・Cookie・FastAPI を知らない。
Repository は常に `user_id` 条件で検索する。`domain` 層なので標準ライブラリと
同一パッケージ内の import だけを使う（design/02-architecture.md §3）。
"""

from datetime import datetime
from types import TracebackType
from typing import Protocol, runtime_checkable

from .entities import Account, Session
from .value_objects import UserId


@runtime_checkable
class AccountRepository(Protocol):
    """Account Aggregate の取得（user_id / login_id_hash / recovery_key_hash）と保存。"""

    def find_by_id(self, user_id: UserId) -> Account | None: ...

    def find_by_login_id_hash(self, login_id_hash: str) -> Account | None: ...

    def find_by_recovery_key_hash(self, recovery_key_hash: str) -> Account | None: ...

    def save(self, account: Account) -> None: ...


@runtime_checkable
class SessionRepository(Protocol):
    """Session Aggregate の token hash 取得・保存・touch・user単位の一括失効。"""

    def find_by_token_hash(self, token_hash: str) -> Session | None: ...

    def save(self, session: Session) -> None: ...

    def touch(self, session: Session, now: datetime) -> None: ...

    def revoke_all_for_user(self, user_id: UserId, now: datetime) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """transaction 境界（`BEGIN IMMEDIATE`）。抜ける時に commit / rollback する。"""

    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...
