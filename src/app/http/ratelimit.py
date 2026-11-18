"""レート制限のHTTP基盤：限界値の集約・429経路（`design/07-security.md` §8 Rate limit /
`design/06-errors.md` §7 H-13 / `design/05-api-contract.md` §6 判定順）。

5つの制限（ログインaccount / ログインIP / 回復 / パスワード変更 / Profile作成）の
回数・窓・key 種別を1箇所に集約する。判定そのものは `RateLimiter` ポート
（`src/shared/ratelimit`）が行い、超過は `RateLimitExceededError` で表して
`Retry-After` 付き 429 に変換する。各エンドポイントへの適用は T-10-03。
"""

from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum
from typing import NamedTuple

from fastapi import Request
from starlette.responses import JSONResponse, Response

from src.app.errors import AppError
from src.shared.errors.errors import ErrorCode, status_for
from src.shared.ratelimit.ratelimit import RateLimiter
from src.shared.web.web import build_error_body


class RateLimitScope(StrEnum):
    """レート制限の key 種別（`design/07-security.md` §8 の key 列）。"""

    IP = "ip"
    ACCOUNT = "account"
    SESSION = "session"


class RateLimitKind(StrEnum):
    """レート制限の種別（`design/07-security.md` §8 の各制限行）。

    閉じた集合。`rate_limited` イベントの項目 `kind` に、超過した制限の種別を記録する
    （§8「セキュリティイベントログ」）。
    """

    LOGIN_ACCOUNT = "login_account"
    LOGIN_IP = "login_ip"
    RECOVERY_MINUTE = "recovery_minute"
    RECOVERY_HOUR = "recovery_hour"
    PASSWORD_CHANGE = "password_change"
    PROFILE_CREATE = "profile_create"


class RateLimit(NamedTuple):
    """1つのレート制限の限界値（種別・key 種別・回数・窓）。"""

    kind: RateLimitKind
    scope: RateLimitScope
    limit: int
    window: timedelta


# `design/07-security.md` §8「Rate limit」の暫定値。限界値は1箇所（ここ）に集約する。
LOGIN_ACCOUNT = RateLimit(
    RateLimitKind.LOGIN_ACCOUNT, RateLimitScope.ACCOUNT, 5, timedelta(minutes=1)
)
LOGIN_IP = RateLimit(RateLimitKind.LOGIN_IP, RateLimitScope.IP, 20, timedelta(minutes=1))
RECOVERY_PER_MINUTE = RateLimit(
    RateLimitKind.RECOVERY_MINUTE, RateLimitScope.IP, 5, timedelta(minutes=1)
)
RECOVERY_PER_HOUR = RateLimit(
    RateLimitKind.RECOVERY_HOUR, RateLimitScope.IP, 20, timedelta(hours=1)
)
PASSWORD_CHANGE = RateLimit(
    RateLimitKind.PASSWORD_CHANGE, RateLimitScope.SESSION, 5, timedelta(minutes=1)
)
PROFILE_CREATE = RateLimit(RateLimitKind.PROFILE_CREATE, RateLimitScope.IP, 10, timedelta(hours=1))

ALL_RATE_LIMITS: tuple[RateLimit, ...] = (
    LOGIN_ACCOUNT,
    LOGIN_IP,
    RECOVERY_PER_MINUTE,
    RECOVERY_PER_HOUR,
    PASSWORD_CHANGE,
    PROFILE_CREATE,
)


class RateLimitExceededError(AppError):
    """H-13: レート制限超過。

    `Retry-After` に渡す秒（1以上）と、超過した制限種別 `kinds` を持つ。複数の制限が
    同時に超過した場合は超過順にすべて載せる（§8「同時に複数の制限を超過した場合は、
    超過した各制限につき1件記録する」）。
    """

    def __init__(self, retry_after: int, kinds: tuple[RateLimitKind, ...] = ()) -> None:
        super().__init__(ErrorCode.RATE_LIMITED)
        self.retry_after = max(1, retry_after)
        self.kinds = kinds


async def rate_limit_handler(request: Request, exc: Exception) -> Response:
    """H-13: 429 `RATE_LIMITED` ＋ `Retry-After` を返す（入力値・内部理由は出さない）。"""
    if not isinstance(exc, RateLimitExceededError):
        raise exc
    return JSONResponse(
        build_error_body(ErrorCode.RATE_LIMITED),
        status_code=status_for(ErrorCode.RATE_LIMITED),
        headers={"Retry-After": str(exc.retry_after)},
    )


def enforce(
    limiter: RateLimiter,
    checks: Iterable[tuple[RateLimit, str]],
) -> None:
    """指定の (制限, key) をすべて判定し、1つでも超過なら 429 用の例外を送出する。

    成功・失敗を区別せず**全試行を計上する**（`design/07-security.md` §8）。複数の制限が
    同時に超過した場合は、超過順に種別を集め、待ち時間が最大の `Retry-After` を返す。判定は
    `run_request_pipeline` の `rate_limit` 段（入力検証の後）で呼ぶ前提
    （`design/05-api-contract.md` §6 判定順）。
    """
    retry_after = 0
    exceeded: list[RateLimitKind] = []
    for policy, key in checks:
        result = limiter.check(key, policy.limit, policy.window)
        if not result.allowed:
            retry_after = max(retry_after, result.retry_after)
            exceeded.append(policy.kind)
    if exceeded:
        raise RateLimitExceededError(retry_after, tuple(exceeded))
