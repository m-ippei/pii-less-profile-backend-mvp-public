"""パスワード変更 API の結合テスト（design/05-api-contract.md §6 /
design/07-security.md §8 / design/06-errors.md §7）。

T-08-04 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 成功 200 `{recovery_key}`（Base58 32文字）＋新 `__Host-ap_session`＋`csrf` 再発行＋`no-store`
- 全Session失効（旧 Session token は以後 401）・新Sessionは有効・`recovery_key` ローテーション
- 現パスワード誤りは 403 `INVALID_CURRENT_PASSWORD`（H-14）
- 未ログイン・無効 Session は 401 `INVALID_SESSION`＋Cookie削除
- 形式違反・現パスワード同一は 422 `VALIDATION_ERROR`
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

PASSWORD_PATH = "/api/v1/me/password"
NEW_PASSWORD = "new-password-123"


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _change(harness: Harness, current_password: str, new_password: str) -> Response:
    return harness.client.post(
        PASSWORD_PATH,
        json={"current_password": current_password, "new_password": new_password},
        headers=csrf_headers(harness.client),
    )


def _client_with_csrf() -> TestClient:
    """`csrf` cookie だけを持つ独立クライアント（Session はリクエスト単位で渡す）。"""
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")
    return client


def _change_with(
    client: TestClient, session_token: str, current_password: str, new: str
) -> Response:
    csrf_token = client.cookies[CSRF_COOKIE]
    return client.post(
        PASSWORD_PATH,
        json={"current_password": current_password, "new_password": new},
        headers={
            "Origin": TEST_ORIGIN,
            "X-CSRF-Token": csrf_token,
            "Cookie": f"{SESSION_COOKIE}={session_token}; {CSRF_COOKIE}={csrf_token}",
        },
    )


def _set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _scalar(db_path: Path, statement: str, params: tuple[str, ...] = ()) -> object:
    connection = connect(db_path)
    try:
        row = connection.execute(statement, params).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def _revoked_at(db_path: Path, token: str) -> str | None:
    token_hash = SecretHasher(PEPPER).hash("session", token)
    value = _scalar(db_path, "SELECT revoked_at FROM sessions WHERE token_hash = ?", (token_hash,))
    return None if value is None else str(value)


def _recovery_key_hash(db_path: Path) -> str:
    return str(_scalar(db_path, "SELECT recovery_key_hash FROM recovery_credentials"))


def test_change_password_returns_200_and_rotates_session_and_recovery_key(
    harness: Harness,
) -> None:
    """成功 200 `{recovery_key}`・新Session・`csrf` 再発行・旧Session失効・キーローテーション。"""
    account = _create_account(harness)
    old_session = harness.client.cookies[SESSION_COOKIE]
    old_recovery_hash = _recovery_key_hash(harness.db_path)

    response = _change(harness, account["initial_password"], NEW_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"recovery_key"}
    assert len(body["recovery_key"]) == 32

    cookies = _set_cookies(response)
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    lowered = session_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=1209600" in lowered
    assert any(c.startswith(f"{CSRF_COOKIE}=") for c in cookies)

    new_session = harness.client.cookies[SESSION_COOKIE]
    assert new_session != old_session
    assert _revoked_at(harness.db_path, old_session) is not None
    assert _revoked_at(harness.db_path, new_session) is None
    assert _recovery_key_hash(harness.db_path) != old_recovery_hash
    assert response.headers["cache-control"] == "no-store"


def test_old_session_is_rejected_and_new_session_is_valid(harness: Harness) -> None:
    """変更後、旧 Session token は 401、新 Session token は有効であること。"""
    account = _create_account(harness)
    old_session = harness.client.cookies[SESSION_COOKIE]

    first = _change(harness, account["initial_password"], NEW_PASSWORD)
    assert first.status_code == 200
    new_session = harness.client.cookies[SESSION_COOKIE]

    rejected = _change_with(_client_with_csrf(), old_session, NEW_PASSWORD, "another-pass-1234")
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "INVALID_SESSION"

    accepted = _change_with(_client_with_csrf(), new_session, NEW_PASSWORD, "another-pass-1234")
    assert accepted.status_code == 200


def test_wrong_current_password_is_403(harness: Harness) -> None:
    """現パスワード誤りは 403 `INVALID_CURRENT_PASSWORD` で、Session は失効しないこと。"""
    _create_account(harness)
    session = harness.client.cookies[SESSION_COOKIE]

    response = _change(harness, "wrong-current-password", NEW_PASSWORD)

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "INVALID_CURRENT_PASSWORD",
            "message": "現在のパスワードが正しくありません",
        }
    }
    assert _revoked_at(harness.db_path, session) is None


def test_new_password_same_as_current_is_422(harness: Harness) -> None:
    """新パスワードが現パスワードと同一なら 422 `VALIDATION_ERROR` になること（H-10）。"""
    account = _create_account(harness)

    response = _change(harness, account["initial_password"], account["initial_password"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_input_is_422_without_echoing_values(harness: Harness) -> None:
    """形式違反が 422 の `fields` になり、入力値を echo しないこと。"""
    _create_account(harness)
    secret = "sUpErSeCrEtCurrent"

    response = harness.client.post(
        PASSWORD_PATH,
        json={"current_password": secret, "new_password": "short"},
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"] == [{"field": "new_password", "code": "too_short"}]
    assert secret not in response.text


def test_unauthenticated_is_401_and_deletes_session_cookie(harness: Harness) -> None:
    """未ログインは 401 `INVALID_SESSION` で、`ap_session` を削除すること（H-11）。"""
    client = TestClient(app, base_url=TEST_ORIGIN)
    client.get("/healthz")

    response = client.post(
        PASSWORD_PATH,
        json={"current_password": "x" * 12, "new_password": NEW_PASSWORD},
        headers={"Origin": TEST_ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]},
    )

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


def test_change_password_requires_csrf(harness: Harness) -> None:
    """CSRF ヘッダー無しの変更が 403 `CSRF_FAILED` になること。"""
    account = _create_account(harness)

    response = harness.client.post(
        PASSWORD_PATH,
        json={"current_password": account["initial_password"], "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
