"""本文サイズ上限の結合・単体テスト（design/05-api-contract.md §6 / design/06-errors.md §7）。

T-07-02 の完了条件を観測する。

- 本文 32KB ちょうどは 413 にならない
- 本文 32KB+1 は 413 `PAYLOAD_TOO_LARGE`（固定文言・入力値を echo しない。H-16）
- 巨大 `Content-Length` は本文を読まずに 413（下流が呼ばれない）
- 413 応答にも T-07-01 の共通ヘッダー・`X-Request-ID` が付く
"""

import anyio
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from src.app.http.middleware import (
    MAX_BODY_BYTES,
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
)
from tests.support.http import Harness, csrf_headers

# 指定バイト数の JSON 本文を作るための固定前後（`{"pad":"` と `"}`）。
_PAD_PREFIX = b'{"pad":"'
_PAD_SUFFIX = b'"}'


def _json_body(size: int) -> bytes:
    """ちょうど `size` バイトの `application/json` 本文。"""
    return _PAD_PREFIX + b"y" * (size - len(_PAD_PREFIX) - len(_PAD_SUFFIX)) + _PAD_SUFFIX


def _http_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    """本文上限ミドルウェアを単体で呼ぶための最小 ASGI scope。"""
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/profiles",
        "headers": headers,
        "query_string": b"",
    }


def test_body_exactly_at_limit_is_accepted(harness: Harness) -> None:
    """32KB ちょうどは上限内として通ること（CSRF を通した JSON 本文）。"""
    headers = {"content-type": "application/json", **csrf_headers(harness.client)}

    response = harness.client.post(
        "/api/v1/profiles", content=_json_body(MAX_BODY_BYTES), headers=headers
    )

    assert response.status_code == 201


def test_body_over_limit_is_rejected_with_413(harness: Harness) -> None:
    """32KB+1 は 413 `PAYLOAD_TOO_LARGE`（固定文言）になること。"""
    response = harness.client.post("/api/v1/profiles", content=b"x" * (MAX_BODY_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "PAYLOAD_TOO_LARGE", "message": "リクエスト本文が大きすぎます"}
    }


def test_413_does_not_echo_input(harness: Harness) -> None:
    """413 応答が入力値を echo しないこと（design/14-dod-conventions.md §16 不変規則）。"""
    secret = "sensitive-" + "y" * (MAX_BODY_BYTES + 1)

    response = harness.client.post("/api/v1/profiles", content=secret.encode())

    assert response.status_code == 413
    assert secret not in response.text


def test_413_has_common_headers(harness: Harness) -> None:
    """413 応答にも共通ヘッダーと `X-Request-ID` が付くこと。"""
    response = harness.client.post("/api/v1/profiles", content=b"x" * (MAX_BODY_BYTES + 1))

    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert REQUEST_ID_HEADER in response.headers


def test_huge_content_length_is_rejected_without_reading_body() -> None:
    """`Content-Length` が上限超なら、本文を読まず下流も呼ばずに 413 を返すこと。"""
    read = False

    async def receive() -> Message:
        nonlocal read
        read = True
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("downstream must not be called")

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    scope = _http_scope([(b"content-length", str(MAX_BODY_BYTES + 1).encode())])

    anyio.run(BodySizeLimitMiddleware(downstream).__call__, scope, receive, send)

    assert read is False
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 413


def test_chunked_body_over_limit_is_rejected() -> None:
    """`Content-Length` が無くても、上限を超えた時点で 413 にすること。"""

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 100, "more_body": True}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("downstream must not be called")

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    scope = _http_scope([])
    middleware = BodySizeLimitMiddleware(downstream, max_bytes=150)

    anyio.run(middleware.__call__, scope, receive, send)

    assert messages[0]["status"] == 413


def test_body_within_limit_is_replayed_to_downstream() -> None:
    """上限以内の本文は分割受信でも連結して下流へ1回で渡すこと。"""
    captured: dict[str, bytes] = {}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        captured["body"] = message["body"]
        again = await receive()
        captured["again"] = again["body"]
        await JSONResponse({"ok": True})(scope, receive, send)

    async def send(message: Message) -> None:
        return None

    scope = _http_scope([])
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"hello", "more_body": True}
        return {"type": "http.request", "body": b"world", "more_body": False}

    middleware = BodySizeLimitMiddleware(downstream, max_bytes=100)

    anyio.run(middleware.__call__, scope, receive, send)

    assert captured["body"] == b"helloworld"
    assert captured["again"] == b""
