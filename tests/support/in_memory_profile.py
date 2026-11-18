"""Profile application 層テスト用の InMemory ダブル（design/04-use-cases.md §5 / §12）。

- `InMemoryProfileRepository` は `design/03-domain-model.md` §4 の `ProfileRepository` を
  満たす InMemory 実装。
- `FakeUnitOfWork` は identity 側のダブル（失敗時に enter 時のスナップショットへ
  rollback する）を再利用する。

外へ出るのはテストダブルなので、`snapshot` / `restore` / `all` / `add` など Port に無い
補助メソッドを持ってよい。
"""

import copy
from collections.abc import Callable
from typing import Any

from src.profile.domain.entities import Profile
from src.profile.domain.value_objects import ProfileStatus, PublicProfileId
from tests.support.in_memory_identity import FakeUnitOfWork

__all__ = ["FakeUnitOfWork", "InMemoryProfileRepository"]


class InMemoryProfileRepository:
    """`ProfileRepository` の InMemory 実装。呼び出しを `log` に記録する。"""

    def __init__(self, log: list[str] | None = None) -> None:
        self._profiles: dict[str, Profile] = {}
        self.log: list[str] = log if log is not None else []

    def add(self, profile: Profile) -> None:
        self._profiles[profile.user_id] = profile

    def find_by_user_id(self, user_id: str) -> Profile | None:
        self.log.append("profile.find_by_user_id")
        return self._profiles.get(user_id)

    def find_by_public_id(self, public_id: PublicProfileId) -> Profile | None:
        self.log.append("profile.find_by_public_id")
        return self._find(lambda profile: profile.public_id == public_id)

    def find_published_by_public_id(self, public_id: PublicProfileId) -> Profile | None:
        self.log.append("profile.find_published_by_public_id")
        return self._find(
            lambda profile: (
                profile.public_id == public_id and profile.status is ProfileStatus.PUBLISHED
            )
        )

    def save(self, profile: Profile) -> None:
        self.log.append("profile.save")
        self._profiles[profile.user_id] = profile

    def _find(self, predicate: Callable[[Profile], bool]) -> Profile | None:
        for profile in self._profiles.values():
            if predicate(profile):
                return profile
        return None

    def all(self) -> list[Profile]:
        return list(self._profiles.values())

    def snapshot(self) -> dict[str, Profile]:
        return copy.deepcopy(self._profiles)

    def restore(self, state: Any) -> None:
        self._profiles = state
