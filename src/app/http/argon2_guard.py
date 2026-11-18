"""Argon2 実行枠のノンブロッキング制御（`design/07-security.md` §8「秘密値と保存」/
`design/10-infra-ops.md` §11「設定」「実行モデルとDB」/ `design/06-errors.md` §7 H-17）。

Argon2id はブロッキングでメモリを使う。同時実行を `ARGON2_MAX_CONCURRENCY`（既定2）に
制限し、枠が埋まっているリクエストは**待たせず** 503 `SERVICE_UNAVAILABLE` ＋ `Retry-After`
で拒否する（待機で threadpool を占有しない）。枠は `hash` / `verify` / `dummy_verify` の
各 Argon2 呼び出しだけを囲み、DB 処理は含めない。
"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from threading import BoundedSemaphore

from src.app.errors import AppError
from src.shared.errors.errors import ErrorCode
from src.shared.security.security import PasswordHasher

# `design/10-infra-ops.md` §11 の既定値。
DEFAULT_ARGON2_MAX_CONCURRENCY = 2
# 503 の `Retry-After`（秒）。枠は短時間で空く想定の最小値（H-17 と同じ扱い）。
ARGON2_RETRY_AFTER_SECONDS = 1


def max_concurrency_from_env() -> int:
    """`ARGON2_MAX_CONCURRENCY` を読む。未設定は既定2、不正は `ValueError`（起動失敗）。"""
    raw = os.getenv("ARGON2_MAX_CONCURRENCY")
    if raw is None:
        return DEFAULT_ARGON2_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("ARGON2_MAX_CONCURRENCY must be an integer") from error
    if value < 1:
        raise ValueError("ARGON2_MAX_CONCURRENCY must be positive")
    return value


class Argon2SaturatedError(AppError):
    """Argon2 実行枠が埋まっている。503 `SERVICE_UNAVAILABLE` ＋ `Retry-After`（H-17 相当）。"""

    def __init__(self, retry_after: int = ARGON2_RETRY_AFTER_SECONDS) -> None:
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE)
        self.retry_after = max(1, retry_after)


class Argon2Guard:
    """`ARGON2_MAX_CONCURRENCY` 個の枠を非ブロッキングで提供する（`worker=1` 前提）。

    枠の取得は非ブロッキングなので、埋まっていれば待たずに即座に拒否できる。
    """

    def __init__(self, max_concurrency: int | None = None) -> None:
        limit = max_concurrency_from_env() if max_concurrency is None else max_concurrency
        if limit < 1:
            raise ValueError("max_concurrency must be positive")
        self._limit = limit
        self._semaphore = BoundedSemaphore(limit)

    @property
    def limit(self) -> int:
        """同時実行できる Argon2 の枠数。"""
        return self._limit

    def try_acquire(self) -> bool:
        """枠を1つ非ブロッキングで取る。取れなければ `False`（待たない）。"""
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        """枠を1つ返す。"""
        self._semaphore.release()

    @contextmanager
    def slot(self) -> Generator[None]:
        """枠を1つ確保して実行する。空きが無ければ待たずに `Argon2SaturatedError`。"""
        if not self.try_acquire():
            raise Argon2SaturatedError()
        try:
            yield
        finally:
            self.release()


class GuardedPasswordHasher(PasswordHasher):
    """Argon2 呼び出しを実行枠で囲む `PasswordHasher`（枠が埋まれば待たずに拒否）。

    各 Argon2 呼び出しの前後だけで枠を確保・解放する。DB 処理や正規化を含む他の処理は
    枠の外にあり、枠を長時間占有しない。
    """

    def __init__(self, guard: Argon2Guard) -> None:
        super().__init__()
        self._guard = guard

    def hash(self, password: str) -> str:
        with self._guard.slot():
            return super().hash(password)

    def verify(self, hashed: str, password: str) -> bool:
        with self._guard.slot():
            return super().verify(hashed, password)

    def dummy_verify(self) -> bool:
        with self._guard.slot():
            return super().dummy_verify()
