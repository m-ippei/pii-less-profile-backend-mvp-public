"""CSRF 検証と `csrf` cookie の発行（design/07-security.md §8 CSRF /
design/05-api-contract.md §6 共通規約）。

`GET`（および `HEAD`/`OPTIONS`）以外の全リクエストに CSRF 検証を掛ける。3条件を
**すべて**満たす必要があり、1つでも欠ければ H-12 `CSRF_FAILED` 403 にする。

1. `Origin` ヘッダーが `PUBLIC_BASE_URL` のオリジンと一致（欠落も違反）
2. 本文を持つリクエストは `Content-Type: application/json`
3. `X-CSRF-Token` ヘッダーと `__Host-csrf` cookie が両方非空で、定数時間比較で一致

`GET` 応答（静的ファイル配信を含む）で `csrf` cookie が無ければ発行する。トークンは
`SecretGenerator` ポートで生成し、保存しない（cookie のみ。§8 秘密値と保存）。
"""

import hmac
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from src.app.http.cookies import CSRF_COOKIE, set_csrf_cookie
from src.app.http.logging import SecurityEvent, SecurityEventLogger
from src.shared.errors.errors import ErrorCode
from src.shared.security.security import SecretGenerator
from src.shared.web.web import build_error_body

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSRF_HEADER = "X-CSRF-Token"


def _origin_of(url: str) -> str:
    """URL から `scheme://host[:port]` を取り出す。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _has_body(request: Request) -> bool:
    """本文を持つリクエストか（`Content-Length` か `Transfer-Encoding` で判定）。"""
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length != "0":
        return True
    return request.headers.get("transfer-encoding") is not None


def _is_json(request: Request) -> bool:
    """`Content-Type` が `application/json` か（パラメータは無視）。"""
    content_type = request.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class CsrfMiddleware(BaseHTTPMiddleware):
    """`GET` 以外に CSRF 検証を掛け、`GET` で `csrf` cookie を発行する。"""

    def __init__(
        self,
        app: ASGIApp,
        secret_generator: SecretGenerator | None = None,
        security_logger: SecurityEventLogger | None = None,
    ) -> None:
        super().__init__(app)
        self._secret_generator = (
            secret_generator if secret_generator is not None else SecretGenerator()
        )
        self._security_logger = security_logger

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in SAFE_METHODS:
            response = await call_next(request)
            if request.cookies.get(CSRF_COOKIE) is None:
                set_csrf_cookie(response, self._secret_generator.csrf_token())
            return response
        if not self._is_valid(request):
            if self._security_logger is not None:
                self._security_logger.emit(SecurityEvent.CSRF_FAILED, request=request)
            return JSONResponse(build_error_body(ErrorCode.CSRF_FAILED), status_code=403)
        return await call_next(request)

    def _is_valid(self, request: Request) -> bool:
        expected_origin = _origin_of(os.environ.get("PUBLIC_BASE_URL", ""))
        origin = request.headers.get("origin")
        if origin is None or _origin_of(origin) != expected_origin:
            return False
        if _has_body(request) and not _is_json(request):
            return False
        header_token = request.headers.get(CSRF_HEADER)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        if not header_token or not cookie_token:
            return False
        return hmac.compare_digest(header_token, cookie_token)
