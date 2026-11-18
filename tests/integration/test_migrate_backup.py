"""migration の適用前バックアップと保持整理（`scripts/migrate.py`）のテスト。

design/09-migrations.md §10「適用前バックアップと復旧」/ design/10-infra-ops.md §11「バックアップ」
のうち、migration テスト層が担う範囲を一時ディレクトリの実 SQLite ファイルで観測する。

1. 未適用 migration があるとき、適用前に `pre-migrate-v<前>-to-v<後>-<ts>.db` を取得する
2. バックアップは適用前のデータを含む（`.backup` API）
3. `ENV=prod` はバックアップ失敗で適用しない（fail-closed）
4. `ENV=dev`/`test` はバックアップ失敗でも適用を続ける
5. 未適用が無ければ新しいバックアップを作らない
6. 成功直後に `pre-migrate-*` だけを、新しい順に3世代・30日以内へ整理する
7. 失敗時は整理しない
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import migrate
from src.shared.db import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MIGRATIONS = REPO_ROOT / "migrations"

FIXED_NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
RUN_BACKUP = "pre-migrate-v0-to-v1-20261010-120000.db"

VALID_MIGRATION = "CREATE TABLE t (id TEXT PRIMARY KEY) STRICT;\n"
BROKEN_MIGRATION = "THIS IS NOT VALID SQL;\n"


class FixedClock:
    """固定時刻を返す `Clock`（design/03-domain-model.md §4 Port）。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


def write_migration(migrations_dir: Path, filename: str, sql: str) -> Path:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    path = migrations_dir / filename
    path.write_text(sql, encoding="utf-8")
    return path


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


def backup_files(db_path: Path) -> list[str]:
    return sorted(path.name for path in migrate.backup_dir_for(db_path).glob("pre-migrate-*.db"))


def seed_marker(db_path: Path) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("CREATE TABLE marker (id TEXT PRIMARY KEY) STRICT")
        connection.execute("INSERT INTO marker (id) VALUES ('before')")
        connection.commit()
    finally:
        connection.close()


def test_backup_is_created_with_the_design_name(db_path: Path) -> None:
    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="test") == 0

    assert backup_files(db_path) == [RUN_BACKUP]


def test_backup_contains_pre_apply_data(db_path: Path) -> None:
    seed_marker(db_path)

    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="test") == 0

    backup = migrate.backup_dir_for(db_path) / RUN_BACKUP
    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT id FROM marker").fetchone() == ("before",)
    finally:
        connection.close()


def test_prod_backup_failure_aborts_before_apply(tmp_path: Path, db_path: Path) -> None:
    (tmp_path / migrate.BACKUP_DIR_NAME).write_text("not a directory", encoding="utf-8")

    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="prod") != 0
    assert read_user_objects(db_path) == set()


def test_dev_backup_failure_still_applies(tmp_path: Path, db_path: Path) -> None:
    (tmp_path / migrate.BACKUP_DIR_NAME).write_text("not a directory", encoding="utf-8")

    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="dev") == 0
    assert "users" in read_user_objects(db_path)


def test_no_backup_when_nothing_is_pending(db_path: Path) -> None:
    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="test") == 0
    for path in migrate.backup_dir_for(db_path).glob("pre-migrate-*.db"):
        path.unlink()

    assert migrate.run(db_path, REAL_MIGRATIONS, clock=FixedClock(FIXED_NOW), env="test") == 0
    assert backup_files(db_path) == []


def test_cleanup_keeps_only_the_newest_three(tmp_path: Path, db_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, "001_t.sql", VALID_MIGRATION)
    backup_dir = migrate.backup_dir_for(db_path)
    backup_dir.mkdir()
    for stamp in ["20261001-000000", "20261002-000000", "20261003-000000", "20261004-000000"]:
        (backup_dir / f"pre-migrate-v0-to-v1-{stamp}.db").write_bytes(b"")

    assert migrate.run(db_path, migrations, clock=FixedClock(FIXED_NOW), env="test") == 0

    assert backup_files(db_path) == [
        "pre-migrate-v0-to-v1-20261003-000000.db",
        "pre-migrate-v0-to-v1-20261004-000000.db",
        RUN_BACKUP,
    ]


def test_cleanup_removes_backups_older_than_thirty_days(tmp_path: Path, db_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, "001_t.sql", VALID_MIGRATION)
    backup_dir = migrate.backup_dir_for(db_path)
    backup_dir.mkdir()
    (backup_dir / "pre-migrate-v0-to-v1-20260801-000000.db").write_bytes(b"")
    (backup_dir / "pre-migrate-v0-to-v1-20261009-000000.db").write_bytes(b"")

    assert migrate.run(db_path, migrations, clock=FixedClock(FIXED_NOW), env="test") == 0

    assert backup_files(db_path) == [
        "pre-migrate-v0-to-v1-20261009-000000.db",
        RUN_BACKUP,
    ]


def test_cleanup_targets_only_pre_migrate_files(tmp_path: Path, db_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, "001_t.sql", VALID_MIGRATION)
    backup_dir = migrate.backup_dir_for(db_path)
    backup_dir.mkdir()
    (backup_dir / "daily-backup.db").write_bytes(b"")
    (backup_dir / "pre-migrate-notes.txt").write_bytes(b"")
    old = backup_dir / "pre-migrate-v0-to-v1-20260801-000000.db"
    old.write_bytes(b"")

    assert migrate.run(db_path, migrations, clock=FixedClock(FIXED_NOW), env="test") == 0

    assert (backup_dir / "daily-backup.db").exists()
    assert (backup_dir / "pre-migrate-notes.txt").exists()
    assert not old.exists()


def test_cleanup_does_not_run_when_migration_fails(tmp_path: Path, db_path: Path) -> None:
    migrations = tmp_path / "migrations"
    write_migration(migrations, "001_broken.sql", BROKEN_MIGRATION)
    backup_dir = migrate.backup_dir_for(db_path)
    backup_dir.mkdir()
    old = backup_dir / "pre-migrate-v0-to-v1-20260801-000000.db"
    old.write_bytes(b"")

    assert migrate.run(db_path, migrations, clock=FixedClock(FIXED_NOW), env="test") != 0
    assert old.exists()
