"""全応答が通る共通ミドルウェア（design/05-api-contract.md §6 /
design/07-security.md §8 / design/10-infra-ops.md §11）。

- `X-Request-ID`: サーバー生成のリクエスト識別子を全応答に付ける。クライアント入力では
  上書きしない。生成は `IdGenerator` ポート経由で行い、`uuid` を直接呼ばない
  （design/14-dod-conventions.md §16 実装規約）。
- セキュリティヘッダー: `X-Content-Type-Options` / `Referrer-Policy` / `X-Robots-Tag` /
  CSP を全応答に付ける。HSTS はリバースプロキシで付与するためここでは付けない。
- `Cache-Control: no-store`: 秘密値・認証系・`/me/*`・認証系HTML・公開プロフィールに付ける。
  例外は `/static/` 配下のアセットのみ（上書きしない）。
- threadpool 上限: `anyio` の既定 40 に任せず 8 に固定する（待機スレッド増殖を防ぐ）。
"""

from collections.abc import Awaitable, Callable

import anyio.to_thread
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.shared.errors.errors import ErrorCode
from src.shared.security.security import IdGenerator
from src.shared.web.web import build_error_body

REQUEST_ID_HEADER = "X-Request-ID"
THREADPOOL_LIMIT = 8

# 本文最大 32KB（design/05-api-contract.md §6 共通規約）。
MAX_BODY_BYTES = 32 * 1024

# design/07-security.md §8「応答ヘッダー（全応答）」の CSP 全文。
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}

# `Cache-Control` を上書きしない（キャッシュ可能な）パス接頭辞。
_CACHEABLE_PREFIXES = ("/static/",)


def cache_control_for(path: str) -> str | None:
    """パスごとの `Cache-Control` を返す。`None` は上書きしないことを表す。

    秘密値・認証系・`/me/*`・認証系HTML・公開プロフィールはすべて `no-store`。静的
    アセットだけは配信側の指定を尊重する（design/07-security.md §8 Cache-Control）。
    """
    if path.startswith(_CACHEABLE_PREFIXES):
        return None
    return "no-store"


def configure_threadpool(limit: int = THREADPOOL_LIMIT) -> None:
    """threadpool の同時実行上限を固定する（design/10-infra-ops.md §11）。

    `anyio` の既定 40 のままにすると、DB・Argon2 の待機でスレッドが増殖する。
    アプリ起動時に 8 へ設定する。
    """
    anyio.to_thread.current_default_thread_limiter().total_tokens = limit


class CommonHeadersMiddleware(BaseHTTPMiddleware):
    """全応答に `X-Request-ID`・セキュリティヘッダー・`no-store` を付ける。"""

    def __init__(self, app: ASGIApp, id_generator: IdGenerator | None = None) -> None:
        super().__init__(app)
        self._id_generator = id_generator if id_generator is not None else IdGenerator()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = self._id_generator.generate()
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        cache_control = cache_control_for(request.url.path)
        if cache_control is not None:
            response.headers["Cache-Control"] = cache_control
        return response


def _content_length(scope: Scope) -> int | None:
    """`Content-Length` ヘッダーを int で返す。無い・不正なら `None`。"""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_payload_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """H-16 413 `PAYLOAD_TOO_LARGE` を固定文言だけで返す（入力値を echo しない）。"""
    response = JSONResponse(
        build_error_body(ErrorCode.PAYLOAD_TOO_LARGE),
        status_code=413,
    )
    await response(scope, receive, send)


async def _empty_receive() -> Message:
    """下流へ渡す空の受信チャネル。"""
    return {"type": "http.request", "body": b"", "more_body": False}


class BodySizeLimitMiddleware:
    """本文 32KB 超を 413 で打ち切る（design/05-api-contract.md §6 / design/06-errors.md §7）。

    - `Content-Length` が上限を超える場合は本文を読まずに 413 を返す（メモリ消費を防ぐ）。
    - `Content-Length` が無い（chunked）場合は上限+1バイトまで読み、超えたら 413 にする。
    - 上限以内なら読んだ本文をそのまま下流へ渡す（下流は通常どおり `Request.body()` を使える）。

    純 ASGI ミドルウェアにしているのは、`BaseHTTPMiddleware` では受信チャネルを横取りして
    「読む前に打ち切る」制御ができないため。
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            await _send_payload_too_large(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self._app(scope, _empty_receive, send)
                return
            body.extend(message.get("body", b""))
            if len(body) > self._max_bytes:
                await _send_payload_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        buffered = bytes(body)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self._app(scope, replay, send)
