"""運用ログ・セキュリティイベントログ・レート制限の上限到達集計ログ
（`design/15-logging.md` §17「ログ」/ `design/10-infra-ops.md` §11）。

- **運用ログ**（`app.access`）: 1リクエスト1行の JSON。項目は request ID・ルートテンプレート・
  メソッド・status・処理時間・安全な内部エラーコードだけ。uvicorn のアクセスログは無効にする。
- **例外ログ**: request 本文・ヘッダー・Cookie・ローカル変数を含めない。記録するのは request ID
  と例外クラス名だけ（`errors.py` の `UnhandledExceptionMiddleware`）。
- **セキュリティイベントログ**（`app.security`）: 許可8種のみ。項目は 時刻・`event`・request ID・
  `ip_daily` だけ。`rate_limited` は超過した制限種別 `kind` を加える。**生IPは出さない**。
- **上限到達の集計ログ**（`app.limit`）: レート制限の上限到達（fail-closed）回数を差分で
  `rate_limit_cap_reached`（`count` のみ）として出す。数値だけを出し、生IP・秘密値は出さない。

`ip_daily` = `HMAC-SHA256(key=APP_PEPPER, msg="ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)`。
日付を含むため同じ IP でも日が変わると値が変わり、同日の相関にのみ使える（保持30日）。
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from src.app.http.client_ip import request_client_ip
from src.app.http.ratelimit import RateLimitKind
from src.shared.clock.clock import Clock, format_timestamp
from src.shared.security.security import SecretHasher

ACCESS_LOGGER_NAME = "app.access"
SECURITY_LOGGER_NAME = "app.security"
LIMIT_LOGGER_NAME = "app.limit"
EXCEPTION_LOGGER_NAME = "app.error"

# 上限到達の集計ログの `event` 名と項目（`design/15-logging.md` §17）。
CAP_REACHED_EVENT = "rate_limit_cap_reached"
CAP_REACHED_FIELDS: frozenset[str] = frozenset({"event", "count"})

# 運用ログの各行が含んでよい項目（`design/15-logging.md` §17）。
OPERATIONAL_FIELDS: frozenset[str] = frozenset(
    {"request_id", "route", "method", "status", "duration_ms", "error_code"}
)
# セキュリティイベントログの各行が含んでよい項目（`design/15-logging.md` §17）。
SECURITY_EVENT_FIELDS: frozenset[str] = frozenset(
    {"timestamp", "event", "request_id", "ip_daily", "kind"}
)


class SecurityEvent(StrEnum):
    """記録してよいセキュリティイベント8種（`design/15-logging.md` §17）。"""

    LOGIN_FAILED = "login_failed"
    LOGIN_SUCCEEDED = "login_succeeded"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_SUCCEEDED = "recovery_succeeded"
    PASSWORD_CHANGED = "password_changed"
    PROFILE_CREATED = "profile_created"
    RATE_LIMITED = "rate_limited"
    CSRF_FAILED = "csrf_failed"


def configure_logging() -> None:
    """uvicorn のアクセスログを無効化し、アプリのログを INFO で出す。

    `design/15-logging.md` §17（チャネル）/ `design/10-infra-ops.md` §11
    「アクセスログは出さない」に従う。`uvicorn --no-access-log` との二重の防御。
    """
    logging.getLogger("uvicorn.access").disabled = True
    for name in (ACCESS_LOGGER_NAME, SECURITY_LOGGER_NAME, LIMIT_LOGGER_NAME):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)


def ip_daily(ip: str, now: datetime, hasher: SecretHasher) -> str:
    """`ip_daily` を `HMAC(pepper, "ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)` で求める。

    日付を含むため、同じ IP でも日が変わると値が変わる（同日の相関にのみ使える）。
    `SecretHasher.hash("ip_daily", f"{date}:{ip}")` がこの式と一致する。
    """
    date = now.astimezone(UTC).date().isoformat()
    return hasher.hash("ip_daily", f"{date}:{ip}")


def _json_line(payload: dict[str, object]) -> str:
    """1行の JSON にする（区切りは詰め、日本語はそのまま）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _route_template(request: Request) -> str:
    """マッチしたルートのテンプレート。未マッチ（未知ルート）は固定値にする。

    生パスをそのまま出さないのは、クライアントが URL に秘密値を載せた場合の漏えいを避けるため。
    既知ルート（`/api/v1/profiles/{public_id}` 等）はテンプレートなので `public_id` も出ない。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class AccessLogMiddleware(BaseHTTPMiddleware):
    """1リクエスト1行の運用ログを許可項目だけで出す。"""

    def __init__(self, app: ASGIApp, logger: logging.Logger | None = None) -> None:
        super().__init__(app)
        self._logger = logger if logger is not None else logging.getLogger(ACCESS_LOGGER_NAME)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        payload: dict[str, object] = {
            "request_id": getattr(request.state, "request_id", ""),
            "route": _route_template(request),
            "method": request.method,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }
        error_code = getattr(request.state, "error_code", None)
        if isinstance(error_code, str):
            payload["error_code"] = error_code
        self._logger.info(_json_line(payload))
        return response


class SecurityEventLogger:
    """セキュリティイベントを許可項目だけで1行の JSON に記録する。"""

    def __init__(
        self,
        hasher_factory: Callable[[], SecretHasher],
        clock: Clock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._hasher_factory = hasher_factory
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(SECURITY_LOGGER_NAME)

    def emit(
        self,
        event: SecurityEvent,
        *,
        request: Request,
        kind: RateLimitKind | None = None,
    ) -> None:
        """`event` を1行記録する。生IPは `ip_daily` に置き換えて出す。

        `kind` は `rate_limited` の超過種別だけが持つ（`design/15-logging.md` §17）。
        それ以外のイベントは指定しない。
        """
        now = self._clock.now()
        payload: dict[str, object] = {
            "timestamp": format_timestamp(now),
            "event": event.value,
            "request_id": getattr(request.state, "request_id", ""),
            "ip_daily": ip_daily(request_client_ip(request), now, self._hasher_factory()),
        }
        if kind is not None:
            payload["kind"] = kind.value
        self._logger.info(_json_line(payload))


class RateLimitCapLogger:
    """レート制限の上限到達（fail-closed）の累積カウンタを差分で集計ログへ出す。

    `design/15-logging.md` §17「上限到達は運用ログ（`rate_limit_cap_reached`）で数値
    （`count`）のみ観測」。累積カウンタの前回値との差分だけを出す。プロセス再起動・
    リミッタ再生成でカウンタが減った場合は基準を取り直し、新プロセス分をそのまま計上する
    （負の差分を出さないガード）。生IP・秘密値は出さない。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else logging.getLogger(LIMIT_LOGGER_NAME)
        self._last_count = 0

    def emit_delta(self, count: int) -> None:
        """累積 `count` を受け取り、前回からの増分があれば1行出す。"""
        previous = self._last_count
        self._last_count = count
        delta = count if count < previous else count - previous
        if delta <= 0:
            return
        self._logger.info(_json_line({"event": CAP_REACHED_EVENT, "count": delta}))
