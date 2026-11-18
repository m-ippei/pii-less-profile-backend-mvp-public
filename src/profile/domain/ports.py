"""Profile の Port（design/03-domain-model.md §4 Port 表）。

Repository は取得・保存の抽象で、SQL・HTTP・Cookie・FastAPI を知らない。
`domain` 層なので標準ライブラリと同一パッケージ内の import だけを使う
（design/02-architecture.md §3）。

`UnitOfWork` は全 Context 共通の transaction 境界だが、`profile` から `identity` を
import できない（Context 相互参照禁止）ため、構造的に同一の Protocol をここにも置く。
具象 `src.shared.db.UnitOfWork` がそのまま満たす。
"""

from types import TracebackType
from typing import Protocol, runtime_checkable

from .entities import Profile
from .value_objects import PublicProfileId


@runtime_checkable
class ProfileRepository(Protocol):
    """Profile Aggregate の取得（user_id / public_id）と保存。"""

    def find_by_user_id(self, user_id: str) -> Profile | None: ...

    def find_by_public_id(self, public_id: PublicProfileId) -> Profile | None: ...

    def find_published_by_public_id(self, public_id: PublicProfileId) -> Profile | None: ...

    def save(self, profile: Profile) -> None: ...


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
