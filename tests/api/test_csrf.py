"""Cookie 発行・削除と CSRF 検証のテスト（design/07-security.md §8 /
design/05-api-contract.md §6 / design/06-errors.md §7）。

T-07-03 の完了条件を観測する。

- `GET` 応答で `__Host-csrf` cookie が発行され、`HttpOnly` が付かない
- `ap_session` が §8 の属性で発行・削除できる
- `Secure` を外す設定・分岐が存在しない（発行・削除ヘルパーが常に `Secure`）
- Origin 欠落/不一致・非 JSON・ヘッダー欠落/不一致がそれぞれ 403 `CSRF_FAILED`（H-12）
- 3条件が揃えば通過し use case へ到達する
"""

from starlette.responses import Response

from src.app.http.cookies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    delete_session_cookie,
    reissue_csrf,
    set_session_cookie,
)
from src.shared.security.security import SecretGenerator
from tests.support.http import TEST_ORIGIN, Harness, csrf_headers, prime_csrf


def test_get_issues_csrf_cookie_without_httponly(harness: Harness) -> None:
    """`GET` 応答で `__Host-csrf` が発行され、`HttpOnly` が付かないこと。"""
    response = harness.client.get("/healthz")

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{CSRF_COOKIE}=")
    lowered = set_cookie.lower()
    assert "secure" in lowered
    assert "path=/" in lowered
    assert "samesite=lax" in lowered
    assert "httponly" not in lowered


def test_get_does_not_reissue_when_cookie_present(harness: Harness) -> None:
    """`csrf` cookie が既にあれば `GET` で再発行しないこと。"""
    harness.client.get("/healthz")

    response = harness.client.get("/healthz")

    assert f"{CSRF_COOKIE}=" not in response.headers.get("set-cookie", "")


def test_unsafe_request_without_origin_is_rejected(harness: Harness) -> None:
    """`Origin` 欠落は 403 `CSRF_FAILED` になること。"""
    token = prime_csrf(harness.client)

    response = harness.client.post("/api/v1/profiles", json={}, headers={"X-CSRF-Token": token})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_unsafe_request_with_wrong_origin_is_rejected(harness: Harness) -> None:
    """`Origin` が `PUBLIC_BASE_URL` と不一致なら 403 になること。"""
    token = prime_csrf(harness.client)

    response = harness.client.post(
        "/api/v1/profiles",
        json={},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
    )

    assert response.status_code == 403


def test_unsafe_request_with_non_json_body_is_rejected(harness: Harness) -> None:
    """本文を持つのに `Content-Type` が JSON でなければ 403 になること。"""
    token = prime_csrf(harness.client)

    response = harness.client.post(
        "/api/v1/profiles",
        content=b"x",
        headers={
            "Origin": TEST_ORIGIN,
            "X-CSRF-Token": token,
            "Content-Type": "text/plain",
        },
    )

    assert response.status_code == 403


def test_unsafe_request_without_header_token_is_rejected(harness: Harness) -> None:
    """`X-CSRF-Token` ヘッダー欠落は 403 になること。"""
    prime_csrf(harness.client)

    response = harness.client.post("/api/v1/profiles", json={}, headers={"Origin": TEST_ORIGIN})

    assert response.status_code == 403


def test_unsafe_request_with_mismatched_token_is_rejected(harness: Harness) -> None:
    """ヘッダーと cookie のトークン不一致は 403 になること。"""
    prime_csrf(harness.client)

    response = harness.client.post(
        "/api/v1/profiles",
        json={},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": "wrong-token"},
    )

    assert response.status_code == 403


def test_valid_csrf_passes_and_reaches_the_use_case(harness: Harness) -> None:
    """3条件が揃えば通過し、`POST /profiles` が use case に到達して 201 になること。"""
    response = harness.client.post(
        "/api/v1/profiles", json={}, headers=csrf_headers(harness.client)
    )

    assert response.status_code == 201


def test_csrf_failure_uses_fixed_message_without_echo(harness: Harness) -> None:
    """403 応答が固定文言で、入力値を echo しないこと。"""
    prime_csrf(harness.client)

    response = harness.client.post(
        "/api/v1/profiles",
        json={},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": "leaky-token"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "CSRF_FAILED", "message": "セキュリティチェックに失敗しました"}
    }
    assert "leaky-token" not in response.text
    assert "evil.example" not in response.text


def test_session_cookie_helpers_use_design_attributes() -> None:
    """`ap_session` の発行・削除が §8 の属性（常に `Secure`）であること。"""
    response = Response()

    set_session_cookie(response, "token-value")
    set_cookie = response.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{SESSION_COOKIE.lower()}=")
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "path=/" in set_cookie
    assert "samesite=lax" in set_cookie
    assert f"max-age={SESSION_MAX_AGE_SECONDS}" in set_cookie

    deleting = Response()
    delete_session_cookie(deleting)
    delete_cookie = deleting.headers["set-cookie"].lower()
    assert delete_cookie.startswith(f"{SESSION_COOKIE.lower()}=")
    assert "secure" in delete_cookie
    assert "path=/" in delete_cookie


def test_reissue_csrf_sets_a_new_cookie() -> None:
    """`reissue_csrf` が新しい CSRF トークンを cookie に設定すること。"""
    response = Response()

    token = reissue_csrf(response, SecretGenerator())

    assert token
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{CSRF_COOKIE}=")
    assert "httponly" not in set_cookie.lower()
