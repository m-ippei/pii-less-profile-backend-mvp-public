"""テストの削除・弱体化・`skip`・`xfail` の不使用を走査する
（design/14-dod-conventions.md §16 / design/11-testing.md §12）。

`tests/` 配下のテストが `pytest.mark.skip` / `skipif` / `xfail` や `pytest.skip` /
`pytest.xfail` / `pytest.importorskip` を使っていないこと、`pytest.mark` が
`parametrize` 以外に使われていないことを AST で検証する。「テストの削除・弱体化・`skip`・
`xfail`・期待値の書き換えで通さない」のうち、機械的に検出できる部分を固定する。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"

ALLOWED_MARKS = frozenset({"parametrize"})
FORBIDDEN_CALLS = frozenset({"pytest.skip", "pytest.xfail", "pytest.importorskip"})


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _mark_name(node: ast.Attribute) -> str | None:
    """`pytest.mark.<name>` の `<name>` を返す（`pytest.mark` でなければ None）。"""
    inner = node.value
    if (
        isinstance(inner, ast.Attribute)
        and inner.attr == "mark"
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "pytest"
    ):
        return node.attr
    return None


def _violations_in_source(source: str) -> list[str]:
    """禁止された mark・呼び出しのドット名を出現順で返す。"""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            mark = _mark_name(node)
            if mark is not None and mark not in ALLOWED_MARKS:
                found.append(f"pytest.mark.{mark}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in FORBIDDEN_CALLS:
                found.append(name)
    return found


def _test_files() -> list[Path]:
    return sorted(TESTS.rglob("*.py"))


def test_no_disabled_or_skipped_tests() -> None:
    """`tests/` に `skip` / `xfail` / 未許可の `pytest.mark` が無いこと。"""
    violations: list[str] = []
    for path in _test_files():
        for name in _violations_in_source(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {name}")
    assert violations == []


def test_scanner_detects_skip_and_xfail() -> None:
    """陽性対照: `skip` / `skipif` / `xfail` の mark と呼び出しを検出する。"""
    source = (
        "import pytest\n"
        "@pytest.mark.skip\n"
        "def test_a():\n"
        "    pass\n"
        "@pytest.mark.xfail(reason='x')\n"
        "def test_b():\n"
        "    pytest.skip('now')\n"
        "    pytest.xfail('x')\n"
    )
    assert sorted(_violations_in_source(source)) == [
        "pytest.mark.skip",
        "pytest.mark.xfail",
        "pytest.skip",
        "pytest.xfail",
    ]


def test_scanner_allows_parametrize_and_fixture() -> None:
    """陰性対照: `parametrize` と `fixture` は検出しない。"""
    source = (
        "import pytest\n"
        "@pytest.fixture()\n"
        "def sample():\n"
        "    return 1\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_ok(sample, x):\n"
        "    assert x\n"
    )
    assert _violations_in_source(source) == []
