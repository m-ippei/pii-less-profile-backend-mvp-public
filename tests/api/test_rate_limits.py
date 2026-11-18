"""5つのレート制限の各エンドポイント適用のテスト（design/07-security.md §8 Rate limit /
design/05-api-contract.md §6 判定順 / design/06-errors.md §7 H-13）。

T-10-03 の完了条件を、実 SQLite と結線済み `main.app` に対して `TestClient` で観測する。

- 5制限それぞれで上限超過が 429 ＋ `Retry-After`（AC-9。ログインaccount / ログインIP / 回復 /
  パスワード変更 / Profile作成）
- ログインを `login_id` だけ変え続けても IP 単位（20回/分）で 429
- 入力検証失敗（422）は rate limit の前（判定順）に確定し、計上されない
- account 単位 key は「最初の `_` より前」の `login_id`（suffix 違いで回避できない）
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx2 import Response

import src.main as main
from src.app.http.ratelimit import (
    LOGIN_ACCOUNT,
    LOGIN_IP,
    PASSWORD_CHANGE,
    PROFILE_CREATE,
    RECOVERY_PER_HOUR,
    RECOVERY_PER_MINUTE,
    RateLimit,
    RateLimitExceededError,
    RateLimitKind,
    RateLimitScope,
    enforce,
)
from src.shared.ratelimit.ratelimit import InMemoryRateLimiter, RateLimitResult
from tests.support.fake_clock import FakeClock
from tests.support.http import Harness, csrf_headers, post_json_with_csrf

LOGIN_PATH = "/api/v1/sessions"
RECOVERY_PATH = "/api/v1/recovery/password-resets"
PASSWORD_PATH = "/api/v1/me/password"
PROFILES_PATH = "/api/v1/profiles"

WRONG_PASSWORD = "wrong-password-123"
NEW_PASSWORD = "new-password-123"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, PROFILES_PATH)
    assert response.status_code == 201
    return response.json()


def _login(harness: Harness, login_id: str, password: str = WRONG_PASSWORD) -> Response:
    return harness.client.post(
        LOGIN_PATH,
        json={"login_id": login_id, "password": password},
        headers=csrf_headers(harness.client),
    )


def _recover(harness: Harness, recovery_key: str) -> Response:
    return harness.client.post(
        RECOVERY_PATH,
        json={"recovery_key": recovery_key, "new_password": NEW_PASSWORD},
        headers=csrf_headers(harness.client),
    )


def _change(harness: Harness) -> Response:
    return harness.client.post(
        PASSWORD_PATH,
        json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
        headers=csrf_headers(harness.client),
    )


def _assert_rate_limited(response: Response) -> None:
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert int(response.headers["Retry-After"]) >= 1


def test_login_account_limit_is_enforced(harness: Harness) -> None:
    """ログイン（account単位）5回/分を超えると 429 ＋ `Retry-After` になること。"""
    account = _create_account(harness)
    for _ in range(LOGIN_ACCOUNT.limit):
        assert _login(harness, account["login_id"]).status_code == 401

    _assert_rate_limited(_login(harness, account["login_id"]))


def test_login_ip_limit_is_enforced_across_rotating_login_ids(harness: Harness) -> None:
    """`login_id` を変え続けても IP 単位（20回/分）で 429 になること（総当たり対策）。"""
    for index in range(LOGIN_IP.limit):
        login_id = "2" * 23 + BASE58[index]
        assert _login(harness, login_id).status_code == 401

    _assert_rate_limited(_login(harness, "2" * 23 + BASE58[LOGIN_IP.limit]))


def test_recovery_minute_limit_is_enforced(harness: Harness) -> None:
    """回復（IP単位）5回/分を超えると 429 になること。"""
    unknown = "2" * 32
    for _ in range(RECOVERY_PER_MINUTE.limit):
        assert _recover(harness, unknown).status_code == 400

    _assert_rate_limited(_recover(harness, unknown))


def test_recovery_hourly_limit_is_enforced(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回復（IP単位）20回/時を超えると 429 になること。分窓は FakeClock でリセットする。"""
    clock = FakeClock(datetime(2026, 10, 3, tzinfo=UTC))
    monkeypatch.setattr(main, "rate_limiter", InMemoryRateLimiter(clock))
    unknown = "2" * 32

    batches = RECOVERY_PER_HOUR.limit // RECOVERY_PER_MINUTE.limit
    for _ in range(batches):
        for _ in range(RECOVERY_PER_MINUTE.limit):
            assert _recover(harness, unknown).status_code == 400
        clock.advance(timedelta(seconds=61))  # 分窓を流し、時の窓へだけ積む

    _assert_rate_limited(_recover(harness, unknown))


def test_password_change_limit_is_enforced_per_session(harness: Harness) -> None:
    """パスワード変更（Session単位）5回/分を超えると 429 になること。現パスワード誤りも計上。"""
    _create_account(harness)
    for _ in range(PASSWORD_CHANGE.limit):
        assert _change(harness).status_code == 403

    _assert_rate_limited(_change(harness))


def test_profile_create_limit_is_enforced(harness: Harness) -> None:
    """Profile作成（IP単位）10回/時を超えると 429 になること。"""
    for _ in range(PROFILE_CREATE.limit):
        assert post_json_with_csrf(harness.client, PROFILES_PATH).status_code == 201

    _assert_rate_limited(post_json_with_csrf(harness.client, PROFILES_PATH))


def test_validation_failure_is_not_counted(harness: Harness) -> None:
    """入力検証失敗（422）は rate limit の前に確定し、計上されないこと（判定順）。"""
    login_id = "2" * 24
    for _ in range(LOGIN_ACCOUNT.limit - 1):
        assert _login(harness, login_id).status_code == 401

    invalid = harness.client.post(
        LOGIN_PATH,
        json={"login_id": "short", "password": ""},
        headers=csrf_headers(harness.client),
    )
    assert invalid.status_code == 422

    # 422 が計上されていれば、この5回目は拒否されるはず。
    assert _login(harness, login_id).status_code == 401
    _assert_rate_limited(_login(harness, login_id))


def test_account_key_ignores_the_display_suffix(harness: Harness) -> None:
    """account 単位 key が「最初の `_` より前」で計算され、suffix 違いで回避できないこと。"""
    account = _create_account(harness)
    login_id = account["login_id"]
    for _ in range(LOGIN_ACCOUNT.limit):
        assert _login(harness, f"{login_id}_labelA").status_code == 401

    _assert_rate_limited(_login(harness, f"{login_id}_labelB"))


class _StubLimiter:
    """`enforce` の単体テスト用。`check` が事前に与えた結果を順に返すだけ。"""

    def __init__(self, results: list[RateLimitResult]) -> None:
        self._results = results

    def check(self, key: str, limit: int, window: timedelta) -> RateLimitResult:
        return self._results.pop(0)


def test_enforce_raises_with_the_longest_retry_after_and_the_exceeded_kinds() -> None:
    """複数の制限が超過したとき、最大 `Retry-After` と超過種別を載せて送出すること。"""
    account = RateLimit(
        RateLimitKind.LOGIN_ACCOUNT, RateLimitScope.ACCOUNT, 5, timedelta(minutes=1)
    )
    ip = RateLimit(RateLimitKind.LOGIN_IP, RateLimitScope.IP, 20, timedelta(minutes=1))
    limiter = _StubLimiter(
        [RateLimitResult(allowed=False, retry_after=3), RateLimitResult(False, 9)]
    )

    with pytest.raises(RateLimitExceededError) as excinfo:
        enforce(limiter, ((account, "a"), (ip, "b")))

    assert excinfo.value.retry_after == 9
    assert excinfo.value.kinds == (RateLimitKind.LOGIN_ACCOUNT, RateLimitKind.LOGIN_IP)


def test_enforce_is_silent_when_all_checks_allowed() -> None:
    """すべて許容なら何も送出しないこと。"""
    policy = RateLimit(RateLimitKind.LOGIN_IP, RateLimitScope.IP, 5, timedelta(minutes=1))
    limiter = _StubLimiter([RateLimitResult(True, 0), RateLimitResult(True, 0)])

    enforce(limiter, ((policy, "a"), (policy, "b")))
