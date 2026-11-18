"""Profile のユースケース（design/04-use-cases.md §5）。

- `GetPublicProfile`: published だけを公開DTO（`public_id`,`content` のみ）に変換する。
  非公開・存在しない・形式違反は同一の `ProfileNotFoundError`（H-1 / H-2）。
- `GetMyProfile` / `UpdateMyProfile`: `user_id` だけを受け取り、`Principal` は受け取らない
  （design/02-architecture.md §3）。現在ユーザーの解決は結線側（`main.py`）の責務。
- `UpdateMyProfile` は `content` 更新と状態遷移を 1 transaction で行い、実変更が無い
  時は `save` を呼ばず `updated_at` も更新しない（§5 Transaction / §4 Domain rule）。
"""

from dataclasses import dataclass
from datetime import datetime

from src.profile.domain.entities import Profile
from src.profile.domain.errors import (
    ProfileNotPublishableError as DomainProfileNotPublishableError,
)
from src.profile.domain.ports import ProfileRepository, UnitOfWork
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileStatus,
    PublicProfileId,
)
from src.shared.clock.clock import Clock

from .errors import ProfileNotFoundError, ProfileNotPublishableError


@dataclass(frozen=True)
class ProfileDependencies:
    """Profile ユースケースが使うポート一式。`main.py` で結線する。"""

    profile_repository: ProfileRepository
    clock: Clock
    unit_of_work: UnitOfWork


@dataclass(frozen=True)
class PublicProfile:
    """公開DTO。`public_id` と `content` だけを持つ（AC-3）。"""

    public_id: str
    content: str


@dataclass(frozen=True)
class MyProfile:
    """`GET /me/profile` / `PATCH /me/profile` の応答（4フィールド）。"""

    public_id: str
    status: str
    content: str
    published_at: datetime | None


def _to_my_profile(profile: Profile) -> MyProfile:
    return MyProfile(
        public_id=profile.public_id.value,
        status=profile.status.value,
        content=profile.content.value,
        published_at=profile.published_at,
    )


class GetPublicProfile:
    """`design/04-use-cases.md` §5 GetPublicProfile。公開中のみ公開DTOへ変換する。"""

    def __init__(self, deps: ProfileDependencies) -> None:
        self._deps = deps

    def execute(self, *, public_id: str) -> PublicProfile:
        try:
            parsed = PublicProfileId(public_id)
        except ValueError:
            # 形式違反も「存在しない」と同じ応答にする（入力を echo しない。H-1/H-2）。
            raise ProfileNotFoundError() from None
        profile = self._deps.profile_repository.find_published_by_public_id(parsed)
        if profile is None or profile.status is not ProfileStatus.PUBLISHED:
            raise ProfileNotFoundError()
        return PublicProfile(public_id=profile.public_id.value, content=profile.content.value)


class GetMyProfile:
    """`design/04-use-cases.md` §5 GetMyProfile。`user_id` の Profile を返す。"""

    def __init__(self, deps: ProfileDependencies) -> None:
        self._deps = deps

    def execute(self, *, user_id: str) -> MyProfile:
        profile = self._deps.profile_repository.find_by_user_id(user_id)
        if profile is None:
            raise ProfileNotFoundError()
        return _to_my_profile(profile)


class UpdateMyProfile:
    """`design/04-use-cases.md` §5 UpdateMyProfile。

    本文更新と状態遷移を1回で行い、実変更がある時だけ `save` する。空 `content` への
    publish は `PROFILE_NOT_PUBLISHABLE`（H-15）。同状態・同内容は no-op 成功。
    """

    def __init__(self, deps: ProfileDependencies) -> None:
        self._deps = deps

    def execute(
        self,
        *,
        user_id: str,
        content: ProfileContent | None = None,
        status: ProfileStatus | None = None,
    ) -> MyProfile:
        deps = self._deps
        with deps.unit_of_work:
            profile = deps.profile_repository.find_by_user_id(user_id)
            if profile is None:
                raise ProfileNotFoundError()

            now = deps.clock.now()
            changed = False
            if content is not None:
                changed = profile.update(content, now)
            if status is not None and status is not profile.status:
                if status is ProfileStatus.PUBLISHED:
                    try:
                        profile.publish(now)
                    except DomainProfileNotPublishableError:
                        raise ProfileNotPublishableError() from None
                else:
                    profile.unpublish(now)
                changed = True

            if changed:
                deps.profile_repository.save(profile)
        return _to_my_profile(profile)
