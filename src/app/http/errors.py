"""例外ハンドラと想定外例外の境界（design/06-errors.md §7 /
design/05-api-contract.md §6 / design/07-security.md §8）。

FastAPI 標準の 422（`{"detail":[...]}`）を捨て、失敗応答を
`{"error":{"code","message","fields"}}` に統一する。文言は `design/06-errors.md` §7 の
固定表からのみ取り、入力値・内部理由・秘密値は応答へ出さない。

登録する分岐:

- `RequestValidationError`（H-10）→ 422 `VALIDATION_ERROR`。`fields` は `field` と
  `code` だけで、pydantic の `input`（入力値）は決して写さない
- 未知ルート / メソッド不一致（H-18）→ 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED`
- `InvalidSessionError`（H-5 / H-6 / H-11）→ 401 `INVALID_SESSION` かつ `ap_session` を削除
- アプリケーションエラー（`AppError` / `IdentityError` / `ProfileApplicationError`）→
  `status_for(code)`
- `sqlite3.OperationalError` の busy / timeout（H-17）→ 503 `SERVICE_UNAVAILABLE` +
  `Retry-After`。それ以外（H-9）→ 500 `INTERNAL_ERROR`
- 想定外例外（H-9）→ 500 `INTERNAL_ERROR`

想定外例外だけは例外ハンドラではなく内側の ASGI ミドルウェアで捕まえる。Starlette は
`Exception` / 500 のハンドラを最外の `ServerErrorMiddleware` へ移すため、`add_middleware`
で登録した `CommonHeadersMiddleware`（T-07-01）の外側になり、500 に共通ヘッダーと
`Cache-Control: no-store` が付かない。`UnhandledExceptionMiddleware` を
`CommonHeadersMiddleware` の内側に置くことで、全エラー応答を同じミドルウェアで包む。
"""

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.app.errors import AppError
from src.app.http.argon2_guard import Argon2SaturatedError
from src.app.http.cookies import delete_session_cookie
from src.app.http.logging import EXCEPTION_LOGGER_NAME
from src.app.http.ratelimit import RateLimitExceededError, rate_limit_handler
from src.identity.application.errors import IdentityError, InvalidSessionError, ValidationError
from src.profile.application.errors import ProfileApplicationError, ProfileValidationError
from src.shared.errors.errors import ErrorCode, status_for
from src.shared.web.web import FieldError, build_error_body

# H-18: ルーティング由来の HTTPException を code へ対応付ける。設計が定めるのはこの2つ。
_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
}

# H-17: busy / timeout の `Retry-After`（1以上の整数秒。design/06-errors.md §7）。
BUSY_RETRY_AFTER_SECONDS = 1

# `loc` の先頭に付くリクエスト部位。`field` からは外す（例: `("body","new_password")`
# → `new_password`）。
_LOCATION_TAGS = frozenset({"body", "query", "path", "header", "cookie"})

# 例外ログのロガー。request 本文・ヘッダー・Cookie・ローカル変数は出さない
# （`design/15-logging.md` §17 チャネル `app.error`）。
_EXCEPTION_LOGGER = logging.getLogger(EXCEPTION_LOGGER_NAME)


def _field_name(loc: tuple[int | str, ...]) -> str:
    """pydantic の `loc` を `fields[].field`（入力値を含まない）へ写す。"""
    parts = [str(part) for part in loc]
    if parts and parts[0] in _LOCATION_TAGS:
        parts = parts[1:]
    return ".".join(parts) if parts else "body"


def _field_errors(exc: RequestValidationError) -> list[FieldError]:
    """422 の `fields` を組み立てる。`loc` と `type` だけを使い `input` は写さない。"""
    return [
        FieldError(field=_field_name(error["loc"]), code=error["type"]) for error in exc.errors()
    ]


def _error_response(
    code: ErrorCode,
    *,
    request: Request | None = None,
    fields: Iterable[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """固定文言だけのエラー応答を、code に対応する status で返す。

    `request` を渡すと、応答の `code` を `request.state.error_code` に残す（運用ログの
    「安全な内部エラーコード」に使う。`design/15-logging.md` §17）。
    """
    if request is not None:
        request.state.error_code = code.value
    return JSONResponse(
        build_error_body(code, fields),
        status_code=status_for(code),
        headers=headers,
    )


# Starlette の例外ハンドラは「任意の Exception を受ける callable」を要求する
# （引数は反変）。発生しうる型へ `isinstance` で絞ってから処理する。
async def validation_error_handler(request: Request, exc: Exception) -> Response:
    """H-10: FastAPI 標準 422 を独自形式 422 `VALIDATION_ERROR` に置き換える。"""
    if not isinstance(exc, RequestValidationError):
        raise exc
    return _error_response(ErrorCode.VALIDATION_ERROR, request=request, fields=_field_errors(exc))


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """H-18: 未知ルート 404 `NOT_FOUND` / メソッド不一致 405 `METHOD_NOT_ALLOWED`。

    設計が定めていない status は想定外として 500 `INTERNAL_ERROR` に寄せる
    （内部理由は出さない）。
    """
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    code = _HTTP_ERROR_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return _error_response(code, request=request, headers=exc.headers)


async def invalid_session_handler(request: Request, exc: Exception) -> Response:
    """H-5 / H-6 / H-11: 401 `INVALID_SESSION` を返し `ap_session` を削除する。"""
    if not isinstance(exc, InvalidSessionError):
        raise exc
    response = _error_response(ErrorCode.INVALID_SESSION, request=request)
    delete_session_cookie(response)
    return response


async def application_error_handler(request: Request, exc: Exception) -> Response:
    """アプリケーションエラーを `code` に対応する status の固定文言で返す。

    `ValidationError`（Identity）と `ProfileValidationError`（Profile）は違反項目を
    `fields`（`field` と `code` だけ）として描画する。入力値は写さない
    （design/05-api-contract.md §6）。
    """
    if not isinstance(exc, (AppError, IdentityError, ProfileApplicationError)):
        raise exc
    fields: tuple[FieldError, ...] | None = None
    if isinstance(exc, (ValidationError, ProfileValidationError)):
        fields = exc.fields
    return _error_response(exc.code, request=request, fields=fields)


async def argon2_saturation_handler(request: Request, exc: Exception) -> Response:
    """H-17 相当: Argon2 実行枠が埋まったとき 503 `SERVICE_UNAVAILABLE` ＋ `Retry-After`。

    枠は短時間で空くため `Retry-After` を1秒で返す。待たせずに即座に拒否する
    （design/10-infra-ops.md §11「枠が埋まれば認証処理へ threadpool を投入せず、
    429または503を返す」）。
    """
    if not isinstance(exc, Argon2SaturatedError):
        raise exc
    return _error_response(
        ErrorCode.SERVICE_UNAVAILABLE,
        request=request,
        headers={"Retry-After": str(exc.retry_after)},
    )


async def sqlite_error_handler(request: Request, exc: Exception) -> Response:
    """H-17 / H-9: DB busy は 503 + `Retry-After`、それ以外は 500 `INTERNAL_ERROR`。

    busy 判定はメッセージの語（`locked` / `busy`）だけを見る。例外の内容は応答へ出さない。
    """
    if not isinstance(exc, sqlite3.OperationalError):
        raise exc
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return _error_response(
            ErrorCode.SERVICE_UNAVAILABLE,
            request=request,
            headers={"Retry-After": str(BUSY_RETRY_AFTER_SECONDS)},
        )
    return _error_response(ErrorCode.INTERNAL_ERROR, request=request)


class UnhandledExceptionMiddleware:
    """想定外例外（H-9）を 500 `INTERNAL_ERROR` にし、共通ヘッダーで包める位置に置く。

    `CommonHeadersMiddleware` より内側・`ExceptionMiddleware` より外側で動く。応答開始後に
    発生した例外は再送出し、正常に送信済みの応答を壊さない。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False

        async def track(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, receive, track)
        except Exception as error:
            if response_started:
                raise
            state = scope.setdefault("state", {})
            state["error_code"] = ErrorCode.INTERNAL_ERROR.value
            _EXCEPTION_LOGGER.error(
                json.dumps(
                    {"request_id": state.get("request_id", ""), "exception": type(error).__name__},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            response = _error_response(ErrorCode.INTERNAL_ERROR)
            await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    """`app` にエラーハンドラ一式を登録する（想定外例外はミドルウェア側）。"""
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(InvalidSessionError, invalid_session_handler)
    app.add_exception_handler(RateLimitExceededError, rate_limit_handler)
    app.add_exception_handler(Argon2SaturatedError, argon2_saturation_handler)
    app.add_exception_handler(AppError, application_error_handler)
    app.add_exception_handler(IdentityError, application_error_handler)
    app.add_exception_handler(ProfileApplicationError, application_error_handler)
    app.add_exception_handler(sqlite3.OperationalError, sqlite_error_handler)
