"""`PATCH /api/v1/me/profile` の結合テスト
（design/05-api-contract.md §6 / design/06-errors.md §7 / design/04-use-cases.md §5）。

T-09-02 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 1項目以上必須・`content` 未指定＝変更なし・`""`＝空・`null` は 422（H-10）
- 空 content の publish は 422 `PROFILE_NOT_PUBLISHABLE`（H-15）
- `content` 6,000コードポイント超・不正制御文字は 422（AC-10）
- 同状態・同内容は no-op 成功、公開→非公開→再公開で `public_id` 不変・`published_at` 再設定（AC-11）
- 未ログインは 401（他人の更新経路が無い。AC-4）
"""

from collections.abc import Mapping

from fastapi.testclient import TestClient
from httpx2 import Response

from src.main import app
from tests.support.http import TEST_ORIGIN, Harness, csrf_headers, post_json_with_csrf

PATCH_PATH = "/api/v1/me/profile"
VALIDATION_BODY = {"error": {"code": "VALIDATION_ERROR", "message": "入力内容に誤りがあります"}}
NOT_PUBLISHABLE_BODY = {
    "error": {"code": "PROFILE_NOT_PUBLISHABLE", "message": "公開できる内容がありません"}
}


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _patch(harness: Harness, payload: Mapping[str, object]) -> Response:
    return harness.client.patch(PATCH_PATH, json=payload, headers=csrf_headers(harness.client))


def test_patch_content_updates_and_returns_me_profile(harness: Harness) -> None:
    """`content` 指定の PATCH が 200 で `GET /me/profile` と同形4フィールドを返すこと。"""
    created = _create_account(harness)

    response = _patch(harness, {"content": "こんにちは"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"public_id", "status", "content", "published_at"}
    assert body["public_id"] == created["public_id"]
    assert body["status"] == "draft"
    assert body["content"] == "こんにちは"
    assert body["published_at"] is None


def test_patch_empty_content_clears(harness: Harness) -> None:
    """`content: ""` が本文を空にする（`null` とは区別する）こと。"""
    _create_account(harness)
    _patch(harness, {"content": "本文"})

    response = _patch(harness, {"content": ""})

    assert response.status_code == 200
    assert response.json()["content"] == ""


def test_patch_publish_sets_published_at(harness: Harness) -> None:
    """非空 content の publish が 200 で `published_at` を ISO27 で返すこと。"""
    _create_account(harness)
    _patch(harness, {"content": "公開本文"})

    response = _patch(harness, {"status": "published"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["published_at"] is not None
    assert len(body["published_at"]) == 27


def test_patch_publish_empty_content_is_not_publishable(harness: Harness) -> None:
    """空 content の publish が 422 `PROFILE_NOT_PUBLISHABLE` になること（H-15）。"""
    _create_account(harness)

    response = _patch(harness, {"status": "published"})

    assert response.status_code == 422
    assert response.json() == NOT_PUBLISHABLE_BODY


def test_patch_requires_at_least_one_field(harness: Harness) -> None:
    """`content` も `status` も未指定の PATCH が 422 `VALIDATION_ERROR` になること。"""
    _create_account(harness)

    response = _patch(harness, {})

    assert response.status_code == 422
    assert response.json() == VALIDATION_BODY


def test_patch_null_and_invalid_values_are_422(harness: Harness) -> None:
    """`null`・不正 `status`・未知フィールド・型違いが 422 `VALIDATION_ERROR` になること。"""
    _create_account(harness)
    payloads = (
        {"content": None},
        {"status": None},
        {"status": "archived"},
        {"content": 123},
        {"unknown": "x"},
    )

    for payload in payloads:
        response = _patch(harness, payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "VALIDATION_ERROR", payload


def test_patch_content_validation_is_422(harness: Harness) -> None:
    """6,000コードポイント超・制御文字を含む content が 422 になること。"""
    _create_account(harness)

    too_long = _patch(harness, {"content": "あ" * 6001})
    control = _patch(harness, {"content": "a\u0000b"})

    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"
    assert control.status_code == 422
    assert control.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_same_state_is_noop_success(harness: Harness) -> None:
    """同状態・同内容の指定が 200 の no-op 成功で `published_at` を変えないこと。"""
    _create_account(harness)
    _patch(harness, {"content": "本文"})
    first = _patch(harness, {"status": "published"}).json()

    again_status = _patch(harness, {"status": "published"})
    again_content = _patch(harness, {"content": "本文"})

    assert again_status.status_code == 200
    assert again_status.json()["published_at"] == first["published_at"]
    assert again_content.status_code == 200
    assert again_content.json()["content"] == "本文"


def test_republish_keeps_public_id_and_resets_published_at(harness: Harness) -> None:
    """公開→非公開→再公開で `public_id` 不変・`published_at` 再設定になること（AC-11）。"""
    created = _create_account(harness)
    _patch(harness, {"content": "本文"})
    first = _patch(harness, {"status": "published"}).json()

    unpublished = _patch(harness, {"status": "draft"}).json()
    second = _patch(harness, {"status": "published"}).json()

    assert unpublished["published_at"] is None
    assert second["public_id"] == created["public_id"]
    assert second["published_at"] is not None
    assert second["published_at"] != first["published_at"]


def test_patch_unauthenticated_is_401(harness: Harness) -> None:
    """未ログインの PATCH が 401 `INVALID_SESSION` になること（AC-4）。"""
    _create_account(harness)
    client = TestClient(app, base_url=TEST_ORIGIN)

    response = client.patch(PATCH_PATH, json={"content": "x"}, headers=csrf_headers(client))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SESSION"
