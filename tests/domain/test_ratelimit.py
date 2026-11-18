"""`shared/ratelimit` のテスト。

このファイルが守るべき設計上の約束は次の5点。いずれも `design/07-security.md` §8
「Rate limit」と `design/03-domain-model.md` §4（Port表）に由来する。

1. `RateLimiter` は key と窓で許容可否を判定し、拒否時は `Retry-After` の秒（1以上）を返す。
2. 固定ウィンドウではなくスライディングウィンドウで、窓の境界で即座に許さない。
3. 成功・失敗の区別なく全試行を計上する。
4. 期限切れ key は検査時に破棄し、key 数は最大10,000を超えない。
5. 判定に使う時刻は `Clock` ポート経由で、テストでは `FakeClock` で固定する。
"""

import threading
from datetime import UTC, datetime, timedelta

from src.shared.ratelimit.ratelimit import (
    MAX_KEYS,
    InMemoryRateLimiter,
    RateLimiter,
)
from tests.support.fake_clock import FakeClock

BASE = datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)


def _limiter(max_keys: int = MAX_KEYS) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(FakeClock(BASE), max_keys=max_keys)


def test_allows_up_to_limit_then_denies() -> None:
    """`limit` 回までは許容し、`limit+1` 回目で拒否すること。

    制限の基本単位。ここがずれると 5回/分 などの設計値が意味を失う。
    """
    limiter = _limiter()
    window = timedelta(seconds=60)
    for _ in range(3):
        assert limiter.check("k", 3, window).allowed
    assert not limiter.check("k", 3, window).allowed


def test_retry_after_is_at_least_one_when_denied() -> None:
    """拒否時の `Retry-After` が1秒以上、許容時は0であること。

    `Retry-After` に 0 や負値を返すと、クライアントが無限に即再試行できる。
    秒未満の端数は切り上げて最低1秒にする。
    """
    limiter = _limiter()
    window = timedelta(seconds=60)
    assert limiter.check("k", 1, window).retry_after == 0
    denied = limiter.check("k", 1, window)
    assert not denied.allowed
    assert denied.retry_after >= 1


def test_sliding_window_denies_at_fixed_window_boundary() -> None:
    """固定ウィンドウなら窓が切り替わる境界でも、スライディングでは拒否すること。

    固定ウィンドウだと窓の境目で直前に使った回数が無視され、`limit` の2倍を一気に
    通してしまう。90秒・95秒に2回使ったあと、固定ウィンドウの境界である100秒で
    まだ窓内に2件が残っていることを確認する。
    """
    clock = FakeClock(BASE)
    limiter = InMemoryRateLimiter(clock)
    window = timedelta(seconds=100)
    clock.advance(timedelta(seconds=90))
    assert limiter.check("k", 2, window).allowed
    clock.advance(timedelta(seconds=5))
    assert limiter.check("k", 2, window).allowed
    clock.advance(timedelta(seconds=5))
    assert not limiter.check("k", 2, window).allowed


def test_window_slides_forward_after_the_oldest_attempt_expires() -> None:
    """最も古い計上が窓から抜けると、再び許容されること。

    スライディングウィンドウは「最後の計上から窓経過」ではなく「各計上が順に
    窓から抜ける」ことで回復する。拒否後、最後の計上から窓を超えて時間が進むと
    全件が期限切れになり、許容に戻ることを確認する。
    """
    clock = FakeClock(BASE)
    limiter = InMemoryRateLimiter(clock)
    window = timedelta(seconds=100)
    clock.advance(timedelta(seconds=90))
    assert limiter.check("k", 2, window).allowed
    clock.advance(timedelta(seconds=5))
    assert limiter.check("k", 2, window).allowed
    clock.advance(timedelta(seconds=5))
    assert not limiter.check("k", 2, window).allowed
    clock.advance(timedelta(seconds=101))
    assert limiter.check("k", 2, window).allowed


def test_all_attempts_are_counted_without_success_failure_distinction() -> None:
    """成功・失敗の区別をせず、呼ばれた回数だけを計上すること。

    `design/07-security.md` §8「全試行を計上する」は、ログイン成功と失敗で
    計上を分けると存在の有無が漏れるため。ポートには成否の引数が無く、呼び出しは
    すべて試行として数えられる。3回で拒否されたあと、窓が過ぎれば回復する。
    """
    clock = FakeClock(BASE)
    limiter = InMemoryRateLimiter(clock)
    window = timedelta(seconds=60)
    for _ in range(3):
        assert limiter.check("k", 3, window).allowed
    assert not limiter.check("k", 3, window).allowed
    clock.advance(timedelta(seconds=61))
    assert limiter.check("k", 3, window).allowed


def test_expired_keys_are_discarded_at_inspection() -> None:
    """期限を過ぎた key が検査時に破棄され、key 数が減ること。

    インメモリ実装は無制限にメモリを使わない。誰かが検査するたびに期限切れ key を
    捨てる。2件が期限切れになったあとの検査で key 数が1件に減ることを確認する。
    """
    clock = FakeClock(BASE)
    limiter = InMemoryRateLimiter(clock)
    window = timedelta(seconds=60)
    assert limiter.check("a", 1, window).allowed
    assert limiter.check("b", 1, window).allowed
    assert limiter.key_count == 2
    clock.advance(timedelta(seconds=61))
    assert limiter.check("c", 1, window).allowed
    assert limiter.key_count == 1


def test_key_count_never_exceeds_the_configured_maximum() -> None:
    """上限に達すると新しい key を登録せず、key 数が上限を超えないこと。

    上限到達時の挙動は設計書が「最大10,000 key」とだけ書く。最小の側として、
    新規 key は登録せず拒否する（既存 key は判定を続ける）。小さい上限で
    振る舞いを確認し、本番値は `MAX_KEYS` が10,000であること別途確かめる。
    """
    limiter = _limiter(max_keys=3)
    window = timedelta(hours=1)
    for key in ("k1", "k2", "k3"):
        assert limiter.check(key, 2, window).allowed
    assert limiter.key_count == 3
    denied = limiter.check("k4", 2, window)
    assert not denied.allowed
    assert limiter.key_count == 3


def test_existing_keys_are_still_judged_when_at_capacity() -> None:
    """上限到達中でも、既存 key の判定は続くこと。

    新規 key を登録しないだけで、既存 key の回数制限まで止めてはならない。
    """
    limiter = _limiter(max_keys=2)
    window = timedelta(hours=1)
    assert limiter.check("k1", 2, window).allowed
    assert limiter.check("k2", 2, window).allowed
    assert not limiter.check("k3", 2, window).allowed
    assert limiter.check("k1", 2, window).allowed


def test_cap_reached_count_counts_only_new_key_rejections_at_capacity() -> None:
    """上限到達（fail-closed）で新規 key を拒否した回数だけを数えること。

    `design/15-logging.md` §17「上限到達は運用ログ（`rate_limit_cap_reached`）で観測」。
    通常の許容や、既存 key の回数超過による拒否では増えない。
    """
    limiter = _limiter(max_keys=2)
    window = timedelta(hours=1)
    assert limiter.check("k1", 5, window).allowed
    assert limiter.check("k2", 5, window).allowed
    assert limiter.cap_reached_count == 0

    assert not limiter.check("k3", 5, window).allowed  # 上限到達（新規 key）
    assert not limiter.check("k4", 5, window).allowed  # 上限到達（新規 key）
    assert limiter.cap_reached_count == 2

    for _ in range(4):
        assert limiter.check("k1", 5, window).allowed
    assert not limiter.check("k1", 5, window).allowed  # 既存 key の回数超過
    assert limiter.cap_reached_count == 2


def test_cap_reached_count_stays_zero_without_capacity_pressure() -> None:
    """上限に達していなければ、通常の拒否でも `cap_reached_count` は増えないこと。"""
    limiter = _limiter()
    window = timedelta(seconds=60)
    assert limiter.check("k", 1, window).allowed
    assert not limiter.check("k", 1, window).allowed
    assert limiter.cap_reached_count == 0


def test_different_keys_are_counted_independently() -> None:
    """異なる key は独立して数えられること。

    IP のみの key と account 単位の key が互いに影響し合うと、制限が早すぎたり
    緩すぎたりする。
    """
    limiter = _limiter()
    window = timedelta(seconds=60)
    assert limiter.check("a", 1, window).allowed
    assert limiter.check("b", 1, window).allowed
    assert not limiter.check("a", 1, window).allowed


def test_default_maximum_is_ten_thousand() -> None:
    """本番の key 上限が設計書どおり10,000であること。

    `design/07-security.md` §8「最大10,000 key」。上限値そのものを固定する。
    """
    assert MAX_KEYS == 10_000


def test_in_memory_limiter_satisfies_the_port() -> None:
    """インメモリ実装が `RateLimiter` ポートを満たすこと。

    呼び出し側が具象ではなくポートに依存し続けられることを保証する。
    """
    assert isinstance(_limiter(), RateLimiter)


def test_retained_attempts_are_bounded_by_the_limit() -> None:
    """窓内で `limit` 個に達したあとの試行は計上されず、保持数が `limit` を超えないこと。

    `design/07-security.md` §8「各 key は窓内のタイムスタンプを最大 `limit` 個だけ
    保持する」。無制限に計上を積むと、攻撃者が同一 key へ大量に試行するだけでメモリを
    使い果たせる。`limit+1` 回目以降を拒否しつつ、保持数を `limit` に保つ。
    """
    limiter = _limiter()
    window = timedelta(seconds=60)
    for _ in range(3):
        assert limiter.check("k", 3, window).allowed
    for _ in range(10):
        assert not limiter.check("k", 3, window).allowed
    assert limiter.attempt_count("k") == 3


def test_retry_after_is_derived_from_the_oldest_attempt() -> None:
    """拒否時の `Retry-After` が最古の計上が窓から落ちるまでの残り秒であること。

    固定値（例: 1秒）だとクライアントが早すぎる再試行を繰り返す。最古の計上から
    窓ぶんが経過するまでの秒を切り上げて返す。
    """
    clock = FakeClock(BASE)
    limiter = InMemoryRateLimiter(clock)
    window = timedelta(seconds=100)
    clock.advance(timedelta(seconds=40))
    assert limiter.check("k", 1, window).allowed
    clock.advance(timedelta(seconds=10))
    denied = limiter.check("k", 1, window)
    assert not denied.allowed
    assert denied.retry_after == 90


def test_capacity_rejection_returns_the_window_width() -> None:
    """上限到達で新規 key を拒否するとき、`Retry-After` が窓幅になること。

    `design/07-security.md` §8「上限到達で対象 key が無い場合は窓幅を返す」。
    登録していない key なので最古の計上が無く、窓幅が正直な待ち時間になる。
    """
    limiter = _limiter(max_keys=1)
    window = timedelta(hours=1)
    assert limiter.check("existing", 2, window).allowed
    denied = limiter.check("new", 2, window)
    assert not denied.allowed
    assert denied.retry_after == 3600
    assert limiter.key_count == 1


def test_concurrent_checks_never_allow_more_than_the_limit() -> None:
    """複数スレッドから並行に判定しても、許容数が `limit` を超えないこと。

    エンドポイントは `def`（threadpool）で動くため、インメモリ実装の判定・計上は
    単一の Lock で直列化される必要がある。計上と判定が分離すると `limit` を超えて
    許容してしまう。50 スレッド・`limit=10` でちょうど10回だけ許容されることを見る。
    """
    limiter = _limiter()
    window = timedelta(seconds=60)
    results: list[bool] = []
    start = threading.Barrier(50)

    def worker() -> None:
        start.wait()
        results.append(limiter.check("k", 10, window).allowed)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 10
    assert limiter.attempt_count("k") == 10
