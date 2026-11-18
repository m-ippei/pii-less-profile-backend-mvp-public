"""`POST /profiles` の結合テスト（design/05-api-contract.md §6 / design/07-security.md §8）。

M06 の完了条件「`POST /profiles` の結合テストが 201・秘密値を一度だけ返す・`no-store` を
通る」を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 成功: 201 で `{public_id, login_id, initial_password, recovery_key}` の4項目だけを返す
- 返した秘密値は DB にはハッシュ（HMAC-HEX64 / Argon2id）でしか存在しない
- `Cache-Control: no-store` が付く
- Session token が `__Host-ap_session` Cookie（`Path=/; HttpOnly; Secure; SameSite=Lax;
  Max-Age=1209600`）で設定される
- H-8（再試行尽き）は 500 `INTERNAL_ERROR` で、秘密値・内部理由・入力を漏らさない

CSRF・共通セキュリティヘッダー・`X-Request-ID`・rate limit は M07/M10 のスコープ。
CSRF（T-07-03）が有効なため、`POST /profiles` は `GET` で cookie を取得してから
`Origin` と `X-CSRF-Token` を付けて送る（`tests/support/http.py`）。
"""

import os
from pathlib import Path

import pytest
from httpx2 import Response

import src.main as main
from src.app.errors import ProfileCreationFailedError
from src.main import SESSION_COOKIE
from src.shared.db import connect
from src.shared.security.security import SecretHasher
from tests.support.env import PEPPER
from tests.support.http import Harness, csrf_headers, post_json_with_csrf

TABLES = ("users", "login_credentials", "recovery_credentials", "profiles", "sessions")
SECRET_FIELDS = ("login_id", "initial_password", "recovery_key")


def _dump_database(db_path: Path) -> str:
    connection = connect(db_path)
    try:
        lines: list[str] = []
        for table in TABLES:
            for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                cells = ("" if value is None else str(value) for value in tuple(row))
                lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        connection.close()


def _fetch_one(db_path: Path, statement: str) -> str:
    connection = connect(db_path)
    try:
        row = connection.execute(statement).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _post_profiles(harness: Harness) -> Response:
    """CSRF を通して `POST /profiles` を送る。"""
    return post_json_with_csrf(harness.client, "/api/v1/profiles")


def test_post_profiles_returns_201_with_the_four_secret_fields(harness: Harness) -> None:
    """201 で4項目だけを返し、公開識別子と秘密値の形式が設計どおりであること。"""
    response = _post_profiles(harness)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"public_id", "login_id", "initial_password", "recovery_key"}
    assert body["public_id"].startswith("p_") and len(body["public_id"]) == 18
    assert len(body["login_id"]) == 24
    assert len(body["initial_password"]) == 16
    assert len(body["recovery_key"]) == 32


def test_post_profiles_is_no_store(harness: Harness) -> None:
    """秘密値を含む応答に `Cache-Control: no-store` が付くこと。"""
    response = _post_profiles(harness)

    assert response.headers["cache-control"] == "no-store"


def test_secrets_are_returned_but_stored_hashed_only(harness: Harness) -> None:
    """生の秘密値が DB に残らず、ハッシュだけが保存されていること（AC-8）。"""
    response = _post_profiles(harness)
    body = response.json()

    dump = _dump_database(harness.db_path)
    for field in SECRET_FIELDS:
        assert body[field] not in dump

    hasher = SecretHasher(PEPPER)
    assert _fetch_one(harness.db_path, "SELECT login_id_hash FROM login_credentials") == (
        hasher.hash("login_id", body["login_id"])
    )
    assert _fetch_one(harness.db_path, "SELECT recovery_key_hash FROM recovery_credentials") == (
        hasher.hash("recovery_key", body["recovery_key"])
    )
    assert _fetch_one(harness.db_path, "SELECT password_hash FROM login_credentials").startswith(
        "$argon2id$"
    )
    assert _fetch_one(harness.db_path, "SELECT token_hash FROM sessions") == hasher.hash(
        "session", response.cookies[SESSION_COOKIE]
    )


def test_secrets_are_not_leaked_into_response_headers(harness: Harness) -> None:
    """返した秘密値が応答本文以外（ヘッダー）へ漏れないこと。"""
    response = _post_profiles(harness)
    body = response.json()

    headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
    for field in SECRET_FIELDS:
        assert body[field] not in headers


def test_creation_secrets_are_not_re_exposed_by_other_endpoints(harness: Harness) -> None:
    """作成で返した秘密値が、他の API 応答本文に再表示されないこと（AC-1・api 一次検証層）。

    許可3応答（作成・パスワード変更・回復）以外の横断走査は security 層
    （`tests/security/test_secret_responses.py`）が担う。ここでは api 契約として、
    作成後の通常エンドポイント本文に生値が現れないことだけを観測する。
    """
    created = _post_profiles(harness).json()
    secrets = [created[field] for field in SECRET_FIELDS]
    client = harness.client

    responses = [
        client.get("/api/v1/me/profile"),
        client.get(f"/api/v1/profiles/{created['public_id']}"),
        client.patch("/api/v1/me/profile", json={"content": "本文"}, headers=csrf_headers(client)),
    ]
    for response in responses:
        for value in secrets:
            assert value not in response.text


def test_session_cookie_is_hardened(harness: Harness) -> None:
    """Session token が `__Host-ap_session` Cookie として設計の属性で設定されること。"""
    response = _post_profiles(harness)

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=1209600" in lowered


def test_h8_exhausted_returns_500_without_leaking(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再試行が尽きたとき 500 `INTERNAL_ERROR` を返し、内部理由・秘密値を出さないこと。"""

    class FailingCreateProfile:
        def execute(self) -> None:
            raise ProfileCreationFailedError()

    monkeypatch.setattr(main, "build_create_profile", lambda: FailingCreateProfile())

    response = _post_profiles(harness)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "内部エラーが発生しました"}
    }
    assert response.headers["cache-control"] == "no-store"
    assert "ProfileCreationFailed" not in response.text
    assert str(harness.db_path) not in response.text
    assert os.environ["APP_PEPPER"] not in response.text
