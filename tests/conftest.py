"""全テスト共通の fixture。

レート制限はアプリ全体で共有するインメモリ実装（`src.main.rate_limiter`）なので、テスト間で
状態が残ると無関係なテストが 429 で落ちる（例: Profile作成 10回/時はスイート全体で容易に
超える）。テストごとに新しい `InMemoryRateLimiter` へ差し替えて隔離する。

`tests/e2e` は実サーバーへ HTTP する別プロセスなので影響しない（この差し替えは無害）。
"""

import pytest

import src.main as main
from src.app.http.logging import RateLimitCapLogger
from src.shared.clock.clock import SystemClock
from src.shared.ratelimit.ratelimit import InMemoryRateLimiter


@pytest.fixture(autouse=True)
def _isolate_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストの前に、結線済みの共有 `RateLimiter` を空のインスタンスへ差し替える。

    上限到達の集計ロガーもカウンタ基準を持つため、あわせて新しいインスタンスへ差し替えて
    テスト間で基準が残らないようにする。
    """
    monkeypatch.setattr(main, "rate_limiter", InMemoryRateLimiter(SystemClock()))
    monkeypatch.setattr(main, "rate_limit_caps", RateLimitCapLogger())
