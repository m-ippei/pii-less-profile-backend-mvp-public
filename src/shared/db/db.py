"""SQLite 接続と transaction 境界（design/08-database.md §9 / design/10-infra-ops.md §11）。

- 接続ごとに `journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000` を設定する
  （design/08-database.md §9 共通規約）
- `isolation_level=None` で sqlite3 の自動 transaction を持たず、transaction は
  `UnitOfWork` の `BEGIN IMMEDIATE` だけで始める。書き込みは開始時点でロックを取り、
  COMMIT まで他接続から見えない（design/10-infra-ops.md §11）
- busy（`sqlite3.OperationalError`）は変換せず呼び出し元へ渡す。503 への変換は M07 の責務
- 接続はリクエスト単位。`UnitOfWork` はコンテキストを抜けば必ず閉じる
"""

import sqlite3
from pathlib import Path
from types import TracebackType

BUSY_TIMEOUT_MS = 5000
JOURNAL_MODE = "WAL"
BEGIN_STATEMENT = "BEGIN IMMEDIATE"
COMMIT_STATEMENT = "COMMIT"
ROLLBACK_STATEMENT = "ROLLBACK"


def connect(path: str | Path) -> sqlite3.Connection:
    """DBファイルへの接続を作る。

    自動 transaction を持たせない（`isolation_level=None`）ため、transaction の開始・
    終了は `UnitOfWork` だけが行う。PRAGMA は接続ごとに効くので、ここで必ず設定する。
    """
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


class UnitOfWork:
    """`BEGIN IMMEDIATE` で始めて、成功なら COMMIT / 失敗なら ROLLBACK する transaction 境界。

    `design/03-domain-model.md` §4 Port 表の `UnitOfWork`。呼び出し側は
    `with UnitOfWork(connect(path)) as uow:` と書き、`uow.connection` を Repository に渡す。
    コンテキストを抜けば接続を閉じるため、接続の所有権はこのオブジェクトにある。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        """transaction の中でだけ使ってよい接続。"""
        return self._connection

    def __enter__(self) -> UnitOfWork:
        # 開始失敗（busy など）でも接続は閉じる。開けなかったコンテキストの接続を
        # 呼び出し元に残さないため。
        try:
            self._connection.execute(BEGIN_STATEMENT)
        except BaseException:
            self._connection.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is None:
                self._connection.execute(COMMIT_STATEMENT)
            else:
                # 例外を握りつぶさない。ROLLBACK の失敗は例外として上へ持ち上げられる。
                self._connection.execute(ROLLBACK_STATEMENT)
        finally:
            # COMMIT/ROLLBACK の成否に関わらず閉じる（閉じ忘れはリクエスト毎に漏れる）。
            self._connection.close()
        return False
