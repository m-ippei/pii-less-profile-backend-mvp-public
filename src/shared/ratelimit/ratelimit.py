"""共有レート制限（`design/07-security.md` §8「Rate limit」）。

`RateLimiter` ポートの背後にインメモリ実装を置く。MVPはプロセス内メモリで、
期限切れ key は検査時に破棄し、key 数は最大10,000に抑える。判定は入力検証の
後に行われる想定で、成功・失敗の区別なく全試行を計上する。

`plan/archive/proposals/rate-limit-design.md` §2 の結論に従い、次を満たす。

1. メモリ有界化: 各 key は窓内のタイムスタンプを最大 `limit` 個だけ保持する。
   窓内で既に `limit` 個に達した key への新たな試行は計上せず拒否する。
2. 上限到達時の fail-closed: key 数が上限（既定10,000。`RATE_LIMIT_MAX_KEYS` で
   設定可能。[`design/10-infra-ops.md`](../../../design/10-infra-ops.md) §11）に達したら
   新規 key は登録せず拒否する（既存 key の判定は継続）。`Retry-After` は窓幅を返す。
3. `Retry-After` の正直化: 拒否時は「その key の最古の計上が窓から落ちるまでの
   残り秒」を切り上げて返す（最低1）。
4. 並行性: 判定・計上は単一の `threading.Lock` で直列化する（エンドポイントは
   `def`＝threadpool で動くため）。

判定に使う時刻は `Clock` ポート経由で取得し、`datetime.now()` を直接呼ばない
（`design/14-dod-conventions.md` §16 実装規約）。
"""

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from threading import Lock
from typing import Protocol, runtime_checkable

from src.shared.clock.clock import Clock

MAX_KEYS = 10_000


def max_keys_from_env() -> int:
    """`RATE_LIMIT_MAX_KEYS` を読む。未設定は既定 `MAX_KEYS`（10,000）。

    不正（非整数・1未満）は `ValueError`（起動時に fail-closed で拒否）。
    `design/07-security.md` §8「Rate limit」/ `design/10-infra-ops.md` §11「設定」。
    """
    raw = os.getenv("RATE_LIMIT_MAX_KEYS")
    if raw is None:
        return MAX_KEYS
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("RATE_LIMIT_MAX_KEYS must be an integer") from error
    if value < 1:
        raise ValueError("RATE_LIMIT_MAX_KEYS must be positive")
    return value


@dataclass(frozen=True)
class RateLimitResult:
    """1回の判定結果。拒否時は `Retry-After` に渡す秒（1以上）を持つ。"""

    allowed: bool
    retry_after: int


@runtime_checkable
class RateLimiter(Protocol):
    """key と窓で許容可否を判定するポート（`design/03-domain-model.md` §4 Port）。"""

    def check(self, key: str, limit: int, window: timedelta) -> RateLimitResult: ...


@dataclass
class _Entry:
    """1つの key の計上履歴。計上時刻を古い順に最大 `limit` 個だけ保持する。"""

    window: timedelta
    attempts: deque[datetime] = field(default_factory=deque)


def _retry_after_seconds(until: datetime, now: datetime) -> int:
    """`until` までの残り秒を切り上げて返す（最低1）。"""
    return max(1, ceil((until - now).total_seconds()))


class InMemoryRateLimiter:
    """スライディングウィンドウのインメモリ実装。

    計上時刻を key ごとに古い順で保持し、検査のたびに窓から抜けた計上を捨てる。
    許容可否は窓内の計上数と `limit` の比較で決め、拒否時は「あと何秒で窓内の
    計上が `limit` 以下になるか」を切り上げて返す。判定・計上は単一の Lock で
    直列化する。
    """

    def __init__(self, clock: Clock, max_keys: int = MAX_KEYS) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._clock = clock
        self._max_keys = max_keys
        self._entries: dict[str, _Entry] = {}
        self._cap_reached_count = 0
        self._lock = Lock()

    @property
    def key_count(self) -> int:
        """現在保持している key 数（テストと運用の観測用）。"""
        with self._lock:
            return len(self._entries)

    @property
    def max_keys(self) -> int:
        """この実装の key 上限（テストと運用の観測用）。"""
        return self._max_keys

    @property
    def cap_reached_count(self) -> int:
        """上限到達（fail-closed）で新規 key を拒否した累積回数（運用ログの観測用）。

        `design/15-logging.md` §17「上限到達は運用ログ（`rate_limit_cap_reached`）で
        観測」。プロセス内の累積値で、再起動で 0 に戻る。
        """
        with self._lock:
            return self._cap_reached_count

    def attempt_count(self, key: str) -> int:
        """`key` が窓内に保持している計上数（テストと運用の観測用）。"""
        with self._lock:
            entry = self._entries.get(key)
            return 0 if entry is None else len(entry.attempts)

    def check(self, key: str, limit: int, window: timedelta) -> RateLimitResult:
        now = self._clock.now()
        with self._lock:
            self._evict_expired(now)
            if limit <= 0:
                return RateLimitResult(
                    allowed=False, retry_after=_retry_after_seconds(now + window, now)
                )
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self._max_keys:
                    self._cap_reached_count += 1
                    return RateLimitResult(
                        allowed=False, retry_after=_retry_after_seconds(now + window, now)
                    )
                entry = _Entry(window=window)
                self._entries[key] = entry
            else:
                entry.window = window
            if len(entry.attempts) >= limit:
                # 有界化: 窓内で `limit` 個に達した key への試行は計上せず拒否する。
                return RateLimitResult(
                    allowed=False,
                    retry_after=_retry_after_seconds(entry.attempts[0] + window, now),
                )
            entry.attempts.append(now)
            return RateLimitResult(allowed=True, retry_after=0)

    def _evict_expired(self, now: datetime) -> None:
        """窓から抜けた計上を捨て、空になった key を破棄する。"""
        expired = [key for key, entry in self._entries.items() if self._discard(entry, now)]
        for key in expired:
            del self._entries[key]

    @staticmethod
    def _discard(entry: _Entry, now: datetime) -> bool:
        threshold = now - entry.window
        while entry.attempts and entry.attempts[0] <= threshold:
            entry.attempts.popleft()
        return not entry.attempts
