"""Profile application の3ユースケースのテスト（design/04-use-cases.md §5 / §12）。

守る約束は 4 点。

1. `GetPublicProfile` は published だけを `{public_id, content}` に変換し、
   非公開・存在しない・形式違反を区別しない（H-1 / H-2、AC-2 / AC-3）。
2. `GetMyProfile` / `UpdateMyProfile` は `user_id` だけを受け取り `Principal` を取らない。
3. `UpdateMyProfile` は content と status を1 transaction で更新し、no-op では
   `save` を呼ばず `updated_at` も更新しない。
4. 空 content への publish は `PROFILE_NOT_PUBLISHABLE`（H-15）で、失敗 transaction は
   rollback される。
"""

import inspect
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from src.profile.application.errors import (
    ProfileNotFoundError,
    ProfileNotPublishableError,
)
from src.profile.application.usecases import (
    GetMyProfile,
    GetPublicProfile,
    ProfileDependencies,
    UpdateMyProfile,
)
from src.profile.domain.entities import Profile
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileId,
    ProfileStatus,
    PublicProfileId,
)
from tests.support.fake_clock import FakeClock
from tests.support.in_memory_identity import FakeUnitOfWork
from tests.support.in_memory_profile import InMemoryProfileRepository

USER_ID = "018f3c2a-7b1c-7abc-8def-0123456789ab"
NOW = datetime(2026, 10, 6, tzinfo=UTC)
LATER = datetime(2026, 10, 6, 1, tzinfo=UTC)


def _public_id() -> PublicProfileId:
    return PublicProfileId("p_" + "1" * 16)


def _profile(
    *,
    content: str = "本文",
    status: ProfileStatus = ProfileStatus.DRAFT,
    now: datetime = NOW,
) -> Profile:
    profile = Profile.create(
        id=ProfileId(USER_ID),
        user_id=USER_ID,
        public_id=_public_id(),
        content=ProfileContent(content),
        now=now,
    )
    if status is ProfileStatus.PUBLISHED:
        profile.publish(now)
    return profile


def _deps(repository: InMemoryProfileRepository, now: datetime = NOW) -> ProfileDependencies:
    return ProfileDependencies(
        profile_repository=repository,
        clock=FakeClock(now),
        unit_of_work=FakeUnitOfWork(repository),
    )


def test_get_public_profile_returns_only_public_fields() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(status=ProfileStatus.PUBLISHED))
    result = GetPublicProfile(_deps(repository)).execute(public_id=_public_id().value)
    assert result.public_id == _public_id().value
    assert result.content == "本文"
    assert {field.name for field in fields(result)} == {"public_id", "content"}


def test_get_public_profile_hides_draft_missing_and_invalid() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(status=ProfileStatus.DRAFT))  # 非公開
    usecase = GetPublicProfile(_deps(repository))

    with pytest.raises(ProfileNotFoundError):
        usecase.execute(public_id=_public_id().value)  # 非公開
    with pytest.raises(ProfileNotFoundError):
        usecase.execute(public_id="p_" + "2" * 16)  # 存在しない
    with pytest.raises(ProfileNotFoundError):
        usecase.execute(public_id="not-a-public-id")  # 形式違反も同一応答


def test_get_my_profile_returns_four_fields() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(status=ProfileStatus.PUBLISHED))
    result = GetMyProfile(_deps(repository)).execute(user_id=USER_ID)
    assert result.public_id == _public_id().value
    assert result.status == "published"
    assert result.content == "本文"
    assert result.published_at == NOW


def test_get_my_profile_missing_raises() -> None:
    repository = InMemoryProfileRepository()
    with pytest.raises(ProfileNotFoundError):
        GetMyProfile(_deps(repository)).execute(user_id=USER_ID)


def test_update_updates_content_in_one_transaction() -> None:
    repository = InMemoryProfileRepository()
    profile = _profile(status=ProfileStatus.PUBLISHED)
    repository.add(profile)
    unit_of_work = FakeUnitOfWork(repository)
    deps = ProfileDependencies(
        profile_repository=repository, clock=FakeClock(LATER), unit_of_work=unit_of_work
    )

    result = UpdateMyProfile(deps).execute(user_id=USER_ID, content=ProfileContent("更新後"))

    assert result.content == "更新後"
    assert result.status == "published"  # published のまま更新できる
    assert profile.updated_at == LATER
    assert unit_of_work.entered == 1
    assert unit_of_work.committed == 1
    assert "profile.save" in repository.log


def test_update_noop_does_not_save_or_touch_updated_at() -> None:
    repository = InMemoryProfileRepository()
    profile = _profile(content="本文", status=ProfileStatus.DRAFT)
    repository.add(profile)

    result = UpdateMyProfile(_deps(repository, LATER)).execute(
        user_id=USER_ID,
        content=ProfileContent("  本文  "),  # 正規化すると同値
        status=ProfileStatus.DRAFT,  # 同状態
    )

    assert result.content == "本文"
    assert profile.updated_at == NOW
    assert "profile.save" not in repository.log


def test_update_publish_sets_status_and_published_at() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(status=ProfileStatus.DRAFT))
    result = UpdateMyProfile(_deps(repository, LATER)).execute(
        user_id=USER_ID, status=ProfileStatus.PUBLISHED
    )
    assert result.status == "published"
    assert result.published_at == LATER


def test_update_unpublish_clears_published_at() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(status=ProfileStatus.PUBLISHED))
    result = UpdateMyProfile(_deps(repository, LATER)).execute(
        user_id=USER_ID, status=ProfileStatus.DRAFT
    )
    assert result.status == "draft"
    assert result.published_at is None


def test_update_empty_content_publish_raises_and_rolls_back() -> None:
    repository = InMemoryProfileRepository()
    repository.add(_profile(content="本文", status=ProfileStatus.DRAFT))

    with pytest.raises(ProfileNotPublishableError) as excinfo:
        UpdateMyProfile(_deps(repository, LATER)).execute(
            user_id=USER_ID,
            content=ProfileContent("   "),  # 空
            status=ProfileStatus.PUBLISHED,
        )
    assert excinfo.value.code.value == "PROFILE_NOT_PUBLISHABLE"

    restored = repository.find_by_user_id(USER_ID)
    assert restored is not None
    assert restored.content.value == "本文"  # content 更新も rollback される
    assert restored.updated_at == NOW
    assert restored.status is ProfileStatus.DRAFT


def test_update_missing_profile_raises() -> None:
    repository = InMemoryProfileRepository()
    with pytest.raises(ProfileNotFoundError):
        UpdateMyProfile(_deps(repository, LATER)).execute(
            user_id=USER_ID, content=ProfileContent("更新")
        )


def test_my_profile_usecases_do_not_take_principal() -> None:
    for usecase_class in (GetMyProfile, UpdateMyProfile):
        parameters = inspect.signature(usecase_class.execute).parameters
        assert "user_id" in parameters
        assert "principal" not in parameters
