"""`SessionRepository` の実 SQLite 実装（design/03-domain-model.md §4 Port）。

根拠: design/03-domain-model.md §4 Port / design/08-database.md §9。

- `UnitOfWork` と同じ `sqlite3.Connection` を受け取る。transaction の開始・終了は
  このクラスでは行わない（`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` は `UnitOfWork`
  だけ。design/10-infra-ops.md §11）。接続はリクエスト単位。
- 全SQLは parameterized。値を SQL 文字列へ埋め込まない（design/08-database.md §9 主要クエリ）。
- 生の秘密値は保存しない。`token_hash` だけ
  （design/08-database.md §9「秘密値は `*_hash` のみ保存」）。
- Session の有効性はドメイン（`Session.is_valid`）で判定し、Repository は行の読み書きだけを行う。
"""

import sqlite3
from datetime import datetime

from src.identity.domain.entities import LAST_SEEN_INTERVAL, Session
from src.identity.domain.value_objects import UserId
from src.shared.clock.clock import format_timestamp, parse_timestamp

# SELECT の列は Entity の属性と1対1（design/03-domain-model.md §4 Entity Session）。
_SESSION_COLUMNS = """
  id           AS id,
  user_id      AS user_id,
  token_hash   AS token_hash,
  created_at   AS created_at,
  last_seen_at AS last_seen_at,
  expires_at   AS expires_at,
  revoked_at   AS revoked_at
"""

# `token_hash` は UNIQUE（design/08-database.md §9 スキーマ）なので1列で引き当てられる。
SELECT_BY_TOKEN_HASH = f"SELECT{_SESSION_COLUMNS} FROM sessions WHERE token_hash = ?"

# 新規作成と既存行更新（失効）を1文で行う。`created_at` は更新対象に含めない
# （作成時に一度だけ。design/03-domain-model.md §4 時刻の意味）。`user_id` と
# `token_hash` は Session の識別なので差し替えない。
UPSERT_SESSION = """
INSERT INTO sessions (
  id, user_id, token_hash, created_at, last_seen_at, expires_at, revoked_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  last_seen_at = excluded.last_seen_at,
  expires_at   = excluded.expires_at,
  revoked_at   = excluded.revoked_at
"""

# 閾値＝現在−5分（design/08-database.md §9 touch）。`last_seen_at` は固定長27文字の
# TEXT なので、文字列比較で時刻順と一致する。行が更新されたときだけ Entity に反映する。
TOUCH = "UPDATE sessions SET last_seen_at = ? WHERE id = ? AND last_seen_at <= ?"

# 未失効の行だけを対象にする（design/08-database.md §9 全Session失効）。
REVOKE_ALL_FOR_USER = "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"


class SqlSessionRepository:
    """`design/03-domain-model.md` §4 Port 表の `SessionRepository` の実 SQLite 実装。

    `UnitOfWork` の中で使う前提のクラス。`with UnitOfWork(connect(path)) as uow:` の
    `uow.connection` を渡して生成し、同じ connection に対して操作する。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_by_token_hash(self, token_hash: str) -> Session | None:
        """`sessions.token_hash` の UNIQUE 索引を引く（§9 Session認証）。

        不在は例外にせず `None` を返す。有効性は呼び出し側のドメイン判定に委ねる。
        """
        row = self._connection.execute(SELECT_BY_TOKEN_HASH, (token_hash,)).fetchone()
        return None if row is None else _to_session(row)

    def save(self, session: Session) -> None:
        """新規作成と既存 Session の更新（失効・touch反映）を UPSERT で行う。

        一意制約違反（`token_hash` の UNIQUE 衝突）は握りつぶさず
        `sqlite3.IntegrityError` のまま外へ出す。
        """
        self._connection.execute(
            UPSERT_SESSION,
            (
                session.id.value,
                session.user_id.value,
                session.token_hash,
                format_timestamp(session.created_at),
                format_timestamp(session.last_seen_at),
                format_timestamp(session.expires_at),
                None if session.revoked_at is None else format_timestamp(session.revoked_at),
            ),
        )

    def touch(self, session: Session, now: datetime) -> None:
        """前回から5分以上経過した時だけ `last_seen_at` を更新する（§4 時刻の意味）。

        失効済み・期限切れはドメイン（`Session.is_valid`）で先に弾く。更新できた行が
        あるときだけ Entity にも反映する（§9 touch）。
        """
        if not session.is_valid(now):
            return
        cursor = self._connection.execute(
            TOUCH,
            (
                format_timestamp(now),
                session.id.value,
                format_timestamp(now - LAST_SEEN_INTERVAL),
            ),
        )
        if cursor.rowcount == 1:
            session.last_seen_at = now

    def revoke_all_for_user(self, user_id: UserId, now: datetime) -> None:
        """対象 user の未失効 Session だけを一括失効する（§9 全Session失効）。

        他 user と失効済みの行には触れない。呼び出し側は Entity を持っていないので、
        失効時刻の反映はこの UPDATE のみで完結する。
        """
        self._connection.execute(
            REVOKE_ALL_FOR_USER,
            (format_timestamp(now), user_id.value),
        )


def _to_session(row: sqlite3.Row) -> Session:
    """1行を `Session` に戻す（時刻は27文字 → aware datetime、`revoked_at` は NULL 可）。"""
    revoked_at = row["revoked_at"]
    return Session(
        id=UserId(row["id"]),
        user_id=UserId(row["user_id"]),
        token_hash=row["token_hash"],
        created_at=parse_timestamp(row["created_at"]),
        last_seen_at=parse_timestamp(row["last_seen_at"]),
        expires_at=parse_timestamp(row["expires_at"]),
        revoked_at=None if revoked_at is None else parse_timestamp(revoked_at),
    )
