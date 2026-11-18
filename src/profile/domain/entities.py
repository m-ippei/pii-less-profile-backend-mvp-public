"""Profile の Entity（design/03-domain-model.md §4）。

Profile は所有者を `user_id` だけで参照する（Context 間は `user_id` のみ、
design/02-architecture.md §3）。`user_id` は不透明な文字列として扱い、
`identity` を import しない。時刻の規則は §4「時刻の意味」に従う。
"""

from dataclasses import dataclass
from datetime import datetime

from .errors import InvalidProfileTransitionError, ProfileNotPublishableError
from .value_objects import ProfileContent, ProfileId, ProfileStatus, PublicProfileId


@dataclass
class Profile:
    """匿名本文と公開状態をまとめて守る Aggregate の実体。"""

    id: ProfileId
    user_id: str
    public_id: PublicProfileId
    content: ProfileContent
    status: ProfileStatus
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        id: ProfileId,
        user_id: str,
        public_id: PublicProfileId,
        content: ProfileContent,
        now: datetime,
    ) -> Profile:
        """新しい draft の Profile を作る。`published_at` は未設定。"""
        return cls(
            id=id,
            user_id=user_id,
            public_id=public_id,
            content=content,
            status=ProfileStatus.DRAFT,
            published_at=None,
            created_at=now,
            updated_at=now,
        )

    def update(self, content: ProfileContent, now: datetime) -> bool:
        """本文を差し替える。published のままでも呼べる（§4 Domain rule）。

        正規化後の値が変わった時だけ `updated_at` を設定して `True` を返す。
        同値なら書き換えず `False`（no-op では `updated_at` を更新しない）。
        """
        if content.value == self.content.value:
            return False
        self.content = content
        self.updated_at = now
        return True

    def publish(self, now: datetime) -> bool:
        """draft → published。更新後の `content` が非空でなければ失敗する（H-15）。"""
        if self.status is ProfileStatus.PUBLISHED:
            raise InvalidProfileTransitionError("profile is already published")
        if self.content.is_empty:
            raise ProfileNotPublishableError("cannot publish an empty profile")
        self.status = ProfileStatus.PUBLISHED
        self.published_at = now
        self.updated_at = now
        return True

    def unpublish(self, now: datetime) -> bool:
        """published → draft。`published_at` を `None` に戻す（§4 時刻の意味）。"""
        if self.status is not ProfileStatus.PUBLISHED:
            raise InvalidProfileTransitionError("profile is not published")
        self.status = ProfileStatus.DRAFT
        self.published_at = None
        self.updated_at = now
        return True
