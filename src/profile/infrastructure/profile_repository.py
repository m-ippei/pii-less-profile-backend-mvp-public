"""`ProfileRepository` の実 SQLite 実装（design/03-domain-model.md §4 Port）。

根拠: design/03-domain-model.md §4 Port / design/08-database.md §9。

- `UnitOfWork` と同じ `sqlite3.Connection` を受け取る。transaction の開始・終了は
  このクラスでは行わない（`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` は `UnitOfWork`
  だけ。design/10-infra-ops.md §11）。接続はリクエスト単位。
- 全SQLは parameterized。値を SQL 文字列へ埋め込まない（design/08-database.md §9 主要クエリ）。
- `published_at` は draft で NULL、publish で値、unpublish で NULL に戻る
  （design/03-domain-model.md §4 時刻の意味）。
"""

import sqlite3

from src.profile.domain.entities import Profile
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileId,
    ProfileStatus,
    PublicProfileId,
)
from src.shared.clock.clock import format_timestamp, parse_timestamp

# SELECT の列は Entity の属性と1対1（design/03-domain-model.md §4 Entity Profile）。
_PROFILE_COLUMNS = """
  id           AS id,
  user_id      AS user_id,
  public_id    AS public_id,
  status       AS status,
  content      AS content,
  published_at AS published_at,
  created_at   AS created_at,
  updated_at   AS updated_at
"""

PROFILE_FROM = "FROM profiles"

# `user_id` は UNIQUE（1人1プロフィール、design/08-database.md §9 スキーマ）。
SELECT_BY_USER_ID = f"SELECT{_PROFILE_COLUMNS} {PROFILE_FROM} WHERE user_id = ?"

# `public_id` は UNIQUE（18文字）。公開フィルタは掛けない（編集・本人経路用）。
SELECT_BY_PUBLIC_ID = f"SELECT{_PROFILE_COLUMNS} {PROFILE_FROM} WHERE public_id = ?"

# 公開取得は published だけ（design/08-database.md §9 主要クエリ「公開取得」）。
SELECT_PUBLISHED_BY_PUBLIC_ID = (
    f"SELECT{_PROFILE_COLUMNS} {PROFILE_FROM} WHERE public_id = ? AND status = 'published'"
)

# 新規作成と既存行更新を1文で行う。`created_at` / `user_id` / `public_id` は更新対象に
# 含めない（作成時に一度だけ。design/03-domain-model.md §4 時刻の意味）。
UPSERT_PROFILE = """
INSERT INTO profiles (
  id, user_id, public_id, status, content, published_at, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  status       = excluded.status,
  content      = excluded.content,
  published_at = excluded.published_at,
  updated_at   = excluded.updated_at
"""


class SqlProfileRepository:
    """`design/03-domain-model.md` §4 Port 表の `ProfileRepository` の実 SQLite 実装。

    `UnitOfWork` の中で使う前提のクラス。`with UnitOfWork(connect(path)) as uow:` の
    `uow.connection` を渡して生成し、同じ connection に対して操作する。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_by_user_id(self, user_id: str) -> Profile | None:
        return self._fetch_one(SELECT_BY_USER_ID, (user_id,))

    def find_by_public_id(self, public_id: PublicProfileId) -> Profile | None:
        return self._fetch_one(SELECT_BY_PUBLIC_ID, (public_id.value,))

    def find_published_by_public_id(self, public_id: PublicProfileId) -> Profile | None:
        """公開中のみを返す。draft・不在は `None`（H-1 / H-2 の入力）。"""
        return self._fetch_one(SELECT_PUBLISHED_BY_PUBLIC_ID, (public_id.value,))

    def save(self, profile: Profile) -> None:
        """新規作成と既存行の更新（本文・状態・`published_at`）を UPSERT で行う。

        一意制約違反（`public_id` の UNIQUE 衝突など）は握りつぶさず
        `sqlite3.IntegrityError` のまま外へ出す（`CreateProfile` の H-8 が支点）。
        """
        self._connection.execute(
            UPSERT_PROFILE,
            (
                profile.id.value,
                profile.user_id,
                profile.public_id.value,
                profile.status.value,
                profile.content.value,
                (None if profile.published_at is None else format_timestamp(profile.published_at)),
                format_timestamp(profile.created_at),
                format_timestamp(profile.updated_at),
            ),
        )

    def _fetch_one(self, statement: str, parameters: tuple[str, ...]) -> Profile | None:
        row = self._connection.execute(statement, parameters).fetchone()
        return None if row is None else _to_profile(row)


def _to_profile(row: sqlite3.Row) -> Profile:
    """1行を `Profile` に戻す（時刻は27文字 → aware datetime、`published_at` は NULL 可）。"""
    published_at = row["published_at"]
    return Profile(
        id=ProfileId(row["id"]),
        user_id=row["user_id"],
        public_id=PublicProfileId(row["public_id"]),
        content=ProfileContent(row["content"]),
        status=ProfileStatus(row["status"]),
        published_at=None if published_at is None else parse_timestamp(published_at),
        created_at=parse_timestamp(row["created_at"]),
        updated_at=parse_timestamp(row["updated_at"]),
    )
