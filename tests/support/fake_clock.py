from datetime import datetime, timedelta

from src.shared.clock.clock import require_aware


class FakeClock:
    """`Clock` ポートのテスト実装。時刻を固定し、`advance` でだけ動かす。

    `freezegun` 等は使わない（design/11-testing.md §12）。
    """

    def __init__(self, start: datetime) -> None:
        self._now = require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        self._now = require_aware(value)
