"""`SqlProfileRepository` のテスト（一時ディレクトリの実 SQLite ファイル）。

このファイルが守るべき設計上の約束は次の6点。

1. `ProfileRepository` は `user_id` / `public_id` で取得でき、`public_id` では
   「公開中のみ」の取得が別メソッドとして分かれている
   （design/03-domain-model.md §4 Port 表）。
2. `find_published_by_public_id` は `status = 'published'` の行だけを返す
   （design/08-database.md §9 主要クエリ「公開取得」）。
3. `save` は新規作成と既存行の更新（本文・状態・`published_at`）の両方に対応し、
   `created_at` / `user_id` / `public_id` は差し替えない（§4 時刻の意味）。
4. `published_at` は draft で NULL、publish で値、unpublish で NULL に戻る。
5. 時刻は27文字の固定長ISO 8601で保存し、マイクロ秒まで含めて aware datetime として
   読み戻せる（design/08-database.md §9 共通規約）。
6. 全SQLは parameterized。

根拠: design/03-domain-model.md §4 / design/08-database.md §9 / design/11-testing.md §12。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.identity.domain.value_objects import UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.profile.domain.entities import Profile
from src.profile.domain.ports import ProfileRepository
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileId,
    ProfileStatus,
    PublicProfileId,
)
from src.profile.infrastructure.profile_repository import SqlProfileRepository
from src.shared.clock.clock import format_timestamp
from src.shared.db import UnitOfWork, connect
from tests.support.sqlite_identity import make_account, migrated_db_path
from tests.support.sqlite_profile import make_profile

USER_ID = "018f3c2a-7b1c-7abc-8def-0123456789ab"
OTHER_USER_ID = "018f3c2a-7b1c-7abd-8def-0123456789ac"
PROFILE_ID = ProfileId("018f3c2a-7b1c-7def-8abc-0123456789cd")

# マイクロ秒が丸められる formatter を見逃さないよう、意図的に 0 以外を使う。
MOMENT = datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=UTC)
LATER = MOMENT + timedelta(hours=1)


def public_id(suffix: str = "1") -> PublicProfileId:
    return PublicProfileId("p_" + suffix * 16)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return migrated_db_path(tmp_path)


def seed_user(db_path: Path, user_id: str = USER_ID) -> None:
    """`profiles.user_id` の FK を満たすため、先に `users` 行を作る。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlAccountRepository(uow.connection).save(make_account(user_id=UserId(user_id)))


def save(db_path: Path, profile: Profile) -> None:
    """ユースケースと同じ形で `UnitOfWork` の中で保存する。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlProfileRepository(uow.connection).save(profile)


def find_by_user_id(db_path: Path, user_id: str) -> Profile | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlProfileRepository(uow.connection).find_by_user_id(user_id)


def find_by_public_id(db_path: Path, value: PublicProfileId) -> Profile | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlProfileRepository(uow.connection).find_by_public_id(value)


def find_published_by_public_id(db_path: Path, value: PublicProfileId) -> Profile | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlProfileRepository(uow.connection).find_published_by_public_id(value)


def test_save_writes_the_row_and_find_by_user_id_reads_it_back(db_path: Path) -> None:
    """保存した draft Profile を `user_id` で読み戻せること。"""
    seed_user(db_path)
    profile = make_profile(
        id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文", now=MOMENT
    )

    save(db_path, profile)

    loaded = find_by_user_id(db_path, USER_ID)
    assert loaded is not None
    assert loaded.id == PROFILE_ID
    assert loaded.user_id == USER_ID
    assert loaded.public_id == public_id()
    assert loaded.content == ProfileContent("本文")
    assert loaded.status is ProfileStatus.DRAFT
    assert loaded.published_at is None
    assert loaded.created_at == MOMENT

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM profiles").fetchone()[0] == 1
    finally:
        reader.close()


def test_find_by_public_id_returns_draft_and_published(db_path: Path) -> None:
    """`find_by_public_id` は公開フィルタを掛けず、draft も published も返すこと。

    「公開中のみ」は `find_published_by_public_id` の責務で、こちらは編集経路用。
    """
    seed_user(db_path)
    profile = make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文")
    save(db_path, profile)

    draft = find_by_public_id(db_path, public_id())
    assert draft is not None
    assert draft.status is ProfileStatus.DRAFT

    profile.publish(LATER)
    save(db_path, profile)

    published = find_by_public_id(db_path, public_id())
    assert published is not None
    assert published.status is ProfileStatus.PUBLISHED


def test_find_published_by_public_id_returns_published_only(db_path: Path) -> None:
    """公開取得は `status = 'published'` だけを返し、draft と不在は `None`。"""
    seed_user(db_path)
    draft = make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id("1"), content="本文")
    save(db_path, draft)

    assert find_published_by_public_id(db_path, public_id("1")) is None

    draft.publish(LATER)
    save(db_path, draft)

    found = find_published_by_public_id(db_path, public_id("1"))
    assert found is not None
    assert found.status is ProfileStatus.PUBLISHED

    assert find_published_by_public_id(db_path, public_id("2")) is None


def test_save_updates_content_and_status_keeps_identity_and_created_at(db_path: Path) -> None:
    """既存 Profile の更新で行が増えず、`created_at` / `user_id` / `public_id` が不変。"""
    seed_user(db_path)
    profile = make_profile(
        id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="旧", now=MOMENT
    )
    save(db_path, profile)

    assert profile.update(ProfileContent("新"), LATER) is True
    assert profile.publish(LATER) is True
    save(db_path, profile)

    loaded = find_by_user_id(db_path, USER_ID)
    assert loaded is not None
    assert loaded.content == ProfileContent("新")
    assert loaded.status is ProfileStatus.PUBLISHED
    assert loaded.published_at == LATER
    assert loaded.updated_at == LATER
    # `created_at` / 識別子は作成時に一度だけ（§4 時刻の意味）。
    assert loaded.created_at == MOMENT
    assert loaded.public_id == public_id()

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM profiles").fetchone()[0] == 1
    finally:
        reader.close()


def test_published_at_round_trips_and_clears_on_unpublish(db_path: Path) -> None:
    """`published_at` が draft で NULL、publish で値、unpublish で NULL に戻ること。"""
    seed_user(db_path)
    profile = make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文")
    save(db_path, profile)
    assert find_by_user_id(db_path, USER_ID).published_at is None  # type: ignore[union-attr]

    profile.publish(LATER)
    save(db_path, profile)
    assert find_by_user_id(db_path, USER_ID).published_at == LATER  # type: ignore[union-attr]

    profile.unpublish(LATER + timedelta(hours=1))
    save(db_path, profile)
    assert find_by_user_id(db_path, USER_ID).published_at is None  # type: ignore[union-attr]


def test_timestamps_are_stored_as_27_characters_and_round_trip(db_path: Path) -> None:
    """時刻が27文字の固定長ISO 8601で保存され、マイクロ秒まで一致すること。"""
    seed_user(db_path)
    profile = make_profile(
        id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文", now=MOMENT
    )
    profile.publish(LATER)
    save(db_path, profile)

    reader = connect(db_path)
    try:
        row = reader.execute("SELECT created_at, updated_at, published_at FROM profiles").fetchone()
    finally:
        reader.close()

    for text in (row["created_at"], row["updated_at"], row["published_at"]):
        assert len(text) == 27
        assert text.endswith("Z")
    assert row["created_at"] == format_timestamp(MOMENT)
    assert row["published_at"] == format_timestamp(LATER)

    loaded = find_by_user_id(db_path, USER_ID)
    assert loaded is not None
    assert loaded.created_at == MOMENT
    assert loaded.created_at.microsecond == MOMENT.microsecond
    assert loaded.updated_at.tzinfo is not None
    assert loaded.published_at == LATER


def test_missing_returns_none(db_path: Path) -> None:
    """該当なしは全経路とも `None`。例外や空 Profile を返さないこと。"""
    seed_user(db_path)
    save(
        db_path,
        make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文"),
    )

    assert find_by_user_id(db_path, OTHER_USER_ID) is None
    assert find_by_public_id(db_path, public_id("2")) is None
    assert find_published_by_public_id(db_path, public_id("2")) is None


def test_lookups_treat_sql_fragments_as_values(db_path: Path) -> None:
    """SQL断片を値として渡してもSQLとして実行されないこと（全SQL parameterized）。"""
    seed_user(db_path)
    save(
        db_path,
        make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文"),
    )

    fragment = "' OR '1'='1"
    assert find_by_user_id(db_path, fragment) is None

    reader = connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM profiles").fetchone()[0] == 1
    finally:
        reader.close()


def test_repository_satisfies_the_profile_repository_port() -> None:
    """`design/03-domain-model.md` §4 の Port を満たしていること。"""
    repository = SqlProfileRepository(sqlite3.connect(":memory:"))

    assert isinstance(repository, ProfileRepository)


def test_public_id_unique_constraint_is_enforced(db_path: Path) -> None:
    """`public_id` の UNIQUE 制約が効き、衝突は `sqlite3.IntegrityError` で外へ出ること。

    `CreateProfile`（T-06-02）の一意衝突再生成がこの例外を支点にする。
    """
    seed_user(db_path)
    first = make_profile(id=PROFILE_ID, user_id=USER_ID, public_id=public_id(), content="本文")
    save(db_path, first)

    second_user = "018f3c2a-7b1c-7abe-8def-0123456789ad"
    seed_user(db_path, second_user)
    second = make_profile(
        id=ProfileId("018f3c2a-7b1c-7eff-8abc-0123456789ce"),
        user_id=second_user,
        public_id=public_id(),  # 同じ public_id
        content="別本文",
    )

    with pytest.raises(sqlite3.IntegrityError):
        save(db_path, second)
