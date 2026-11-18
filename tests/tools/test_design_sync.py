"""design_sync（`design/` → `DESIGN.md` の一方向生成）のテスト。

根拠: design/00-INDEX.md（分割表・メタ）/ design/14-dod-conventions.md §16 /
plan/proposals/design-doc-operations.md §A-3/A-4。

観測すること:
1. `assemble` が各分割ファイル先頭のメタ（タイトル・出典・関連）を落として本文を連結する
   （`00-INDEX.md` は含めない。非番号 H2 も本文として連結する）
2. `DESIGN.md` の先頭ヘッダが `00-INDEX.md` の version / 最終更新 と一致する
3. version / 最終更新 が欠落・形式不正なら `DesignSyncError`（fail-closed）
4. CRLF も LF に正規化して同一の出力になる
5. `main(["assemble", ...])` が指定パスへ出力する
6. 区切り `---` を持たないファイルはエラーになる
"""

from pathlib import Path

import pytest

from scripts import design_sync

VERSION = "1.0.0"
UPDATED = "2026-10-08"
HEADER = f"# DESIGN — 統合設計書\n\n- version: {VERSION}\n- 最終更新: {UPDATED}\n\n"

BODY_01 = "---\n\n## 1. 概要\n\n本文A\n"
BODY_02 = "---\n\n## 3. アーキテクチャ\n\n本文B\n---\n\n## 派生メモ\n\nメモ\n"
EXPECTED = HEADER + BODY_01 + "\n" + BODY_02

DEFAULT_INDEX_META = f"- version: {VERSION}\n- 最終更新: {UPDATED}\n"


def _make_design(
    design_dir: Path, newline: str = "\n", index_meta: str = DEFAULT_INDEX_META
) -> None:
    (design_dir / "00-INDEX.md").write_text(
        f"# 設計書インデックス\n\n{index_meta}\n## 分割表\n".replace("\n", newline),
        encoding="utf-8",
        newline="",
    )
    (design_dir / "01-overview.md").write_text(
        "# 設計 01: 概要\n\n> 出典: x\n> 関連: y\n\n---\n\n## 1. 概要\n\n本文A\n".replace(
            "\n", newline
        ),
        encoding="utf-8",
        newline="",
    )
    (design_dir / "02-arch.md").write_text(
        "# 設計 02: アーキテクチャ\n\n> 出典: z\n> 関連: w\n\n---\n\n"
        "## 3. アーキテクチャ\n\n本文B\n---\n\n## 派生メモ\n\nメモ\n".replace("\n", newline),
        encoding="utf-8",
        newline="",
    )


def test_assemble_concatenates_bodies_without_metadata(tmp_path: Path) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir)

    assert design_sync.assemble(design_dir) == EXPECTED


def test_assemble_header_matches_index_meta(tmp_path: Path) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir)

    output = design_sync.assemble(design_dir)

    assert output.startswith(HEADER)
    assert f"- version: {VERSION}\n" in output
    assert f"- 最終更新: {UPDATED}\n" in output


def test_assemble_normalizes_crlf(tmp_path: Path) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir, newline="\r\n")

    assert design_sync.assemble(design_dir) == EXPECTED


def test_main_assemble_writes_to_given_paths(tmp_path: Path) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir)
    out_path = tmp_path / "DESIGN.md"

    args = ["assemble", "--design-dir", str(design_dir), "--design-md", str(out_path)]
    rc = design_sync.main(args)

    assert rc == 0
    assert out_path.read_text(encoding="utf-8") == EXPECTED


def test_rewrite_design_links_adds_design_prefix() -> None:
    """`DESIGN.md` から解決できるよう、分割設計書へのリンクに `design/` を付ける。"""
    text = "参照: [テスト](11-testing.md) / [§18](16-deployment.md#sec18) / [外部](https://example.com)"
    assert design_sync.rewrite_design_links(text) == (
        "参照: [テスト](design/11-testing.md) / "
        "[§18](design/16-deployment.md#sec18) / [外部](https://example.com)"
    )


def test_missing_separator_raises(tmp_path: Path) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    (design_dir / "01-overview.md").write_text(
        "# 設計 01: 概要\n\n## 1. 概要\n\n本文\n", encoding="utf-8", newline=""
    )

    with pytest.raises(design_sync.DesignSyncError):
        design_sync.assemble(design_dir)


@pytest.mark.parametrize(
    "index_meta",
    [
        "",  # 両方欠落
        "- version: 1.0.0\n",  # 最終更新 欠落
        "- 最終更新: 2026-10-08\n",  # version 欠落
    ],
)
def test_missing_index_meta_raises(tmp_path: Path, index_meta: str) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir, index_meta=index_meta)

    with pytest.raises(design_sync.DesignSyncError):
        design_sync.assemble(design_dir)


@pytest.mark.parametrize(
    "index_meta",
    [
        "- version: v1\n- 最終更新: 2026-10-08\n",  # version 形式不正
        "- version: 1.0\n- 最終更新: 2026-10-08\n",  # version 形式不正
        "- version: 1.0.0\n- 最終更新: 2026/10/08\n",  # 日付形式不正
        "- version: 1.0.0\n- 最終更新: 2026-10-8\n",  # 日付形式不正
    ],
)
def test_invalid_index_meta_format_raises(tmp_path: Path, index_meta: str) -> None:
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    _make_design(design_dir, index_meta=index_meta)

    with pytest.raises(design_sync.DesignSyncError):
        design_sync.assemble(design_dir)
