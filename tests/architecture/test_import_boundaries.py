"""アーキテクチャ境界を import の走査で固定する（design/02-architecture.md §3 / §12）。

`design/14-dod-conventions.md` §16 の実装規約のうち、M06 の完了条件に含まれる3件を検証する。

1. `domain` 層が標準ライブラリのみ import している
2. `profile` から `identity`、`identity` から `profile` の import が無い
3. Context 跨ぎ（両 Context を import するファイル）が `src/app/` と `src/main.py` に限られている

`datetime.now` 等の直接呼び出しの走査は M11 のスコープ（design/11-testing.md §12）。
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

IDENTITY = "src.identity"
PROFILE = "src.profile"


def _source_files() -> list[Path]:
    """`src/` 配下の全 Python ファイル。"""
    return sorted(SRC.rglob("*.py"))


def _absolute_imports(path: Path) -> set[str]:
    """絶対 import のモジュール名を集める（相対 import は対象外）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _imports_module(modules: set[str], prefix: str) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for module in modules)


def test_domain_imports_only_the_standard_library() -> None:
    """`src/<context>/domain/` は標準ライブラリと同一パッケージ内 import だけを使う。"""
    domain_files = [path for path in _source_files() if "domain" in path.relative_to(SRC).parts]
    assert domain_files, "domain 層のファイルが見つからない"

    violations: list[str] = []
    for path in domain_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in sys.stdlib_module_names:
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue
                module = node.module or ""
                top = module.split(".")[0]
                if top not in sys.stdlib_module_names:
                    violations.append(f"{path.name}: from {module} import ...")
    assert violations == []


def test_profile_and_identity_do_not_import_each_other() -> None:
    """`profile` ↔ `identity` の相互 import が無い（Context 間は `user_id` だけで参照）。"""
    violations: list[str] = []
    for path in _source_files():
        modules = _absolute_imports(path)
        relative = path.relative_to(REPO_ROOT).as_posix()
        if path.is_relative_to(SRC / "profile") and _imports_module(modules, IDENTITY):
            violations.append(f"{relative} が identity を import")
        if path.is_relative_to(SRC / "identity") and _imports_module(modules, PROFILE):
            violations.append(f"{relative} が profile を import")
    assert violations == []


def test_context_crossing_is_limited_to_app_and_main() -> None:
    """両 Context を import できるのは `src/app/` と `src/main.py` だけ。"""
    allowed = SRC / "app"
    crossers: list[str] = []
    for path in _source_files():
        modules = _absolute_imports(path)
        if _imports_module(modules, IDENTITY) and _imports_module(modules, PROFILE):
            if path == SRC / "main.py" or path.is_relative_to(allowed):
                continue
            crossers.append(path.relative_to(REPO_ROOT).as_posix())
    assert crossers == []
