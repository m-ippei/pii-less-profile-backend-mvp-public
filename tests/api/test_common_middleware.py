"""共通レスポンスミドルウェアの結合テスト（design/05-api-contract.md §6 /
design/07-security.md §8 / design/10-infra-ops.md §11）。

T-07-01 の完了条件を、結線済み `main.app` と `cache_control_for` の単体で観測する。

- 全応答に `X-Request-ID`（サーバー生成。クライアント指定に依存しない）
- 全応答に `X-Content-Type-Options` / `Referrer-Policy` / `X-Robots-Tag` / CSP。HSTS なし
- `Cache-Control: no-store` が秘密値・認証系・`/me/*`・認証系HTML・公開プロフィールに付く
- threadpool 上限が8に固定される
"""

import uuid

import anyio
import anyio.to_thread
from fastapi.testclient import TestClient

from src.app.http.middleware import (
    CONTENT_SECURITY_POLICY,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    THREADPOOL_LIMIT,
    cache_control_for,
    configure_threadpool,
)
from src.main import app
from tests.support.http import Harness, post_json_with_csrf


def test_all_responses_have_a_server_generated_request_id(harness: Harness) -> None:
    """`X-Request-ID` が全応答に付き、UUID として解釈できるサーバー生成値であること。"""
    response = harness.client.get("/healthz")

    assert response.status_code == 200
    request_id = response.headers[REQUEST_ID_HEADER]
    assert str(uuid.UUID(request_id)) == request_id


def test_client_supplied_request_id_is_not_trusted(harness: Harness) -> None:
    """クライアントが `X-Request-ID` を送っても、応答はサーバー生成値に置き換えること。"""
    forged = "client-controlled-request-id"

    response = harness.client.get("/healthz", headers={REQUEST_ID_HEADER: forged})

    assert response.headers[REQUEST_ID_HEADER] != forged


def test_security_headers_are_present_on_success(harness: Harness) -> None:
    """成功応答に §8 のセキュリティヘッダーが揃うこと。"""
    response = harness.client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_security_headers_are_present_on_unknown_route(harness: Harness) -> None:
    """エラー応答にもセキュリティヘッダーと `X-Request-ID` が付くこと。"""
    response = harness.client.get("/no-such-route")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert REQUEST_ID_HEADER in response.headers


def test_hsts_is_not_added(harness: Harness) -> None:
    """HSTS はリバースプロキシで付与するため、アプリは付けないこと。"""
    response = harness.client.get("/healthz")

    assert "strict-transport-security" not in response.headers


def test_no_store_is_applied_to_healthz_and_secret_response(harness: Harness) -> None:
    """`/healthz` と秘密値を返す `POST /profiles` に `no-store` が付くこと。"""
    health = harness.client.get("/healthz")
    created = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert health.headers["cache-control"] == "no-store"
    assert created.headers["cache-control"] == "no-store"


def test_cache_control_for_covers_the_required_categories() -> None:
    """秘密値・認証系・`/me/*`・認証系HTML・公開プロフィールが `no-store` になること。"""
    for path in (
        "/api/v1/profiles",
        "/sessions",
        "/me/profile",
        "/me/password",
        "/recovery/password-resets",
        "/new",
        "/login",
        "/profile-editor",
        "/account",
        "/recovery",
        "/credentials-saved",
        "/api/v1/profiles/p_1234567890abcdef",
        "/healthz",
    ):
        assert cache_control_for(path) == "no-store", path


def test_static_assets_are_not_forced_no_store() -> None:
    """静的アセットの `Cache-Control` は上書きしない（配信側の指定を尊重）。"""
    assert cache_control_for("/static/app.js") is None


def test_threadpool_limit_is_fixed_to_eight() -> None:
    """`configure_threadpool()` が threadpool 上限を8へ固定できること。"""

    async def check() -> None:
        configure_threadpool(5)
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == 5
        configure_threadpool()
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == THREADPOOL_LIMIT

    anyio.run(check)


def test_lifespan_configures_threadpool() -> None:
    """起動（lifespan）で threadpool 上限が8に設定されること。"""

    async def read_limit() -> float:
        limiter = anyio.to_thread.current_default_thread_limiter()
        return limiter.total_tokens

    with TestClient(app, base_url="https://testserver") as client:
        client.get("/healthz")  # lifespan を起動させる
        portal = client.portal
        assert portal is not None
        limit = portal.call(read_limit)

    assert limit == THREADPOOL_LIMIT


def test_security_headers_constant_matches_design() -> None:
    """ヘッダー定数が §8 の4種だけであること（HSTS を含まない）。"""
    assert SECURITY_HEADERS == {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    }
