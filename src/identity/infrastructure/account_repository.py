"""`AccountRepository` の実 SQLite 実装（design/03-domain-model.md §4 Port）。

根拠: design/03-domain-model.md §4 Port / design/08-database.md §9。

- `UnitOfWork` と同じ `sqlite3.Connection` を受け取る。transaction の開始・終了は
  このクラスでは行わない（`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` は `UnitOfWork` だけ。
  design/10-infra-ops.md §11）。接続はリクエスト単位。
- 全SQLは parameterized。値を SQL 文字列へ埋め込まない（design/08-database.md §9 主要クエリ）。
- 生の秘密値は保存しない。`login_id_hash` / `recovery_key_hash` / `password_hash` だけ
  （design/08-database.md §9「秘密値は `*_hash` のみ保存」）。
"""

import sqlite3

from src.identity.domain.entities import Account, LoginCredential, RecoveryCredential, User
from src.identity.domain.value_objects import UserId
from src.shared.clock.clock import format_timestamp, parse_timestamp

# Aggregate は User + LoginCredential + RecoveryCredential の3点セット（§4）。
# SELECT の列はすべての取得経路で共通なので1箇所にまとめ、WHERE だけを切り替える。
_ACCOUNT_COLUMNS = """
  u.id                   AS user_id,
  u.created_at           AS user_created_at,
  u.updated_at           AS user_updated_at,
  lc.login_id_hash       AS login_id_hash,
  lc.password_hash       AS password_hash,
  lc.password_changed_at AS password_changed_at,
  lc.created_at          AS login_created_at,
  lc.updated_at          AS login_updated_at,
  rc.recovery_key_hash   AS recovery_key_hash,
  rc.created_at          AS recovery_created_at,
  rc.updated_at          AS recovery_updated_at
"""

# 資格情報が欠けた行は `Account` として成立しないので INNER JOIN にする（戻り値は None）。
_ACCOUNT_FROM = """
FROM users u
JOIN login_credentials lc ON lc.user_id = u.id
JOIN recovery_credentials rc ON rc.user_id = u.id
"""

SELECT_BY_USER_ID = f"SELECT{_ACCOUNT_COLUMNS}{_ACCOUNT_FROM}WHERE u.id = ?"
SELECT_BY_LOGIN_ID_HASH = f"SELECT{_ACCOUNT_COLUMNS}{_ACCOUNT_FROM}WHERE lc.login_id_hash = ?"
SELECT_BY_RECOVERY_KEY_HASH = (
    f"SELECT{_ACCOUNT_COLUMNS}{_ACCOUNT_FROM}WHERE rc.recovery_key_hash = ?"
)

# `users` には更新すべき属性がない（`updated_at` は作成時に一度だけ。§4 時刻の意味）。
# 既にあれば書き換えないので、noop な save が `created_at` を壊さない。
INSERT_USER = """
INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)
ON CONFLICT(id) DO NOTHING
"""

# 競合対象は `user_id`。`login_id_hash` の UNIQUE 衝突は UPSERT では吸収されず IntegrityError
# のまま上がるので、M06 の一意衝突再生成（H-8）が支点にできる。
# `created_at` は更新対象に含めない（作成時に一度だけ。§4 時刻の意味）。
UPSERT_LOGIN_CREDENTIAL = """
INSERT INTO login_credentials (
  user_id, login_id_hash, password_hash, password_changed_at, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
  login_id_hash       = excluded.login_id_hash,
  password_hash       = excluded.password_hash,
  password_changed_at = excluded.password_changed_at,
  updated_at          = excluded.updated_at
"""

# `recovery_credentials` は1ユーザー1行。password変更・回復のたびに同じ行を差し替える
# （design/08-database.md §9 設計メモ）。INSERT は増やさず UPDATE だけを行う。
UPSERT_RECOVERY_CREDENTIAL = """
INSERT INTO recovery_credentials (user_id, recovery_key_hash, created_at, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
  recovery_key_hash = excluded.recovery_key_hash,
  updated_at        = excluded.updated_at
"""


class SqlAccountRepository:
    """`design/03-domain-model.md` §4 Port 表の `AccountRepository` の実 SQLite 実装。

    `UnitOfWork` の中で使う前提のクラス。`with UnitOfWork(connect(path)) as uow:` の
    `uow.connection` を渡して生成し、同じ connection に対して操作する。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_by_id(self, user_id: UserId) -> Account | None:
        return self._fetch_one(SELECT_BY_USER_ID, (user_id.value,))

    def find_by_login_id_hash(self, login_id_hash: str) -> Account | None:
        """`login_credentials.login_id_hash` の UNIQUE 索引を引く（§9 ログイン照合）。

        不在でも例外にせず `None` を返す。呼び出し側がダミーArgon2検証を実行して
        処理時間を揃えるため（H-3 / H-4）。
        """
        return self._fetch_one(SELECT_BY_LOGIN_ID_HASH, (login_id_hash,))

    def find_by_recovery_key_hash(self, recovery_key_hash: str) -> Account | None:
        """`recovery_credentials.recovery_key_hash` の UNIQUE 索引を引く（§9 回復用検索）。"""
        return self._fetch_one(SELECT_BY_RECOVERY_KEY_HASH, (recovery_key_hash,))

    def save(self, account: Account) -> None:
        """3行を書き込む。子（資格情報）は親（`users`）の後、同じ transaction 内で。

        既存の Account なら UPSERT で同じ行を差し替える。一意制約違反は握りつぶさず
        `sqlite3.IntegrityError` のまま外へ出す（M06 の再生成が支点）。
        """
        user = account.user
        self._connection.execute(
            INSERT_USER,
            (
                user.id.value,
                format_timestamp(user.created_at),
                format_timestamp(user.updated_at),
            ),
        )

        login_credential = account.login_credential
        self._connection.execute(
            UPSERT_LOGIN_CREDENTIAL,
            (
                login_credential.user_id.value,
                login_credential.login_id_hash,
                login_credential.password_hash,
                format_timestamp(login_credential.password_changed_at),
                format_timestamp(login_credential.created_at),
                format_timestamp(login_credential.updated_at),
            ),
        )

        recovery_credential = account.recovery_credential
        self._connection.execute(
            UPSERT_RECOVERY_CREDENTIAL,
            (
                recovery_credential.user_id.value,
                recovery_credential.recovery_key_hash,
                format_timestamp(recovery_credential.created_at),
                format_timestamp(recovery_credential.updated_at),
            ),
        )

    def _fetch_one(self, statement: str, parameters: tuple[str, ...]) -> Account | None:
        row = self._connection.execute(statement, parameters).fetchone()
        return None if row is None else _to_account(row)


def _to_account(row: sqlite3.Row) -> Account:
    """1行を `Account` Aggregate に戻す（時刻は27文字 → aware datetime）。"""
    user_id = UserId(row["user_id"])
    return Account(
        user=User(
            id=user_id,
            created_at=parse_timestamp(row["user_created_at"]),
            updated_at=parse_timestamp(row["user_updated_at"]),
        ),
        login_credential=LoginCredential(
            user_id=user_id,
            login_id_hash=row["login_id_hash"],
            password_hash=row["password_hash"],
            password_changed_at=parse_timestamp(row["password_changed_at"]),
            created_at=parse_timestamp(row["login_created_at"]),
            updated_at=parse_timestamp(row["login_updated_at"]),
        ),
        recovery_credential=RecoveryCredential(
            user_id=user_id,
            recovery_key_hash=row["recovery_key_hash"],
            created_at=parse_timestamp(row["recovery_created_at"]),
            updated_at=parse_timestamp(row["recovery_updated_at"]),
        ),
    )
