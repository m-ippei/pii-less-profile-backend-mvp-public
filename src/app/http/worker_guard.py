"""`worker > 1` の起動時拒否（`design/07-security.md` §8 Rate limit / D-24 /
`design/10-infra-ops.md` §11 実行モデルとDB）。

レート制限はプロセス内メモリで、worker ごとに分断される。worker を2つ以上起動すると
制限が worker 数倍に緩むため、起動時に拒否する（fail-closed）。

uvicorn は worker を multiprocessing `spawn` で起動する。spawn は
`get_preparation_data()` で `sys.argv` を子へ渡し `prepare()` で復元するため、worker
プロセスのアプリでも `sys.argv` に `--workers` が残る。`--workers` 省略時の既定は
`$WEB_CONCURRENCY`（無ければ1）。
"""

import os
import sys
from collections.abc import Mapping, Sequence

WORKER_LIMIT_ERROR = "worker > 1 is not supported: in-memory rate limit is per-process"


def worker_count(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """起動引数と環境変数から uvicorn の worker 数を求める。

    `--workers`（`--workers N` / `--workers=N`）を最優先し、無ければ `WEB_CONCURRENCY`、
    それも無ければ1。整数に解釈できない値は `ValueError`。
    """
    arguments = sys.argv if argv is None else argv
    environment = os.environ if environ is None else environ
    from_argv = _workers_from_argv(arguments)
    if from_argv is not None:
        return from_argv
    raw = environment.get("WEB_CONCURRENCY")
    if raw is None:
        return 1
    return _parse_count(raw, "WEB_CONCURRENCY")


def ensure_single_worker(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """worker 数が1より大きいなら起動を拒否する（fail-closed）。"""
    if worker_count(argv, environ) > 1:
        raise RuntimeError(WORKER_LIMIT_ERROR)


def _workers_from_argv(argv: Sequence[str]) -> int | None:
    """`--workers` を探して値を返す。無ければ `None`。"""
    for index, argument in enumerate(argv):
        if argument == "--workers":
            if index + 1 >= len(argv):
                raise ValueError("--workers requires a value")
            return _parse_count(argv[index + 1], "--workers")
        if argument.startswith("--workers="):
            return _parse_count(argument.split("=", 1)[1], "--workers")
    return None


def _parse_count(raw: str, source: str) -> int:
    try:
        count = int(raw)
    except ValueError as error:
        raise ValueError(f"{source} must be an integer") from error
    if count < 1:
        raise ValueError(f"{source} must be positive")
    return count
