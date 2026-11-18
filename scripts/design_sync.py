"""design/（正本・分割）から統合スナップショット DESIGN.md を一方向生成する。

- `assemble`: 各分割ファイル先頭のメタを落として本文を連結し `DESIGN.md` を生成。
  `00-INDEX.md` の `version` / `最終更新` を先頭ヘッダへ付与する（欠落・形式不正は fail-closed）

逆変換（`DESIGN.md` → `design/`）は行わない。差分表示は標準の `diff` を使う
（`make design-diff`）。根拠: plan/archive/proposals/design-sync-tooling.md /
plan/proposals/design-doc-operations.md §A-3/A-4 / design/00-INDEX.md /
design/14-dod-conventions.md §16。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESIGN_DIR = REPO_ROOT / "design"
DEFAULT_DESIGN_MD = REPO_ROOT / "DESIGN.md"
INDEX_NAME = "00-INDEX.md"
DESIGN_TITLE = "# DESIGN — 統合設計書"

INDEX_VERSION_RE = re.compile(r"^- version:\s*(\S+)\s*$", re.MULTILINE)
INDEX_UPDATED_RE = re.compile(r"^- 最終更新:\s*(\S+)\s*$", re.MULTILINE)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# `design/` 内の分割ファイルへの相対リンク。`DESIGN.md` はリポジトリ直下にあるため、
# 連結時に `design/` 接頭辞を付けて解決できるようにする。
DESIGN_FILE_LINK_RE = re.compile(r"\]\((\d{2}-[a-z0-9-]+\.md)(#[^)]*)?\)")


class DesignSyncError(Exception):
    """design/ の構成が想定と異なるときのエラー。"""


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _search_index_meta(text: str, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise DesignSyncError(f"{INDEX_NAME}: {label} が見つからない")
    return match.group(1)


def parse_index_meta(design_dir: Path) -> tuple[str, str]:
    """`00-INDEX.md` の `version` / `最終更新` を返す。欠落・形式不正は DesignSyncError。"""
    index_path = design_dir / INDEX_NAME
    if not index_path.exists():
        raise DesignSyncError(f"{INDEX_NAME}: 見つからない")
    text = normalize(index_path.read_text(encoding="utf-8"))
    version = _search_index_meta(text, INDEX_VERSION_RE, "version")
    updated = _search_index_meta(text, INDEX_UPDATED_RE, "最終更新")
    if not VERSION_RE.match(version):
        raise DesignSyncError(f"{INDEX_NAME}: version の形式が不正: {version!r}")
    if not DATE_RE.match(updated):
        raise DesignSyncError(f"{INDEX_NAME}: 最終更新 の形式が不正: {updated!r}")
    return version, updated


def build_header(version: str, updated: str) -> str:
    return f"{DESIGN_TITLE}\n\n- version: {version}\n- 最終更新: {updated}\n\n"


def iter_design_files(design_dir: Path) -> list[Path]:
    return sorted(p for p in design_dir.glob("*.md") if p.name != INDEX_NAME)


def read_body(path: Path) -> str:
    text = normalize(path.read_text(encoding="utf-8"))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line == "---":
            body = "\n".join(lines[i:])
            break
    else:
        raise DesignSyncError(f"{path.name}: 区切り '---' が見つからない")
    if not body.endswith("\n"):
        body += "\n"
    return body


def rewrite_design_links(text: str) -> str:
    """`design/` 内の分割ファイルへのリンクを、直下の `DESIGN.md` から解決できる形に直す。"""
    return DESIGN_FILE_LINK_RE.sub(
        lambda match: f"](design/{match.group(1)}{match.group(2) or ''})", text
    )


def assemble(design_dir: Path) -> str:
    version, updated = parse_index_meta(design_dir)
    parts = [read_body(p) for p in iter_design_files(design_dir)]
    return build_header(version, updated) + rewrite_design_links("\n".join(parts))


def write_design(design_dir: Path, out_path: Path) -> None:
    out_path.write_text(assemble(design_dir), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="design/ から DESIGN.md を生成")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--design-dir", default=str(DEFAULT_DESIGN_DIR))
    p.add_argument("--design-md", default=str(DEFAULT_DESIGN_MD))
    args = parser.parse_args(argv)

    write_design(Path(args.design_dir), Path(args.design_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
