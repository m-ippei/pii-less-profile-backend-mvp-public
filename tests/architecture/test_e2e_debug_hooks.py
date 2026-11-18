"""`tests/e2e/` へのデバッグ用フックの残置を検出する（D-05 / design/11-testing.md §12）。

目視確認モード（`make e2e-headed`）では `page.pause()` や `PWDEBUG` を一時的に使う。
headless の `make e2e` では `page.pause()` は no-op のため実行は止まらないが、意図しない
残置を防ぐため、`tests/e2e/` 配下の `.py` を再帰的に走査して混入を検出する。

`plan/archive/proposals/e2e-headed-vnc.md` §7-2 / `design/14-dod-conventions.md` §16。
"""

from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"

FORBIDDEN_TOKENS = ("page.pause", "PWDEBUG")


def _scan_text(text: str) -> list[str]:
    """テキストに含まれる禁止トークンを返す（走査ロジックの本体）。"""
    return [token for token in FORBIDDEN_TOKENS if token in text]


def _scan_files(paths: Iterable[Path]) -> list[str]:
    """各ファイルを読み、禁止トークンを含む箇所を `path: token` の形で返す。"""
    violations: list[str] = []
    for path in paths:
        for token in _scan_text(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {token}")
    return violations


def test_e2e_sources_do_not_contain_debug_hooks() -> None:
    """`tests/e2e/**/*.py` に `page.pause` / `PWDEBUG` が無い。"""
    files = sorted(E2E.rglob("*.py"))
    assert files, "tests/e2e 配下の .py が見つからない"
    assert _scan_files(files) == []


def test_scanner_detects_debug_hooks() -> None:
    """陽性対照: 走査ロジックが混入を検出できる（テスト自体は走査対象外）。"""
    sample = "page.goto('/')\npage.pause()\nimport os\nos.environ['PWDEBUG'] = '1'\n"
    found = _scan_text(sample)
    assert "page.pause" in found
    assert "PWDEBUG" in found


def test_scanner_passes_clean_text() -> None:
    """陰性対照: 禁止トークンが無ければ検出しない。"""
    assert _scan_text("page.goto('/healthz')\n") == []
