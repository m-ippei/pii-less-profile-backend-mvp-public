"""ログイン・ログアウト API の結合テスト（design/05-api-contract.md §6 /
design/07-security.md §8 / design/06-errors.md §7）。

T-08-03 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 正しい `login_id`＋password で 204 ＋ `__Host-ap_session`（§8 の属性）
- `login_id` 不明と password 誤りが同一の 401 `INVALID_CREDENTIALS`（AC-5）
- ログイン成功で `__Host-csrf` を再発行し、提示された有効な旧Sessionを失効させる
- ログアウトは 204 で Cookie 削除、未ログイン・無効 Session でも 204（冪等）
- `login_id` の接尾の表示識別文字は抽出されて認証に使われる
- 形式違反は 422 `VALIDATION_ERROR`（入力値を echo しない）
"""

from pathlib import Path

from httpx2 import Response

from src.app.http.cookies import CSRF_COOKIE, SESSION_COOKIE
from src.app.http.csrf import CSRF_HEADER
from src.shared.db import connect
from src.shared.security.security import SecretHasher
from tests.support.env import PEPPER
from tests.support.http import TEST_ORIGIN, Harness, csrf_headers, post_json_with_csrf

SESSION_PATH = "/api/v1/sessions"
LOGOUT_PATH = "/api/v1/sessions/current"
WRONG_CREDENTIALS = {
    "error": {
        "code": "INVALID_CREDENTIALS",
        "message": "ログインIDまたはパスワードが正しくありません",
    }
}


def _create_account(harness: Harness) -> dict[str, str]:
    """`POST /profiles` でアカウントを作り、返された秘密値を返す。"""
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")
    assert response.status_code == 201
    return response.json()


def _login(harness: Harness, login_id: str, password: str) -> Response:
    """CSRF を通してログインする。"""
    return harness.client.post(
        SESSION_PATH,
        json={"login_id": login_id, "password": password},
        headers=csrf_headers(harness.client),
    )


def _logout(harness: Harness) -> Response:
    return harness.client.delete(LOGOUT_PATH, headers=csrf_headers(harness.client))


def _set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _revoked_at(db_path: Path, token: str) -> str | None:
    """Session token の `revoked_at`（未失効は `None`）。token は HMAC 化して照合する。"""
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


def test_valid_login_returns_204_and_rotates_the_session(harness: Harness) -> None:
    """正しい資格情報で 204、§8 の属性の新Session Cookie、`csrf` 再発行、旧Session失効。"""
    account = _create_account(harness)
    old_token = harness.client.cookies[SESSION_COOKIE]

    response = _login(harness, account["login_id"], account["initial_password"])

    assert response.status_code == 204
    cookies = _set_cookies(response)
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    lowered = session_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "path=/" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=1209600" in lowered
    assert any(c.startswith(f"{CSRF_COOKIE}=") for c in cookies)

    new_token = harness.client.cookies[SESSION_COOKIE]
    assert new_token != old_token
    assert _revoked_at(harness.db_path, old_token) is not None
    assert _revoked_at(harness.db_path, new_token) is None


def test_login_accepts_suffix_on_login_id(harness: Harness) -> None:
    """`login_id` に表示識別文字（`_` 以降）が付いていても認証できること。"""
    account = _create_account(harness)

    response = _login(harness, f"{account['login_id']}_MyLabel", account["initial_password"])

    assert response.status_code == 204


def test_unknown_login_id_and_wrong_password_share_the_same_401(harness: Harness) -> None:
    """`login_id` 不明と password 誤りが同一の 401 `INVALID_CREDENTIALS` になること。"""
    account = _create_account(harness)

    unknown = _login(harness, "z" * 24, account["initial_password"])
    wrong = _login(harness, account["login_id"], "wrong-password-123")

    assert unknown.status_code == 401
    assert wrong.status_code == 401
    assert unknown.json() == WRONG_CREDENTIALS
    assert wrong.json() == WRONG_CREDENTIALS


def test_login_invalid_input_is_422_without_echoing_values(harness: Harness) -> None:
    """形式違反が 422 `VALIDATION_ERROR` の `fields` になり、入力値を echo しないこと。"""
    secret = "sUpErSeCrEtLoginId"

    response = harness.client.post(
        SESSION_PATH,
        json={"login_id": secret, "password": ""},
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "入力内容に誤りがあります"
    assert body["error"]["fields"] == [
        {"field": "login_id", "code": "too_short"},
        {"field": "password", "code": "too_short"},
    ]
    assert secret not in response.text


def test_login_rejects_unknown_fields(harness: Harness) -> None:
    """未知フィールドが 422 になること（design/05-api-contract.md §6 共通規約）。"""
    account = _create_account(harness)

    response = harness.client.post(
        SESSION_PATH,
        json={
            "login_id": account["login_id"],
            "password": account["initial_password"],
            "extra": "x",
        },
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 422


def test_login_requires_csrf(harness: Harness) -> None:
    """CSRF ヘッダー無しのログインが 403 `CSRF_FAILED` になること。"""
    account = _create_account(harness)

    response = harness.client.post(
        SESSION_PATH,
        json={"login_id": account["login_id"], "password": account["initial_password"]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_login_is_no_store_and_does_not_echo_secrets(harness: Harness) -> None:
    """ログイン応答が `no-store` で、秘密値を返さないこと。"""
    account = _create_account(harness)

    response = _login(harness, account["login_id"], account["initial_password"])

    assert response.headers["cache-control"] == "no-store"
    for field in ("login_id", "initial_password", "recovery_key"):
        assert account[field] not in response.text


def test_logout_returns_204_deletes_session_and_reissues_csrf(harness: Harness) -> None:
    """ログアウトが 204 で `ap_session` を削除し `csrf` を再発行し、Session を失効させること。"""
    _create_account(harness)
    token = harness.client.cookies[SESSION_COOKIE]

    response = _logout(harness)

    assert response.status_code == 204
    cookies = _set_cookies(response)
    deletion = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    assert "max-age=0" in deletion.lower()
    assert any(c.startswith(f"{CSRF_COOKIE}=") for c in cookies)
    assert _revoked_at(harness.db_path, token) is not None


def test_logout_is_idempotent(harness: Harness) -> None:
    """ログアウトが冪等で、未ログイン・失効済み Session でも 204 になること。"""
    _create_account(harness)

    first = _logout(harness)
    second = _logout(harness)

    assert first.status_code == 204
    assert second.status_code == 204


def test_logout_with_unknown_session_is_204(harness: Harness) -> None:
    """無効な Session token でも 204 を返すこと（401 にしない）。"""
    csrf_token = csrf_headers(harness.client)[CSRF_HEADER]
    harness.client.cookies.set(SESSION_COOKIE, "unknown-session-token", domain="testserver")

    response = harness.client.delete(
        LOGOUT_PATH,
        headers={"Origin": TEST_ORIGIN, CSRF_HEADER: csrf_token},
    )

    assert response.status_code == 204
