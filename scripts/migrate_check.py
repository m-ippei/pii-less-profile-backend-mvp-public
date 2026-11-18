"""バックアップの検証（`make migrate-check SRC=<backup>`、design/09-migrations.md §10）。

バックアップ（SQLite ファイル）を一時ディレクトリへ複製し、その複製に未適用 migration を
適用してから、`integrity_check`・`foreign_key_check`・テーブル別行数を表示する。

- 元のバックアップ（本番DBやそのコピー）には書き込まない。複製は SQLite の `.backup` API で
  行い、WAL/SHM を手でコピーしない（design/10-infra-ops.md §11）。
- 複製への migration 適用は `scripts/migrate.py` の runner をそのまま使う（`ENV=dev` で、
  複製の使い捨てバックアップは失敗しても続行する）。
"""

import argparse
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from scripts import migrate
from src.shared.db import connect

COPY_NAME = "app.db"


def copy_database(src: Path, dest: Path) -> None:
    """`.backup` API で `src` を `dest` へ複製する（`src` は読み取りのみ）。"""
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def report(db_path: Path, out: TextIO) -> None:
    """`integrity_check`・`foreign_key_check`・テーブル別行数を表示する。"""
    connection = connect(db_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_text = "; ".join(str(row[0]) for row in integrity)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        print(f"integrity_check: {integrity_text}", file=out)
        print(f"foreign_key_check: {len(violations)} violation(s)", file=out)

        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for row in tables:
            name = row["name"]
            identifier = name.replace('"', '""')
            count = connection.execute(f'SELECT count(*) FROM "{identifier}"').fetchone()[0]
            print(f"{name}: {count}", file=out)
    finally:
        connection.close()


def run(
    src: Path,
    *,
    migrations_dir: Path = migrate.DEFAULT_MIGRATIONS_DIR,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """`src` を複製して migration を適用し、結果を表示する。成功で 0。"""
    if not src.is_file():
        print(f"backup not found: {src}", file=err)
        return 1
    with tempfile.TemporaryDirectory(prefix="migrate-check-") as tmp:
        copy = Path(tmp) / COPY_NAME
        copy_database(src, copy)
        if migrate.run(copy, migrations_dir, env="dev", out=out, err=err) != 0:
            print("migration failed on the copy", file=err)
            return 1
        report(copy, out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="バックアップの整合性とテーブル別行数を確認する（本番DBに触らない）"
    )
    parser.add_argument("src", help="バックアップ SQLite ファイルのパス")
    args = parser.parse_args(argv)
    return run(Path(args.src))


if __name__ == "__main__":
    sys.exit(main())
