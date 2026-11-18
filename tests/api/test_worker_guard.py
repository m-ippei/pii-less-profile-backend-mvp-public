"""`worker > 1` の起動時拒否のテスト（design/07-security.md §8 Rate limit / D-24 /
design/10-infra-ops.md §11 実行モデルとDB）。

T-10-02 の完了条件「`worker > 1` で起動が失敗する」を、検出関数の単体と、`main.lifespan`
の起動で観測する。

uvicorn は worker を multiprocessing `spawn` で起動するが、spawn は `get_preparation_data`
で `sys.argv` を子へ渡し `prepare` で復元する（CPython `multiprocessing/spawn.py`）。
そのため worker プロセスのアプリでも `sys.argv` に `--workers` が残り、起動時に検出できる。
`--workers` 省略時の uvicorn の既定は `$WEB_CONCURRENCY`（無ければ1）。
"""

import sys
from collections.abc import Mapping, Sequence

import anyio
import pytest

import src.main as main
from src.app.http.worker_guard import ensure_single_worker, worker_count


def test_default_worker_count_is_one() -> None:
    """引数も環境変数も無ければ1であること。"""
    assert worker_count([], {}) == 1


def test_explicit_single_worker_is_allowed() -> None:
    """`--workers 1` は許容すること（Dockerfile の既定）。"""
    assert worker_count(["--workers", "1"], {}) == 1


def test_explicit_multiple_workers_are_detected() -> None:
    """`--workers 2`（空白区切り）を検出すること。"""
    assert worker_count(["--workers", "2"], {}) == 2


def test_equals_form_is_detected() -> None:
    """`--workers=2`（等号区切り）も検出すること。"""
    assert worker_count(["--workers=2"], {}) == 2


def test_web_concurrency_supplies_the_default() -> None:
    """`--workers` が無いときは `WEB_CONCURRENCY` を既定として読むこと。"""
    assert worker_count([], {"WEB_CONCURRENCY": "3"}) == 3


def test_argv_takes_precedence_over_web_concurrency() -> None:
    """`--workers` が明示されていれば `WEB_CONCURRENCY` より優先すること。"""
    env: Mapping[str, str] = {"WEB_CONCURRENCY": "3"}
    assert worker_count(["--workers", "1"], env) == 1


def test_missing_value_after_flag_is_rejected() -> None:
    """`--workers` の値が欠けている起動引数を拒否すること。"""
    with pytest.raises(ValueError):
        worker_count(["--workers"], {})


def test_non_integer_worker_count_is_rejected() -> None:
    """整数に解釈できない worker 数を拒否すること。"""
    with pytest.raises(ValueError):
        worker_count(["--workers", "abc"], {})


def test_ensure_single_worker_allows_one() -> None:
    """worker が1なら起動を許可すること。"""
    ensure_single_worker(["--workers", "1"], {})


def test_ensure_single_worker_rejects_more_than_one() -> None:
    """worker が2以上なら起動を拒否すること（fail-closed）。"""
    with pytest.raises(RuntimeError):
        ensure_single_worker(["--workers", "2"], {})


async def _enter_lifespan() -> None:
    """`main.lifespan` を起動するだけ（起動時の検査を通す）。"""
    async with main.lifespan(main.app):
        pass


def test_lifespan_rejects_multiple_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main.lifespan` の起動で worker>1 を検出し、起動が失敗すること。"""
    monkeypatch.setattr(sys, "argv", ["uvicorn", "src.main:app", "--workers", "2"])
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    with pytest.raises(RuntimeError):
        anyio.run(_enter_lifespan)


def test_lifespan_allows_single_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker=1 なら lifespan の起動が成功すること（陽性対照）。"""
    monkeypatch.setattr(sys, "argv", ["uvicorn", "src.main:app", "--workers", "1"])
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    anyio.run(_enter_lifespan)


def test_sequence_and_mapping_types_are_generic() -> None:
    """`argv` / `environ` が `Sequence` / `Mapping` で受け取れること（呼び出し側の自由度）。"""
    argv: Sequence[str] = ("--workers", "2")
    env: Mapping[str, str] = {}
    assert worker_count(argv, env) == 2
