"""H表・AC表の一次検証層カバレッジ監査（design/06-errors.md §7 / design/11-testing.md §12 /
design/14-dod-conventions.md §15）。

`design/06-errors.md` §7 の H 表と `design/11-testing.md` §12 の AC 表を正本として、
各 ID に対応するテストが存在することを機械的に検証する。

- H-1〜H-18: 挙動を観測するテストが `tests/` にある（定数表の突き合わせである
  `tests/domain/test_errors.py` だけでは充足とみなさない）
- AC-1〜AC-14: 設計表の「一次検証層」に挙げられた層のテストで AC ID が参照されている

「AC に対応するテストがある」とは、一次検証層で AC を直接観測するテストがあることを指す
（`design/11-testing.md` §12）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = REPO_ROOT / "tests"
ERRORS_DOC = REPO_ROOT / "design" / "06-errors.md"
TESTING_DOC = REPO_ROOT / "design" / "11-testing.md"

# H の定数表テストのみを挙動テストと誤認しないための除外。
CONSTANTS_ONLY = frozenset({"tests/domain/test_errors.py"})

# AC 表の「一次検証層」列で使う層名 → `tests/` 配下のディレクトリ。
LAYER_DIRS = {
    "api": "api",
    "application": "application",
    "domain": "domain",
    "security": "security",
    "architecture": "architecture",
    "e2e": "e2e",
}

_H_ROW = re.compile(r"^\|\s*(H-\d+)\s*\|", re.MULTILINE)
_AC_ROW = re.compile(r"^\|\s*(AC-\d+)\s*\|(.+?)\|(.+?)\|\s*$", re.MULTILINE)
_PAREN = re.compile(r"（[^）]*）")
_LAYER_SPLIT = re.compile(r"[+、,／/]")


def parse_h_ids(errors_text: str) -> list[str]:
    """H 表の ID 列を表の出現順で返す。"""
    return _H_ROW.findall(errors_text)


def parse_ac_rows(testing_text: str) -> list[tuple[str, frozenset[str]]]:
    """AC 表の (ID, 一次検証層) を返す。層は補足（`（補助: ...）`）を除いた主層のみ。"""
    rows: list[tuple[str, frozenset[str]]] = []
    for match in _AC_ROW.finditer(testing_text):
        ac_id, _criteria, layer_cell = match.groups()
        rows.append((ac_id, primary_layers(layer_cell)))
    return rows


def primary_layers(layer_cell: str) -> frozenset[str]:
    """「一次検証層」セルから `LAYER_DIRS` に載る層名を抽出する。"""
    stripped = _PAREN.sub("", layer_cell)
    return frozenset(
        token.strip() for token in _LAYER_SPLIT.split(stripped) if token.strip() in LAYER_DIRS
    )


def _test_texts() -> dict[str, tuple[str, str]]:
    """`tests/` の `test_*.py` を (層, 本文) で返す。層は `tests/<層>/...` の第1階層。"""
    texts: dict[str, tuple[str, str]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        relative = path.relative_to(TESTS)
        layer = relative.parts[0] if len(relative.parts) > 1 else "root"
        texts[path.relative_to(REPO_ROOT).as_posix()] = (
            layer,
            path.read_text(encoding="utf-8"),
        )
    return texts


def _references(identifier: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(identifier)}\b", text) is not None


def missing_h_coverages(
    h_ids: list[str], texts: dict[str, tuple[str, str]], exclude: frozenset[str]
) -> list[str]:
    """挙動テストで参照されていない H ID を返す。"""
    return [
        h_id
        for h_id in h_ids
        if not any(
            _references(h_id, text) for path, (_, text) in texts.items() if path not in exclude
        )
    ]


def missing_ac_coverages(
    ac_rows: list[tuple[str, frozenset[str]]], texts: dict[str, tuple[str, str]]
) -> list[str]:
    """一次検証層で参照されていない AC ID を返す。"""
    missing: list[str] = []
    for ac_id, layers in ac_rows:
        covered = any(
            layer in layers and _references(ac_id, text) for _, (layer, text) in texts.items()
        )
        if not covered:
            missing.append(f"{ac_id} (layers={sorted(layers)})")
    return missing


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_h_table_has_eighteen_ids() -> None:
    """H 表が H-1〜H-18 の18件であること（正本の読み取り検証）。"""
    assert parse_h_ids(_read(ERRORS_DOC)) == [f"H-{n}" for n in range(1, 19)]


def test_ac_table_has_fourteen_ids() -> None:
    """AC 表が AC-1〜AC-14 の14件であること（正本の読み取り検証）。"""
    assert [ac for ac, _ in parse_ac_rows(_read(TESTING_DOC))] == [f"AC-{n}" for n in range(1, 15)]


def test_every_h_id_has_a_behavioral_test() -> None:
    """H-1〜H-18 の各 ID に対応する挙動テストが存在すること。"""
    h_ids = parse_h_ids(_read(ERRORS_DOC))
    assert missing_h_coverages(h_ids, _test_texts(), CONSTANTS_ONLY) == []


def test_every_ac_id_is_covered_in_its_primary_layer() -> None:
    """AC-1〜AC-14 の各 ID が、設計表の一次検証層で参照されていること。"""
    ac_rows = parse_ac_rows(_read(TESTING_DOC))
    assert missing_ac_coverages(ac_rows, _test_texts()) == []


def test_primary_layers_ignores_supplementary_notes() -> None:
    """陽性対照: 補足（`（補助: ...）`）は主層に含めず、複合層を分解する。"""
    assert primary_layers("api（補助: security）") == frozenset({"api"})
    assert primary_layers("api + application") == frozenset({"api", "application"})
    assert primary_layers("e2e（UIゲート）+ api（トランザクション）") == frozenset({"api", "e2e"})
    assert primary_layers("repository") == frozenset()


def test_detects_missing_h_coverage() -> None:
    """陽性対照: どのテストにも現れない H ID を検出する。"""
    texts = {"tests/api/sample.py": ("api", "H-1 の話だけ")}
    assert missing_h_coverages(["H-1", "H-2"], texts, frozenset()) == ["H-2"]
    only_constants = {"tests/domain/test_errors.py": ("domain", "H-1")}
    assert missing_h_coverages(["H-1"], only_constants, CONSTANTS_ONLY) == ["H-1"]


def test_detects_missing_ac_primary_layer() -> None:
    """陽性対照: 一次検証層に無いテストだけでは AC 充足にならない。"""
    ac_rows = [("AC-9", frozenset({"api"}))]
    assert missing_ac_coverages(ac_rows, {"tests/domain/x.py": ("domain", "AC-9")}) == [
        "AC-9 (layers=['api'])"
    ]
    assert missing_ac_coverages(ac_rows, {"tests/api/x.py": ("api", "AC-9")}) == []
