"""`shared/db` のテスト（一時ディレクトリの実 SQLite ファイル）。

このファイルが守るべき設計上の約束は次の4点。

1. 接続ごとに `journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000` を設定し、
   `isolation_level=None` で sqlite3 の自動 transaction を持たせない
   （design/08-database.md §9 共通規約 / design/10-infra-ops.md §11）。
2. transaction は `UnitOfWork` の `BEGIN IMMEDIATE` だけで始める。書き込みの前、
   つまりコンテキストに入った時点でロックを取り、COMMIT まで他の接続から見えない
   （design/03-domain-model.md §4 Port 表 / design/10-infra-ops.md §11）。
3. 正常終了で COMMIT、例外で ROLLBACK、どちらでも接続は閉じる。
4. busy（`sqlite3.OperationalError`）は変換せず呼び出し元へ渡す。503 への変換は M07 の責務。

`migrations/001_initial.sql` はまだ無い（T-02-05）ため、テーブルはこのファイルで
一時DBに作る。本タスクのテストは接続と transaction 境界だけを対象にする。
"""

import sqlite3
from pathlib import Path

import pytest

from src.shared.db import BUSY_TIMEOUT_MS, UnitOfWork, connect

# 本番の busy_timeout（5000ms）のまま待つとテストが5秒止まるので、観測したい busy が
# タイムアウトまで引っかからないよう短くした値。
SHORT_BUSY_TIMEOUT_MS = 100

# 本物のスキーマが未着手の間の代替。STRICT と foreign key を使う実テーブルと同じ形にする
# （design/08-database.md §9「全テーブル STRICT」）。
CREATE_ITEMS = """
CREATE TABLE items (
  id    TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT
"""


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """テーブルを1つだけ持つ一時DBのパス。"""
    path = tmp_path / "app.db"
    connection = connect(path)
    connection.execute(CREATE_ITEMS)
    connection.close()
    return path


def assert_closed(connection: sqlite3.Connection) -> None:
    """接続が閉じていること（閉じた後に呼ぶと `ProgrammingError`）を確認する。"""
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def fetch_ids(connection: sqlite3.Connection) -> list[str]:
    """コミット済みの行を読み出す。`row_factory` が `sqlite3.Row` であることもここで見る。"""
    rows = connection.execute("SELECT id FROM items ORDER BY id").fetchall()
    return [row["id"] for row in rows]


def test_connect_applies_the_three_pragmas(db_path: Path) -> None:
    """接続ごとに3つの PRAGMA が設定されること（design/08-database.md §9 共通規約）。

    3つの値は設計書に書かれたそのままの数字。`journal_mode` はDBファイルに永続化され、
    `foreign_keys` と `busy_timeout` は接続ごとの設定なので、新規接続のたびに効くかを見る。
    """
    connection = connect(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert BUSY_TIMEOUT_MS == 5000
    finally:
        connection.close()


def test_connect_disables_sqlite_automatic_transaction(db_path: Path) -> None:
    """`isolation_level` が `None` で、sqlite3 が暗黙の transaction を持たないこと。

    design/10-infra-ops.md §11「`isolation_level=None`で自前に制御」。自動 transaction が
    残ると `BEGIN IMMEDIATE` の前に別の `BEGIN` が挟まり、ロックを取得した瞬間を
    呼び出し側が管理できなくなる。書き込み直後に `in_transaction` が False のままなことも
    あわせて確かめる。
    """
    connection = connect(db_path)
    try:
        assert connection.isolation_level is None
        connection.execute("INSERT INTO items (id, value) VALUES ('committed', 'x')")
        assert connection.in_transaction is False
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    finally:
        connection.close()


def test_connect_returns_rows_with_named_columns(db_path: Path) -> None:
    """行を列名で読めること。

    Repository は `SELECT` の列の並び順に依存してはいけない（design/08-database.md §9
    主要クエリは列を明示した parameterized が中心）。`row_factory` が `sqlite3.Row` で
    あることを、接続を閉じた後もオブジェクトとしては使える状態で確認する。
    """
    connection = connect(db_path)
    connection.execute("INSERT INTO items (id, value) VALUES ('a', 'alpha')")
    connection.close()

    reader = connect(db_path)
    try:
        row = reader.execute("SELECT id, value FROM items WHERE id = ?", ("a",)).fetchone()
        assert row["id"] == "a"
        assert row["value"] == "alpha"
    finally:
        reader.close()


def test_uow_takes_the_write_lock_before_any_statement(db_path: Path) -> None:
    """`BEGIN IMMEDIATE` が書き込みの前からロックを取ること（design/10-infra-ops.md §11）。

    遅延ロック（通常の `BEGIN`）なら、まだ何も書いていない間は他接続が `BEGIN IMMEDIATE`
    に成功してしまう。ここではコンテキストに入った直後・どの statement も実行していない
    時点で、2つ目の接続の `BEGIN IMMEDIATE` が busy で落ちることを確認する。
    """
    with UnitOfWork(connect(db_path)) as uow:
        assert uow.connection.in_transaction is True

        other = connect(db_path)
        try:
            other.execute(f"PRAGMA busy_timeout={SHORT_BUSY_TIMEOUT_MS}")
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_uow_hides_uncommitted_writes_from_other_connections(db_path: Path) -> None:
    """未コミットの変更が他接続から見えないこと（終了条件「別接続から読むと変更が見えない」）。

    WAL では読取りは書込みロックを妨げないため、他接続は（古いスナップショットを）
    普通に読める。読めた結果に未コミットの行が混ざっていないことが、transaction 境界が
    効いている証拠になる。
    """
    with UnitOfWork(connect(db_path)) as uow:
        uow.connection.execute("INSERT INTO items (id, value) VALUES ('a', 'x')")

        other = connect(db_path)
        try:
            assert fetch_ids(other) == []
        finally:
            other.close()

    reader = connect(db_path)
    try:
        assert fetch_ids(reader) == ["a"]
    finally:
        reader.close()


def test_uow_commits_on_success_and_closes_the_connection(db_path: Path) -> None:
    """正常終了で COMMIT され、コンテキストを抜けば接続が閉じられること。

    「閉じる」はリクエスト単位の接続（design/10-infra-ops.md §11）を確実に返すためで、
    閉じ忘れはファイルハンドラの取りっぱぐれに直結する。
    """
    connection = connect(db_path)
    with UnitOfWork(connection) as uow:
        assert uow.connection is connection
        uow.connection.execute("INSERT INTO items (id, value) VALUES ('a', 'x')")
        assert uow.connection.in_transaction is True

    # 閉じた後の接続では属性も読めないため、閉じたことは `assert_closed` で確かめる
    assert_closed(connection)

    reader = connect(db_path)
    try:
        assert fetch_ids(reader) == ["a"]
    finally:
        reader.close()


def test_uow_rolls_back_on_exception_and_passes_it_through(db_path: Path) -> None:
    """例外発生で ROLLBACK され、例外はそのまま外へ出て、接続は閉じられること。

    例外を握りつぶすと呼び出し元（M07 のエラーハンドラー）が失敗を知らず、
    「失敗したのにコミットされた」状態が作れる。伝播とロールバックはセットで見る。
    """
    connection = connect(db_path)
    with pytest.raises(RuntimeError, match="boom"):
        with UnitOfWork(connection) as uow:
            uow.connection.execute("INSERT INTO items (id, value) VALUES ('a', 'x')")
            raise RuntimeError("boom")

    assert_closed(connection)

    reader = connect(db_path)
    try:
        assert fetch_ids(reader) == []
    finally:
        reader.close()


def test_busy_error_propagates_unchanged_and_closes_the_connection(db_path: Path) -> None:
    """busy は `sqlite3.OperationalError` のまま呼び出し元へ出ること（M07 で 503 に変換）。

    1つ目の transaction がロックを持っている間に2つ目の `UnitOfWork` を開くと
    `BEGIN IMMEDIATE` が busy で失敗する。このとき変換・握りつぶしはせず、
    かつ開始に失敗した接続も閉じる（開けなかったコンテキストで残さない）。
    """
    with UnitOfWork(connect(db_path)):
        other = connect(db_path)
        other.execute(f"PRAGMA busy_timeout={SHORT_BUSY_TIMEOUT_MS}")

        with pytest.raises(sqlite3.OperationalError):
            with UnitOfWork(other):
                pytest.fail("BEGIN IMMEDIATE は busy で失敗するはず")

        assert_closed(other)
