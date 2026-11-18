"""migration runner（`scripts/migrate.py`）と `migrations/001_initial.sql` のテスト。

一時ディレクトリの実 SQLite ファイルで、次を観測する。

1. `001_initial.sql` が design/08-database.md §9 のスキーマそのままで、全テーブルが STRICT
2. 適用済み migration の再実行でテーブル定義も `schema_migrations` も変わらない
3. 適用済み migration 本文の改変が checksum 不一致で失敗する
4. migration 途中の失敗が ROLLBACK され、`schema_migrations` に残らない
   （1 migration = 1 transaction）
5. `-- migrate: foreign_keys=off` の間だけ FK 検査が切れ、終了後に戻る
6. 未知の `-- migrate:` 指示がエラーになる
7. runner が `executescript` を使っていない

根拠: design/09-migrations.md §10 / design/08-database.md §9 /
design/14-dod-conventions.md §15。
"""

import hashlib
from pathlib import Path

import pytest

from scripts import migrate
from src.shared.db import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MIGRATIONS = REPO_ROOT / "migrations"

# design/08-database.md §9 のスキーマ（正）。normalize して比較する。
EXPECTED_SCHEMA: dict[str, str] = {
    "users": """
CREATE TABLE users (
  id          TEXT PRIMARY KEY CHECK (length(id) = 36),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
) STRICT""",
    "login_credentials": """
CREATE TABLE login_credentials (
  user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  login_id_hash       TEXT NOT NULL UNIQUE CHECK (length(login_id_hash) = 64),
  password_hash       TEXT NOT NULL,
  password_changed_at TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
) STRICT""",
    "recovery_credentials": """
CREATE TABLE recovery_credentials (
  user_id            TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  recovery_key_hash  TEXT NOT NULL UNIQUE CHECK (length(recovery_key_hash) = 64),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
) STRICT""",
    "sessions": """
CREATE TABLE sessions (
  id           TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  CHECK (expires_at > created_at)
) STRICT""",
    "ix_sessions_user_active": (
        "CREATE INDEX ix_sessions_user_active ON sessions(user_id) WHERE revoked_at IS NULL"
    ),
    "profiles": """
CREATE TABLE profiles (
  id             TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id        TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  public_id      TEXT NOT NULL UNIQUE CHECK (length(public_id) = 18),
  status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
  content        TEXT NOT NULL DEFAULT '' CHECK (length(content) <= 100000),
  published_at   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  CHECK ((status = 'published') = (published_at IS NOT NULL)),
  CHECK (status = 'draft' OR length(content) > 0)
) STRICT""",
}

TABLE_NAMES = {"users", "login_credentials", "recovery_credentials", "sessions", "profiles"}


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


def normalize(sql: str) -> str:
    """空白の違いを無視して DDL の内容だけを比較する。"""
    return " ".join(sql.split()).rstrip(";").strip()


def read_schema(db_path: Path) -> dict[str, str]:
    """アプリのスキーマオブジェクト（管理表を除く）を名前→`sqlite_master.sql` で返す。"""
    connection = connect(db_path)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations'"
        ).fetchall()
        return {row["name"]: row["sql"] for row in rows}
    finally:
        connection.close()


def read_user_objects(db_path: Path) -> set[str]:
    """管理表を除く、DB に存在するオブジェクト名の集合。"""
    connection = connect(db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations'"
        ).fetchall()
        return {row["name"] for row in rows}
    finally:
        connection.close()


def migration_rows(db_path: Path) -> list[tuple[object, ...]]:
    connection = connect(db_path)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def make_migrations_dir(tmp_path: Path, filename: str, sql: str) -> Path:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / filename).write_text(sql, encoding="utf-8")
    return migrations_dir


def test_empty_db_gets_the_design_schema(db_path: Path) -> None:
    assert migrate.run(db_path, REAL_MIGRATIONS) == 0

    schema = read_schema(db_path)
    assert set(schema) == set(EXPECTED_SCHEMA)
    for name, expected in EXPECTED_SCHEMA.items():
        assert normalize(schema[name]) == normalize(expected), name

    rows = migration_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "initial"
    expected_checksum = hashlib.sha256(
        (REAL_MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    assert rows[0][2] == expected_checksum


def test_every_table_is_strict(db_path: Path) -> None:
    assert migrate.run(db_path, REAL_MIGRATIONS) == 0

    connection = connect(db_path)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()

    names = {row["name"] for row in rows}
    assert names == TABLE_NAMES | {"schema_migrations"}
    for row in rows:
        assert "STRICT" in row["sql"], row["name"]


def test_reapplying_an_applied_migration_changes_nothing(db_path: Path) -> None:
    assert migrate.run(db_path, REAL_MIGRATIONS) == 0
    before_schema = read_schema(db_path)
    before_rows = migration_rows(db_path)

    assert migrate.run(db_path, REAL_MIGRATIONS) == 0
    assert read_schema(db_path) == before_schema
    assert migration_rows(db_path) == before_rows


def test_modified_applied_migration_fails_the_checksum(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = make_migrations_dir(
        tmp_path,
        "001_initial.sql",
        (REAL_MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8"),
    )
    assert migrate.run(db_path, migrations_dir) == 0

    path = migrations_dir / "001_initial.sql"
    edited = path.read_text(encoding="utf-8") + "\n-- edited after apply\n"
    path.write_text(edited, encoding="utf-8")

    assert migrate.run(db_path, migrations_dir) != 0
    # 失敗しても適用済みの記録は書き換わらない
    assert len(migration_rows(db_path)) == 1


def test_failed_statement_rolls_back_the_whole_migration(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = make_migrations_dir(
        tmp_path,
        "001_broken.sql",
        "CREATE TABLE first (id TEXT PRIMARY KEY) STRICT;\n"
        "CREATE TABLE second (id TEXT PRIMARY KEY) STRICT;\n"
        "THIS IS NOT VALID SQL;\n",
    )

    assert migrate.run(db_path, migrations_dir) != 0
    assert read_user_objects(db_path) == set()
    assert migration_rows(db_path) == []


def test_foreign_keys_off_migration_succeeds_and_restores_the_pragma(db_path: Path) -> None:
    # FK OFF でなければ `DELETE FROM parent` が FK 制約で失敗する。成功＝実行中は OFF。
    sql = (
        "-- migrate: foreign_keys=off\n"
        "CREATE TABLE parent (id TEXT PRIMARY KEY) STRICT;\n"
        "CREATE TABLE child (id TEXT PRIMARY KEY, pid TEXT REFERENCES parent(id)) STRICT;\n"
        "INSERT INTO parent (id) VALUES ('p');\n"
        "INSERT INTO child (id, pid) VALUES ('c', 'p');\n"
        "DELETE FROM parent WHERE id = 'p';\n"
        "INSERT INTO parent (id) VALUES ('p');\n"
    )
    connection = connect(db_path)
    try:
        connection.execute(migrate.SCHEMA_MIGRATIONS_DDL)
        migrate.apply_migration(
            connection,
            version=1,
            name="recreate",
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            sql=sql,
            applied_at="2026-10-04T00:00:00.000000Z",
        )
        # finally で PRAGMA foreign_keys=ON に戻っている
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_foreign_key_check_rejects_an_orphan(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = make_migrations_dir(
        tmp_path,
        "001_orphan.sql",
        "-- migrate: foreign_keys=off\n"
        "CREATE TABLE parent (id TEXT PRIMARY KEY) STRICT;\n"
        "CREATE TABLE child (id TEXT PRIMARY KEY, pid TEXT REFERENCES parent(id)) STRICT;\n"
        "INSERT INTO child (id, pid) VALUES ('c', 'missing');\n",
    )

    assert migrate.run(db_path, migrations_dir) != 0
    assert read_user_objects(db_path) == set()
    assert migration_rows(db_path) == []


def test_unknown_directive_is_rejected(tmp_path: Path, db_path: Path) -> None:
    migrations_dir = make_migrations_dir(
        tmp_path,
        "001_weird.sql",
        "-- migrate: something=else\nCREATE TABLE t (id TEXT PRIMARY KEY) STRICT;\n",
    )

    assert migrate.run(db_path, migrations_dir) != 0
    assert read_user_objects(db_path) == set()


def test_migrations_are_discovered_in_version_order(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "010_b.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (migrations_dir / "002_a.sql").write_text("SELECT 1;\n", encoding="utf-8")

    discovered = migrate.discover_migrations(migrations_dir)
    assert [migration.version for migration in discovered] == [2, 10]
    assert [migration.name for migration in discovered] == ["a", "b"]


def test_runner_does_not_use_executescript() -> None:
    source = (REPO_ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert ".executescript(" not in source
