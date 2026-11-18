from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
TIMESTAMP_LENGTH = 27


@runtime_checkable
class Clock(Protocol):
    """UTC の timezone-aware な現在時刻を返すポート（design/03-domain-model.md §4 Port）。"""

    def now(self) -> datetime: ...


class SystemClock:
    """実行環境で使う `Clock`。

    `datetime.now` を直接呼べるのはこのクラスだけ（design/08-database.md §9
    「生成は `Clock` アダプターのみ」）。
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


def require_aware(value: datetime) -> datetime:
    """naive な datetime を拒否し、timezone-aware な UTC に寄せる。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed")
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SS.ffffffZ`（27文字）にする。

    固定長なので、文字列比較が時刻順と一致する（design/08-database.md §9）。
    `strftime` は年4桁のゼロ埋めを保証しないため、穴埋めして組み立てる。
    """
    moment = require_aware(value)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        f".{moment.microsecond:06d}Z"
    )


def parse_timestamp(text: str) -> datetime:
    """`format_timestamp` の出力を timezone-aware な UTC に戻す。"""
    if len(text) != TIMESTAMP_LENGTH:
        raise ValueError(f"timestamp must be exactly {TIMESTAMP_LENGTH} characters")
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
