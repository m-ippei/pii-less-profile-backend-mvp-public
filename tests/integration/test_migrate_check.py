"""`make migrate-check SRC=<backup>`（`scripts/migrate_check.py`）のテスト。

design/09-migrations.md §10「`make migrate-check SRC=<バックアップ>`」を一時ディレクトリの
実 SQLite ファイルで観測する。

1. バックアップを複製して未適用 migration を適用し、`integrity_check`・`foreign_key_check`・
   テーブル別行数を表示する
2. 元のバックアップ（本番DB相当）には書き込まない
3. 行数はデータを反映する
4. `SRC` が無いのは非0
"""

import sqlite3
from io import StringIO
from pathlib import Path

from scripts import migrate, migrate_check
from src.shared.db import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MIGRATIONS = REPO_ROOT / "migrations"

USER_ID = "00000000-0000-0000-0000-000000000001"
TIMESTAMP = "2026-10-10T00:00:00.000000Z"


def make_backup(tmp_path: Path) -> Path:
    src = tmp_path / "backup.db"
    connection = sqlite3.connect(str(src))
    connection.execute("CREATE TABLE marker (id TEXT PRIMARY KEY) STRICT")
    connection.commit()
    connection.close()
    return src


def migrate_backup(tmp_path: Path) -> Path:
    src = tmp_path / "backup.db"
    assert migrate.run(src, REAL_MIGRATIONS, env="test") == 0
    return src


def test_applies_pending_migrations_to_the_copy_and_reports(tmp_path: Path) -> None:
    src = make_backup(tmp_path)

    out = StringIO()
    assert migrate_check.run(src, out=out) == 0

    text = out.getvalue()
    assert "applied: 001_initial.sql" in text
    assert "integrity_check: ok" in text
    assert "foreign_key_check: 0 violation(s)" in text
    assert "users: 0" in text
    assert "schema_migrations: 1" in text


def test_src_backup_is_not_modified(tmp_path: Path) -> None:
    src = migrate_backup(tmp_path)
    before = src.read_bytes()

    out = StringIO()
    assert migrate_check.run(src, out=out) == 0

    assert src.read_bytes() == before
    assert not (tmp_path / "backup.db-wal").exists()
    assert not (tmp_path / "backup.db-shm").exists()


def test_row_counts_reflect_the_data(tmp_path: Path) -> None:
    src = migrate_backup(tmp_path)
    connection = connect(src)
    connection.execute(
        "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
        (USER_ID, TIMESTAMP, TIMESTAMP),
    )
    connection.close()

    out = StringIO()
    assert migrate_check.run(src, out=out) == 0

    text = out.getvalue()
    assert "users: 1" in text
    assert "profiles: 0" in text


def test_missing_src_returns_nonzero(tmp_path: Path) -> None:
    err = StringIO()
    assert migrate_check.run(tmp_path / "absent.db", err=err) != 0
    assert "not found" in err.getvalue()
