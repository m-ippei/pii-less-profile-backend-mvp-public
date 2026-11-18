"""migration runner（design/09-migrations.md §10）。

- `migrations/NNN_name.sql` を番号順に適用する。down migration は無い（forward-only）。
- 適用は `make migrate` の明示実行のみ。アプリ起動時には呼ばない。
- 管理表 `schema_migrations` に version / name / checksum / applied_at を記録し、
  適用済み checksum の不一致は失敗にする（exit 1）。
- 1 migration = 1 transaction。`executescript` は使わず、`sqlite3.complete_statement` で
  文単位に分割して `execute` する（`executescript` は暗黙に COMMIT して原子性を壊す）。
- `-- migrate: foreign_keys=off` があるときだけ、その migration の実行中に FK 検査を切る。
- 未適用の migration がある場合、適用前に SQLite の `.backup` API で `pre-migrate-*.db` を
  取得する（`design/09-migrations.md` §10）。`ENV=prod` は取得失敗で適用しない。適用成功の
  直後にだけ、`pre-migrate-*` だけを対象に新しい順に3世代・30日以内を残して整理する。
"""

import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from src.shared.clock.clock import Clock, SystemClock, format_timestamp
from src.shared.db import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "migrations"

DIRECTIVE_PREFIX = "-- migrate:"
FOREIGN_KEYS_OFF = "foreign_keys=off"

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "pre-migrate-"
BACKUP_SUFFIX = ".db"
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
BACKUP_KEEP = 3
BACKUP_MAX_AGE_DAYS = 30
PROD_ENV = "prod"

# `pre-migrate-v<前>-to-v<後>-<YYYYMMDD-HHMMSS>.db`（design/09-migrations.md §10）。
BACKUP_NAME_PATTERN = re.compile(
    rf"^{re.escape(BACKUP_PREFIX)}v\d+-to-v\d+-(\d{{8}}-\d{{6}}){re.escape(BACKUP_SUFFIX)}$"
)

# design/09-migrations.md §10 の列定義。design/08-database.md §9「全テーブル STRICT」に合わせる。
SCHEMA_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "checksum TEXT NOT NULL, "
    "applied_at TEXT NOT NULL) STRICT"
)


class MigrationError(Exception):
    """migration の指示・本文が仕様に反するときに送出する。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """`NNN_name.sql` を番号順に列挙する。"""
    migrations = [
        Migration(version=int(path.name[:3]), name=path.stem[4:], path=path)
        for path in migrations_dir.glob("[0-9][0-9][0-9]_*.sql")
    ]
    migrations.sort(key=lambda migration: migration.version)
    return migrations


def checksum_of(sql: str) -> str:
    """指示行を含むファイル全体（UTF-8）の SHA-256。"""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def parse_foreign_keys_off(sql: str) -> bool:
    """`-- migrate:` 指示を検証し、`foreign_keys=off` の有無を返す。未知の指示はエラー。"""
    foreign_keys_off = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith(DIRECTIVE_PREFIX):
            continue
        directive = stripped[len(DIRECTIVE_PREFIX) :].strip()
        if directive == FOREIGN_KEYS_OFF:
            foreign_keys_off = True
        else:
            raise MigrationError(f"unknown migration directive: {directive!r}")
    return foreign_keys_off


def split_statements(sql: str) -> list[str]:
    """`sqlite3.complete_statement` で 1 文ずつに分割する。"""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if _has_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _has_sql(buffer):
        statements.append(buffer.strip())
    return statements


def _has_sql(text: str) -> bool:
    """コメント行だけの塊を実行対象にしないための判定。"""
    return any(line.strip() and not line.strip().startswith("--") for line in text.splitlines())


def backup_dir_for(db_path: str | Path) -> Path:
    """バックアップの置き場（DB と同じディレクトリ配下の `backups/`）。"""
    return Path(db_path).parent / BACKUP_DIR_NAME


def backup_name(before: int, after: int, now: datetime) -> str:
    """`pre-migrate-v<前>-to-v<後>-<YYYYMMDD-HHMMSS>.db` を作る（UTC）。"""
    stamp = now.astimezone(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)
    return f"{BACKUP_PREFIX}v{before}-to-v{after}-{stamp}{BACKUP_SUFFIX}"


def create_pre_migrate_backup(
    db_path: str | Path,
    backup_dir: Path,
    *,
    before: int,
    after: int,
    now: datetime,
) -> Path:
    """適用前バックアップを SQLite の `.backup` API で取得する。

    WAL/SHM を手でコピーしない（design/10-infra-ops.md §11）。ソースは別接続で開き、
    バックアップ先も接続として渡す。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / backup_name(before, after, now)
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return destination


def _backup_timestamp(name: str) -> datetime | None:
    """`pre-migrate-*` 名から取得時刻（UTC）を取り出す。形式外は None。"""
    match = BACKUP_NAME_PATTERN.match(name)
    if match is None:
        return None
    try:
        moment = datetime.strptime(match.group(1), BACKUP_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC)


def cleanup_pre_migrate_backups(
    backup_dir: Path,
    now: datetime,
    *,
    keep: int = BACKUP_KEEP,
    max_age_days: int = BACKUP_MAX_AGE_DAYS,
) -> list[Path]:
    """`pre-migrate-*.db` だけを対象に、新しい順に `keep` 世代・`max_age_days` 以内を残す。

    形式外のファイルは対象にしない（日次・週次バックアップに触れない。design/09 §10）。
    """
    if not backup_dir.is_dir():
        return []
    moment = now.astimezone(UTC)
    deadline = moment - timedelta(days=max_age_days)
    candidates: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        timestamp = _backup_timestamp(path.name)
        if timestamp is not None:
            candidates.append((timestamp, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    removed: list[Path] = []
    for index, (timestamp, path) in enumerate(candidates):
        if index >= keep or timestamp < deadline:
            path.unlink()
            removed.append(path)
    return removed


def apply_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
    name: str,
    checksum: str,
    sql: str,
    applied_at: str,
) -> None:
    """1つの migration を 1 transaction で適用し、`schema_migrations` に記録する。

    `foreign_keys=off` 指示があるときは、実行中だけ FK 検査を切り、終了時に必ず戻す
    （成功・失敗を問わず `finally`）。`foreign_key_check` が 1 行でも返せば ROLLBACK する。
    """
    foreign_keys_off = parse_foreign_keys_off(sql)
    statements = split_statements(sql)
    try:
        if foreign_keys_off:
            connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            if foreign_keys_off:
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationError(
                        f"foreign_key_check failed: {len(violations)} violation(s)"
                    )
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (version, name, checksum, applied_at),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        if foreign_keys_off:
            connection.execute("PRAGMA foreign_keys=ON")


def _prepare_backup(
    db_path: str | Path,
    *,
    before: int,
    after: int,
    now: datetime,
    environment: str,
    err: TextIO,
) -> bool:
    """適用前バックアップを取得する。`ENV=prod` の取得失敗は適用中止（False）。"""
    try:
        create_pre_migrate_backup(
            db_path, backup_dir_for(db_path), before=before, after=after, now=now
        )
    except (OSError, sqlite3.Error) as error:
        if environment == PROD_ENV:
            print(f"backup failed (ENV=prod, migration aborted): {error}", file=err)
            return False
        print(f"backup skipped: {error}", file=err)
    return True


def run(
    db_path: str | Path,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
    *,
    clock: Clock | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    env: str | None = None,
) -> int:
    """未適用の migration を順に適用する。成功で 0、失敗で 0 以外を返す。

    未適用がある場合、適用前にバックアップを取得し（`ENV=prod` は必須）、成功直後に
    `pre-migrate-*` を整理する（design/09-migrations.md §10）。
    """
    active_clock = clock or SystemClock()
    environment = env if env is not None else os.environ.get("ENV", "dev")
    connection = connect(db_path)
    try:
        connection.execute(SCHEMA_MIGRATIONS_DDL)
        applied = {
            row["version"]: row["checksum"]
            for row in connection.execute("SELECT version, checksum FROM schema_migrations")
        }

        pending: list[tuple[Migration, str, str]] = []
        for migration in discover_migrations(migrations_dir):
            sql = migration.path.read_text(encoding="utf-8")
            checksum = checksum_of(sql)
            if migration.version in applied:
                if applied[migration.version] != checksum:
                    print(f"checksum mismatch: {migration.path.name}", file=err)
                    return 1
                continue
            pending.append((migration, sql, checksum))

        if pending:
            before = max(applied, default=0)
            after = pending[-1][0].version
            if not _prepare_backup(
                db_path,
                before=before,
                after=after,
                now=active_clock.now(),
                environment=environment,
                err=err,
            ):
                return 1

        for migration, sql, checksum in pending:
            try:
                apply_migration(
                    connection,
                    version=migration.version,
                    name=migration.name,
                    checksum=checksum,
                    sql=sql,
                    applied_at=format_timestamp(active_clock.now()),
                )
            except (MigrationError, sqlite3.Error) as error:
                print(f"failed: {migration.path.name}: {error}", file=err)
                return 1
            print(f"applied: {migration.path.name}", file=out)

        if pending:
            removed = cleanup_pre_migrate_backups(backup_dir_for(db_path), active_clock.now())
            for path in removed:
                print(f"removed backup: {path.name}", file=out)
        return 0
    finally:
        connection.close()


def main() -> int:
    db_path = os.environ.get("DATABASE_PATH")
    if not db_path:
        print("DATABASE_PATH is required", file=sys.stderr)
        return 1
    return run(db_path)


if __name__ == "__main__":
    sys.exit(main())
