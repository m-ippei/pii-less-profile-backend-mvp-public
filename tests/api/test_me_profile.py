"""`GET /api/v1/me/profile` の結合テスト（design/05-api-contract.md §6 /
design/04-use-cases.md §5 / design/07-security.md §8）。

T-08-06 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- ログイン済みで 200 の4フィールド（`published_at` は 27文字 ISO 8601 または null）
- 未ログイン・無効 Session は 401 `INVALID_SESSION`＋`ap_session` 削除
- `Cache-Control: no-store` が付く
"""

from pathlib import Path

from fastapi.testclient import TestClient
from httpx2 import Response

from src.app.http.cookies import SESSION_COOKIE
from src.main import app
from src.shared.db import connect
from tests.support.http import TEST_ORIGIN, Harness, post_json_with_csrf

ME_PROFILE_PATH = "/api/v1/me/profile"
PUBLISHED_AT = "2026-10-07T00:00:00.000000Z"


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _get(harness: Harness) -> Response:
    return harness.client.get(ME_PROFILE_PATH)


def _publish_in_db(db_path: Path, content: str) -> None:
    """API（PATCH は M09）に依存せず、DB を公開状態へ更新して `published_at` 経路を観測する。"""
    connection = connect(db_path)
    try:
        connection.execute(
            "UPDATE profiles SET status = 'published', content = ?, "
            "published_at = ?, updated_at = ?",
            (content, PUBLISHED_AT, PUBLISHED_AT),
        )
        connection.commit()
    finally:
        connection.close()


def test_me_profile_returns_draft_four_fields(harness: Harness) -> None:
    """draft の Profile が 200 で `{public_id, status, content, published_at}` を返すこと。"""
    created = _create_account(harness)

    response = _get(harness)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"public_id", "status", "content", "published_at"}
    assert body["public_id"] == created["public_id"]
    assert body["status"] == "draft"
    assert body["content"] == ""
    assert body["published_at"] is None


def test_published_profile_returns_iso27_published_at(harness: Harness) -> None:
    """公開済み Profile の `published_at` が 27文字 ISO 8601 で返ること。"""
    _create_account(harness)
    _publish_in_db(harness.db_path, "こんにちは")

    response = _get(harness)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["content"] == "こんにちは"
    assert body["published_at"] == PUBLISHED_AT
    assert len(body["published_at"]) == 27


def test_me_profile_is_no_store(harness: Harness) -> None:
    """認証付き `/me/*` 応答に `Cache-Control: no-store` が付くこと。"""
    _create_account(harness)

    response = _get(harness)

    assert response.headers["cache-control"] == "no-store"


def test_unauthenticated_is_401_and_deletes_session_cookie(harness: Harness) -> None:
    """未ログインは 401 `INVALID_SESSION` で `ap_session` を削除すること（H-11）。"""
    _create_account(harness)
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")

    response = client.get(ME_PROFILE_PATH)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "INVALID_SESSION", "message": "セッションが無効です"}
    }
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    lowered = set_cookie.lower()
    assert "max-age=0" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered
    assert response.headers["cache-control"] == "no-store"


def test_invalid_session_is_401_and_deletes_session_cookie(harness: Harness) -> None:
    """不明な Session token は 401 `INVALID_SESSION` で `ap_session` を削除すること（H-11）。"""
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")
    client.cookies.set(SESSION_COOKIE, "unknown-session-token", domain="testserver")

    response = client.get(ME_PROFILE_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SESSION"
    assert "max-age=0" in response.headers["set-cookie"].lower()
