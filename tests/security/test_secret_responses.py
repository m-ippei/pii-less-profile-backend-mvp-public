"""AC-1: 秘密値は一度だけ返り、以後どの API でも生値が再表示されない（design/05-api-contract.md §6
/ design/07-security.md §8 / design/11-testing.md §12 security 層）。

作成応答で得た `login_id` / `initial_password` / `recovery_key` を保持し、**秘密値を返して
よい3応答（作成・パスワード変更・回復）以外**の全 API 応答（本文・ヘッダー・`Set-Cookie`）に
生値が含まれないことを横断的に確認する。あわせて3応答が「期待する秘密値だけ」を返すこと
（過不足の検出）と、ローテーション後に旧値・新値が漏れないことを確認する。

IP アドレスの生値が DB に無いこと（AC-8）は `tests/security/test_no_raw_ip.py` で確認する。
ログの許可リスト検査は `tests/security/test_security_event_log.py` で確認する。
"""

import logging

import pytest
from httpx2 import Response

from tests.support.http import Harness, csrf_headers, post_json_with_csrf

SECRET_FIELDS = ("login_id", "initial_password", "recovery_key")


def _post_profiles(harness: Harness) -> Response:
    return post_json_with_csrf(harness.client, "/api/v1/profiles")


def _secrets(body: dict[str, str]) -> dict[str, str]:
    return {field: body[field] for field in SECRET_FIELDS}


def _response_text(response: Response) -> str:
    """応答本文・ヘッダー・`Set-Cookie` を連結した走査対象テキスト。"""
    parts = [response.text]
    parts.extend(response.headers.get_list("set-cookie"))
    parts.extend(f"{key}: {value}" for key, value in response.headers.items())
    return "\n".join(parts)


def _leak_names(text: str, secrets: dict[str, str]) -> list[str]:
    """`text` に生値が含まれる秘密値の名前を返す（走査ロジックの本体）。"""
    return [name for name, value in secrets.items() if value and value in text]


def _battery(harness: Harness, public_id: str) -> list[tuple[str, Response]]:
    """秘密値を返してよい3応答**以外**の API 応答を集める。"""
    client = harness.client
    return [
        ("GET /api/v1/me/profile", client.get("/api/v1/me/profile")),
        ("GET /api/v1/profiles/{public_id}", client.get(f"/api/v1/profiles/{public_id}")),
        (
            "PATCH /api/v1/me/profile",
            client.patch(
                "/api/v1/me/profile", json={"content": "本文"}, headers=csrf_headers(client)
            ),
        ),
        (
            "POST /api/v1/me/password (422)",
            client.post(
                "/api/v1/me/password",
                json={"current_password": "x", "new_password": "short"},
                headers=csrf_headers(client),
            ),
        ),
        (
            "POST /api/v1/recovery/password-resets (400)",
            client.post(
                "/api/v1/recovery/password-resets",
                json={"recovery_key": "A" * 32, "new_password": "validpassword123"},
                headers=csrf_headers(client),
            ),
        ),
        ("GET /missing", client.get("/missing")),
        ("GET /healthz", client.get("/healthz")),
    ]


def _battery_leaks(
    harness: Harness, public_id: str, secrets: dict[str, str]
) -> dict[str, list[str]]:
    leaks: dict[str, list[str]] = {}
    for label, response in _battery(harness, public_id):
        names = _leak_names(_response_text(response), secrets)
        if names:
            leaks[label] = names
    return leaks


def test_creation_returns_exactly_the_three_secrets(harness: Harness) -> None:
    """作成応答は4フィールドで、秘密値3項目が非空であること。"""
    body = _post_profiles(harness).json()
    assert set(body) == {"public_id", "login_id", "initial_password", "recovery_key"}
    assert all(_secrets(body).values())


def test_creation_secrets_are_not_reexposed_by_other_endpoints(harness: Harness) -> None:
    """作成で返した秘密値が、他のどの API 応答にも現れないこと（AC-1）。"""
    body = _post_profiles(harness).json()
    leaks = _battery_leaks(harness, body["public_id"], _secrets(body))
    assert leaks == {}


def test_login_response_does_not_echo_credentials(harness: Harness) -> None:
    """ログイン成功応答（204）が `login_id` / パスワードを echo しないこと。"""
    body = _post_profiles(harness).json()
    response = harness.client.post(
        "/api/v1/sessions",
        json={"login_id": body["login_id"], "password": body["initial_password"]},
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 204
    assert _leak_names(_response_text(response), _secrets(body)) == []


def test_change_password_returns_only_the_new_recovery_key(harness: Harness) -> None:
    """パスワード変更応答は新しい `recovery_key` だけを返し、旧値・`login_id` を含まないこと。"""
    created = _post_profiles(harness).json()
    old = _secrets(created)
    response = harness.client.post(
        "/api/v1/me/password",
        json={"current_password": old["initial_password"], "new_password": "changed-password-123"},
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"recovery_key"}
    assert body["recovery_key"] != old["recovery_key"]
    text = _response_text(response)
    for value in old.values():
        assert value not in text

    rotated = {"recovery_key": body["recovery_key"], **old}
    assert _battery_leaks(harness, created["public_id"], rotated) == {}


def test_recovery_returns_only_new_ids_and_rotates(harness: Harness) -> None:
    """回復応答は新しい `login_id` と `recovery_key` だけを返し、旧値を含まないこと。"""
    created = _post_profiles(harness).json()
    old = _secrets(created)
    response = harness.client.post(
        "/api/v1/recovery/password-resets",
        json={"recovery_key": old["recovery_key"], "new_password": "recovered-password-123"},
        headers=csrf_headers(harness.client),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"login_id", "recovery_key"}
    assert body["login_id"] != old["login_id"]
    assert body["recovery_key"] != old["recovery_key"]
    text = _response_text(response)
    for value in old.values():
        assert value not in text

    rotated = {**body, **old}
    assert _battery_leaks(harness, created["public_id"], rotated) == {}


def test_app_logs_do_not_contain_secrets(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """作成とその後の API 呼び出しで、捕捉できるアプリログに秘密値が出ないこと。"""
    with caplog.at_level(logging.DEBUG):
        body = _post_profiles(harness).json()
        secrets = _secrets(body)
        _battery(harness, body["public_id"])

    for value in secrets.values():
        assert value not in caplog.text


def test_scanner_detects_a_leaked_secret() -> None:
    """陽性対照: 走査ロジックが漏えいを検出できる。"""
    secrets = {"login_id": "L" * 24, "recovery_key": "R" * 32}
    assert _leak_names(f"body says {secrets['login_id']}", secrets) == ["login_id"]


def test_scanner_ignores_empty_and_clean_text() -> None:
    """陰性対照: 空値・非該当テキストは検出しない。"""
    assert _leak_names("clean response", {"login_id": "L" * 24}) == []
    assert _leak_names("anything", {"login_id": ""}) == []
