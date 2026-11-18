"""`GET /api/v1/profiles/{public_id}` の結合テスト
（design/05-api-contract.md §6 / design/06-errors.md §7 / design/11-testing.md §12）。

T-09-01 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- published は 200 で `{public_id, content}` の2キーだけを返す（AC-3）
- draft・存在しない・形式違反はすべて同一本文の 404 `PROFILE_NOT_FOUND`（H-1 / H-2、AC-2）
- 404 応答に入力値が echo されない
- `Cache-Control: no-store` が付く
"""

from pathlib import Path

from fastapi.testclient import TestClient

from src.main import app
from src.shared.db import connect
from tests.support.http import TEST_ORIGIN, Harness, post_json_with_csrf

PUBLISHED_AT = "2026-10-07T00:00:00.000000Z"
NOT_FOUND_BODY = {"error": {"code": "PROFILE_NOT_FOUND", "message": "プロフィールが見つかりません"}}


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _publish_in_db(db_path: Path, content: str) -> None:
    """API（PATCH は T-09-02）に依存せず、DB を公開状態へ更新して公開取得を観測する。"""
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


def test_published_profile_returns_public_dto(harness: Harness) -> None:
    """published の公開取得が 200 で `{public_id, content}` だけを返すこと（AC-3）。"""
    created = _create_account(harness)
    _publish_in_db(harness.db_path, "こんにちは")

    response = harness.client.get(f"/api/v1/profiles/{created['public_id']}")

    assert response.status_code == 200
    assert response.json() == {"public_id": created["public_id"], "content": "こんにちは"}
    assert set(response.json()) == {"public_id", "content"}


def test_public_profile_needs_no_auth(harness: Harness) -> None:
    """Session cookie を持たない第三者でも公開プロフィールを取得できること。"""
    created = _create_account(harness)
    _publish_in_db(harness.db_path, "公開本文")
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")

    response = client.get(f"/api/v1/profiles/{created['public_id']}")

    assert response.status_code == 200
    assert response.json()["content"] == "公開本文"


def test_draft_missing_and_malformed_are_indistinguishable(harness: Harness) -> None:
    """draft・存在しない・形式違反が同一本文の 404 で区別できないこと（H-1 / H-2、AC-2）。"""
    created = _create_account(harness)
    draft = harness.client.get(f"/api/v1/profiles/{created['public_id']}")
    missing = harness.client.get("/api/v1/profiles/p_1111111111111111")
    malformed = harness.client.get("/api/v1/profiles/not-a-public-id")

    assert draft.status_code == 404
    assert missing.status_code == 404
    assert malformed.status_code == 404
    assert draft.json() == NOT_FOUND_BODY
    assert missing.json() == NOT_FOUND_BODY
    assert malformed.json() == NOT_FOUND_BODY


def test_not_found_does_not_echo_input(harness: Harness) -> None:
    """404 応答に入力値（違反した `public_id`）が含まれないこと（AC-2）。"""
    _create_account(harness)
    malformed = "p_0000000000000000"

    response = harness.client.get(f"/api/v1/profiles/{malformed}")

    assert response.status_code == 404
    assert malformed not in response.text
    assert "not-a-public-id" not in response.text


def test_public_profile_is_no_store(harness: Harness) -> None:
    """公開プロフィールの応答に `Cache-Control: no-store` が付くこと（design/07 §8）。"""
    created = _create_account(harness)
    _publish_in_db(harness.db_path, "公開本文")

    response = harness.client.get(f"/api/v1/profiles/{created['public_id']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
