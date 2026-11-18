"""時刻の直接呼び出し不在検証（design/14-dod-conventions.md §16 / design/11-testing.md §12）。

`src/` 配下の全 Python ファイルを AST 解析し、壁時計の現在時刻を直接取得する呼び出しが
無いことを検証する。時刻は `Clock` ポート経由で取得する規約（design/03-domain-model.md §4
Port / design/08-database.md §9）を機械的に固定する。

単調時間（`time.perf_counter` / `time.monotonic`）は処理時間の計測に用いる正規の用法なので
対象外（design/10-infra-ops.md §11 の運用ログ）。`datetime.now` を直接呼べる唯一の例外は
`src/shared/clock/clock.py` の `SystemClock`（`tests/domain/test_clock.py` の宣言と一致）。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# 壁時計の現在時刻を直接取得する呼び出し（design/14 §16 の「datetime.now 等」）。
FORBIDDEN_CALLS = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "date.today",
        "datetime.date.today",
        "time.time",
        "time.localtime",
        "time.gmtime",
        "time.mktime",
    }
)

# 例外: `datetime.now(UTC)` を呼んでよい唯一のファイル（test_clock.py が宣言する例外）。
ALLOWED = frozenset({SRC / "shared" / "clock" / "clock.py"})


def _dotted(node: ast.expr) -> str | None:
    """`datetime.now` のような属性/名前の連鎖をドット結合で表す。"""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _violations_in_source(source: str) -> list[str]:
    """ソース文字列に含まれる禁止呼び出しのドット名（出現順）を返す。"""
    tree = ast.parse(source)
    return [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for name in (_dotted(node.func),)
        if name in FORBIDDEN_CALLS
    ]


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_wall_clock_calls_outside_system_clock() -> None:
    """`src/` の壁時計直接取得が `src/shared/clock/clock.py` だけであること。"""
    violations: list[str] = []
    for path in _source_files():
        if path in ALLOWED:
            continue
        for name in _violations_in_source(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {name}")
    assert violations == []


def test_system_clock_exception_is_real() -> None:
    """例外ファイルが実際に `datetime.now` を呼んでいること（例外の空振り検出）。"""
    source = (SRC / "shared" / "clock" / "clock.py").read_text(encoding="utf-8")
    assert "datetime.now" in _violations_in_source(source)


def test_scanner_detects_wall_clock_calls() -> None:
    """陽性対照: 壁時計の直接取得を検出する。"""
    source = (
        "from datetime import datetime, date\n"
        "import time\n"
        "a = datetime.now()\n"
        "b = datetime.utcnow()\n"
        "c = date.today()\n"
        "d = time.time()\n"
    )
    assert _violations_in_source(source) == [
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "time.time",
    ]


def test_scanner_ignores_port_and_monotonic_usage() -> None:
    """陰性対照: ポート経由・単調時間・整形・構築は検出しない。"""
    source = (
        "from datetime import datetime\n"
        "import time\n"
        "a = clock.now()\n"
        "b = time.perf_counter()\n"
        "c = time.monotonic()\n"
        "d = datetime.strptime('2026-01-01T00:00:00.000000Z', fmt)\n"
        "e = datetime(2026, 1, 1)\n"
    )
    assert _violations_in_source(source) == []
