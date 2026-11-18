"""回復 API の結合テスト（design/05-api-contract.md §6 / design/07-security.md §8 /
design/06-errors.md §7）。

T-08-05 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 成功 200 `{login_id, recovery_key}`（Base58 24/32文字）＋`no-store`＋`ap_session` 削除
  （Session は発行しない）＋`csrf` 再発行
- 旧 `login_id`・旧パスワード・旧 `recovery_key` が以後使えず、全 Session が 401
- 不正 `recovery_key` は 400 `INVALID_RECOVERY_CREDENTIAL`（入力値を echo しない）
- 形式違反は 422 `VALIDATION_ERROR`、CSRF 無しは 403 `CSRF_FAILED`
"""

from pathlib import Path

from fastapi.testclient import TestClient
from httpx2 import Response

from src.app.http.cookies import CSRF_COOKIE, SESSION_COOKIE
from src.main import app
from src.shared.db import connect
from src.shared.security.security import SecretHasher
from tests.support.env import PEPPER
from tests.support.http import TEST_ORIGIN, Harness, csrf_headers, post_json_with_csrf

RECOVERY_PATH = "/api/v1/recovery/password-resets"
SESSION_PATH = "/api/v1/sessions"
PASSWORD_PATH = "/api/v1/me/password"
NEW_PASSWORD = "recovered-pass-123"


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _recover(harness: Harness, recovery_key: str, new_password: str) -> Response:
    return harness.client.post(
        RECOVERY_PATH,
        json={"recovery_key": recovery_key, "new_password": new_password},
        headers=csrf_headers(harness.client),
    )


def _login(harness: Harness, login_id: str, password: str) -> Response:
    return harness.client.post(
        SESSION_PATH,
        json={"login_id": login_id, "password": password},
        headers=csrf_headers(harness.client),
    )


def _client_with_csrf() -> TestClient:
    """`csrf` cookie だけを持つ独立クライアント（Session はリクエスト単位で渡す）。"""
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")
    return client


def _probe_session(session_token: str) -> Response:
    """`POST /me/password` を認証付きで叩き、Session の有効性だけを観測する。"""
    client = _client_with_csrf()
    csrf_token = client.cookies[CSRF_COOKIE]
    return client.post(
        PASSWORD_PATH,
        json={"current_password": NEW_PASSWORD, "new_password": "another-pass-1234"},
        headers={
            "Origin": TEST_ORIGIN,
            "X-CSRF-Token": csrf_token,
            "Cookie": f"{SESSION_COOKIE}={session_token}; {CSRF_COOKIE}={csrf_token}",
        },
    )


def _set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _revoked_at(db_path: Path, token: str) -> object:
    token_hash = SecretHasher(PEPPER).hash("session", token)
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT revoked_at FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def test_recovery_returns_200_rotates_credentials_and_clears_session_cookie(
    harness: Harness,
) -> None:
    """成功 200 `{login_id, recovery_key}`・`no-store`・`ap_session` 削除・`csrf` 再発行。"""
    account = _create_account(harness)
    old_session = harness.client.cookies[SESSION_COOKIE]

    response = _recover(harness, account["recovery_key"], NEW_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"login_id", "recovery_key"}
    assert len(body["login_id"]) == 24
    assert len(body["recovery_key"]) == 32
    assert body["login_id"] != account["login_id"]
    assert body["recovery_key"] != account["recovery_key"]
    assert response.headers["cache-control"] == "no-store"

    cookies = _set_cookies(response)
    session_cookies = [c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")]
    assert session_cookies
    for cookie in session_cookies:
        deletion = cookie.lower()
        assert "max-age=0" in deletion
        assert "secure" in deletion
        assert "path=/" in deletion
        value = cookie.split(";", 1)[0].split("=", 1)[1]
        assert value.strip('"') == ""
    assert any(c.startswith(f"{CSRF_COOKIE}=") for c in cookies)

    assert _revoked_at(harness.db_path, old_session) is not None


def test_recovery_invalidates_old_credentials_and_all_sessions(harness: Harness) -> None:
    """旧 `login_id`・旧パスワード・旧 `recovery_key` が使えず、全 Session が 401 になること。"""
    account = _create_account(harness)
    session_a = harness.client.cookies[SESSION_COOKIE]

    other = _client_with_csrf()
    login_b = other.post(
        SESSION_PATH,
        json={"login_id": account["login_id"], "password": account["initial_password"]},
        headers=csrf_headers(other),
    )
    assert login_b.status_code == 204
    session_b = other.cookies[SESSION_COOKIE]

    recovered = _recover(harness, account["recovery_key"], NEW_PASSWORD)
    assert recovered.status_code == 200
    new = recovered.json()

    assert _revoked_at(harness.db_path, session_a) is not None
    assert _revoked_at(harness.db_path, session_b) is not None
    for token in (session_a, session_b):
        probe = _probe_session(token)
        assert probe.status_code == 401
        assert probe.json()["error"]["code"] == "INVALID_SESSION"

    old_login = _login(harness, account["login_id"], account["initial_password"])
    assert old_login.status_code == 401
    assert old_login.json()["error"]["code"] == "INVALID_CREDENTIALS"

    assert _login(harness, new["login_id"], NEW_PASSWORD).status_code == 204

    reused = _recover(harness, account["recovery_key"], "another-pass-1234")
    assert reused.status_code == 400
    assert reused.json()["error"]["code"] == "INVALID_RECOVERY_CREDENTIAL"


def test_invalid_recovery_key_is_400_without_echoing_values(harness: Harness) -> None:
    """未知の `recovery_key` が 400 `INVALID_RECOVERY_CREDENTIAL` になり、値を echo しないこと。"""
    _create_account(harness)
    unknown = "z" * 32

    response = _recover(harness, unknown, NEW_PASSWORD)

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "INVALID_RECOVERY_CREDENTIAL",
            "message": "回復用キーが正しくありません",
        }
    }
    assert unknown not in response.text


def test_recovery_format_violation_is_422_without_echoing_values(harness: Harness) -> None:
    """形式違反が 422 の `fields` になり、入力値を echo しないこと（H-10）。"""
    _create_account(harness)
    secret = "sUpErSeCrEtR"

    response = _recover(harness, secret, "short")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"] == [
        {"field": "recovery_key", "code": "too_short"},
        {"field": "new_password", "code": "too_short"},
    ]
    assert secret not in response.text


def test_recovery_requires_csrf(harness: Harness) -> None:
    """CSRF ヘッダー無しの回復が 403 `CSRF_FAILED` になること。"""
    account = _create_account(harness)

    response = harness.client.post(
        RECOVERY_PATH,
        json={"recovery_key": account["recovery_key"], "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
