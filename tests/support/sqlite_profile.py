"""実 SQLite を使う Profile Repository テストの共通ヘルパー（design/11-testing.md §12）。

- `make_profile` は design/03-domain-model.md §4 の `Profile` を固定値で組み立てる。
  DB へ保存する前に `Profile.create` / `publish` を通すので、テスト側で状態遷移規則を
  再実装しない。実 migration を適用した一時DB（`tests.support.sqlite_identity` の
  `migrated_db_path`）と組み合わせて使う。

テスト以外から import してはいけない。
"""

from datetime import datetime

from src.profile.domain.entities import Profile
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileId,
    ProfileStatus,
    PublicProfileId,
)
from tests.support.sqlite_identity import START

__all__ = ["START", "make_profile"]


def make_profile(
    *,
    id: ProfileId,
    user_id: str,
    public_id: PublicProfileId,
    content: str = "",
    status: ProfileStatus = ProfileStatus.DRAFT,
    now: datetime = START,
) -> Profile:
    """`Profile` を固定値で組み立てる。published を指定する場合は content を非空にする。"""
    profile = Profile.create(
        id=id,
        user_id=user_id,
        public_id=public_id,
        content=ProfileContent(content),
        now=now,
    )
    if status is ProfileStatus.PUBLISHED:
        profile.publish(now)
    return profile
