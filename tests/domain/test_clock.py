"""`shared/clock` のテスト。

このファイルが守るべき設計上の約束は次の3点。いずれも `design/08-database.md` §9 共通規約と
`design/03-domain-model.md` §4（Port表・時刻の意味）に由来する。

1. 時刻は UTC の timezone-aware で、DB に保存する形式は
   `YYYY-MM-DDTHH:MM:SS.ffffffZ`（27文字）に1つだけ。
2. 固定長なので、DB に保存した文字列の辞書順比較がそのまま時刻順になる。
3. 時刻の生成は `Clock` ポート経由だけで、`FakeClock` でテスト中は固定できる。

3番目については `datetime.now()` を直接呼べるのは `src/shared/clock/clock.py` の
`SystemClock` だけでなければならない（M11 のアーキテクチャテストは、このファイルを
唯一の例外として許可する）。
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.shared.clock.clock import (
    TIMESTAMP_LENGTH,
    Clock,
    SystemClock,
    format_timestamp,
    parse_timestamp,
)
from tests.support.fake_clock import FakeClock

# 往復テストに使う観測点。端の値（年の最小・最大、マイクロ秒の最小・最大）と、UTC 以外の
# タイムゾーン（+09:00）を混ぜて、formatter と parser の取り違えやゼロ埋め漏れを検出させる。
ROUND_TRIP_VALUES = [
    datetime(2026, 10, 3, 4, 5, 6, 789012, tzinfo=UTC),
    datetime(2026, 10, 3, 4, 5, 6, 0, tzinfo=UTC),
    datetime(2026, 10, 3, 4, 5, 6, 999999, tzinfo=UTC),
    datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
    datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    datetime(2026, 10, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=9))),
]


def test_format_timestamp_matches_design_format() -> None:
    """出力の書式が設計書どおりであること。

    27文字という前提は設計書の記述そのもの。出力は文字列なので、定数と実際の書式が
    食い違わないよう固定する。いずれかの定数が置き換わってもここで落ちるようにする。
    """
    formatted = format_timestamp(datetime(2026, 10, 3, 4, 5, 6, 789012, tzinfo=UTC))
    assert formatted == "2026-10-03T04:05:06.789012Z"
    assert TIMESTAMP_LENGTH == 27
    assert len(formatted) == TIMESTAMP_LENGTH


def test_every_round_trip_value_is_27_characters() -> None:
    """観測点がどれでも必ず27文字であること。

    「文字列比較で時刻順にできる」は固定長だからです。桁数が減れば前提が崩れる。
    `strftime` の `%Y` は環境によってゼロ埋めしないため、年の端の値でも 27 を確かめる。
    """
    for value in ROUND_TRIP_VALUES:
        assert len(format_timestamp(value)) == TIMESTAMP_LENGTH


def test_parse_timestamp_is_the_inverse_of_format_timestamp() -> None:
    """保存した文字列を Repository から読み戻せること。

    `parse_timestamp` は必ず timezone-aware な UTC を返す。naive な datetime が
    返ると `design/03-domain-model.md` §4「時刻はすべてUTCのtimezone-aware」に反する。
    """
    for value in ROUND_TRIP_VALUES:
        parsed = parse_timestamp(format_timestamp(value))
        assert parsed == value.astimezone(UTC)
        assert parsed.tzinfo is UTC
        assert parsed.utcoffset() == timedelta(0)


def test_non_utc_offset_is_converted_to_utc() -> None:
    """UTC 以外の aware な時刻を渡しても UTC に寄せてから整形すること。

    丸め/切捨てを許すと記録した時刻と実際の時刻がずれる。+09:00 の 13:05:06.007000 が
    UTC では 04:05:06.007000 になることを確認する（このケースでは日付をまたがない）。
    """
    value = datetime(2026, 10, 3, 13, 5, 6, 7000, tzinfo=timezone(timedelta(hours=9)))
    assert format_timestamp(value) == "2026-10-03T04:05:06.007000Z"


def test_string_comparison_matches_chronological_order() -> None:
    """文字列の辞書順と時刻順が一致すること。

    `expires_at > created_at` のようなDBのCHECKや、`WHERE expires_at <= ?` のような
    絞り込みを文字列比較に委ねている（design/08-database.md §9 共通規約・主要クエリ）。
    これが成り立つのは固定長だからで、年をまたぐ順序とマイクロ秒の桁がそろっているかを見る。
    """
    moments = [
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
    ]
    formatted = [format_timestamp(moment) for moment in moments]
    assert sorted(formatted) == [format_timestamp(moment) for moment in sorted(moments)]


def test_naive_datetime_is_rejected() -> None:
    """naive な時刻を受け入れないこと。

    naive な値がそのまま入ると、UTC として解釈されるか ローカルとして解釈されるかで
    記録した時刻がずれる。`design/14-dod-conventions.md` §16「naiveな時刻は禁止」に対する
    ガードで、`FakeClock` の初期値にも同じ制約を効かせる。
    """
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 10, 3, 4, 5, 6))
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 10, 3, 4, 5, 6))


def test_malformed_timestamp_is_rejected() -> None:
    """固定長になっていない文字列を DB 由来データとして受け入れないこと。

    精度が足りない文字列（マイクロ秒なし）や、UTC 表記ではない文字列（オフセット付き）を
    弾く。文字列比較の前提が崩れる値を黙って通さないためのテスト。
    """
    with pytest.raises(ValueError):
        parse_timestamp("2026-10-03T04:05:06Z")
    with pytest.raises(ValueError):
        parse_timestamp("2026-10-03T04:05:06.789012+00:00")


def test_system_clock_returns_aware_utc_now() -> None:
    """実時計アダプターが aware な UTC を返すこと。

    `src/shared/clock/` が `datetime.now` を直接呼べる唯一の場所であることも、
    戻り値が必ずUTCの aware であることで担保する（naive だと静かに混入する）。
    """
    before = datetime.now(UTC)
    moment = SystemClock().now()
    after = datetime.now(UTC)
    assert moment.tzinfo is UTC
    assert moment.utcoffset() == timedelta(0)
    assert before <= moment <= after


def test_fake_clock_holds_time_and_advances_only_on_demand() -> None:
    """`FakeClock` が時刻を固定できること。

    Sessionの14日満了（`design/03-domain-model.md` §4「Sessionは14日の絶対期限」）のように
    実時間だと検証できない条件は、`FakeClock` を手動で進めることでしかテストできない。
    `advance` を呼ぶまで時刻が動かないこと、`set` で差し替えられること、
    `set` にも naive 拒否が効くことを確認する。
    """
    start = datetime(2026, 10, 3, 4, 5, 6, 789012, tzinfo=UTC)
    clock = FakeClock(start)
    assert clock.now() == start
    assert clock.now() == start
    clock.advance(timedelta(days=1))
    assert clock.now() == start + timedelta(days=1)
    clock.set(datetime(2030, 1, 1, tzinfo=UTC))
    assert clock.now() == datetime(2030, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        clock.set(datetime(2030, 1, 1))


def test_both_clock_implementations_satisfy_the_port() -> None:
    """本番用とテスト用の両アダプターが `Clock` ポートを満たすこと。

    ポートは `now() -> datetime` だけを要求する。実装が置き換わっても
    呼び出し側が `Clock` に依存し続けられることを保証する。
    """
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(datetime(2026, 10, 3, tzinfo=UTC)), Clock)
