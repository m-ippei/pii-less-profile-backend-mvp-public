"""セキュリティイベントログのテスト（design/15-logging.md §17「ログ」）。

- 種別は許可8種のみ、項目は許可リスト（時刻・`event`・request ID・`ip_daily`・`kind`）のみ
- `ip_daily` は `HMAC-SHA256(key=APP_PEPPER, msg="ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)`。
  日付が変わると値が変わる
- 生IPはログに出ない
- 主要フロー（作成・ログイン成否・CSRF失敗・回復成否・パスワード変更・rate limit）で実際に記録される
- `rate_limited` は超過種別 `kind` ごとに記録される
- 上限到達（fail-closed）は `app.limit` に集計値（`count` のみ）で出る
"""

import hmac
import json
import logging
from datetime import UTC, datetime

import pytest
from httpx2 import Response

import src.main as main
from src.app.http.logging import (
    CAP_REACHED_EVENT,
    CAP_REACHED_FIELDS,
    SECURITY_EVENT_FIELDS,
    RateLimitCapLogger,
    SecurityEvent,
    ip_daily,
)
from src.app.http.ratelimit import PROFILE_CREATE, RateLimitKind
from src.shared.clock.clock import SystemClock
from src.shared.ratelimit.ratelimit import InMemoryRateLimiter
from src.shared.security.security import SecretHasher
from tests.support.env import PEPPER
from tests.support.http import Harness, csrf_headers, post_json_with_csrf

LOGIN_PATH = "/api/v1/sessions"
RECOVERY_PATH = "/api/v1/recovery/password-resets"
PASSWORD_PATH = "/api/v1/me/password"
PROFILES_PATH = "/api/v1/profiles"

WRONG_PASSWORD = "wrong-password-123"
CHANGED_PASSWORD = "changed-password-123"
RECOVERED_PASSWORD = "recovered-password-123"
ALLOWED_EVENTS = {
    "login_failed",
    "login_succeeded",
    "recovery_failed",
    "recovery_succeeded",
    "password_changed",
    "profile_created",
    "rate_limited",
    "csrf_failed",
}


def _security_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "app.security"
    ]


def _limit_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage()) for record in caplog.records if record.name == "app.limit"
    ]


def test_security_event_fields_follow_the_design() -> None:
    """許可項目に `kind` があり `user_pseudonym` が無いこと（design/15-logging.md §17）。"""
    assert SECURITY_EVENT_FIELDS == {"timestamp", "event", "request_id", "ip_daily", "kind"}
    assert "user_pseudonym" not in SECURITY_EVENT_FIELDS


def test_security_event_types_are_exactly_the_eight() -> None:
    """記録してよいイベント種別が設計どおり8種であること。"""
    assert {event.value for event in SecurityEvent} == ALLOWED_EVENTS


def test_ip_daily_matches_the_design_formula() -> None:
    """`ip_daily` が `HMAC(pepper, "ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)` であること。"""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    expected = hmac.new(PEPPER, b"ip_daily:2026-10-09:203.0.113.9", "sha256").hexdigest()

    assert ip_daily("203.0.113.9", now, SecretHasher(PEPPER)) == expected


def test_ip_daily_changes_with_the_date() -> None:
    """同じ IP でも日付が変わると値が変わり、同じ日なら同じであること。"""
    hasher = SecretHasher(PEPPER)
    day1 = datetime(2026, 10, 9, 23, 59, tzinfo=UTC)
    day1_later = datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    day2 = datetime(2026, 10, 10, 0, 1, tzinfo=UTC)

    assert ip_daily("203.0.113.9", day1, hasher) != ip_daily("203.0.113.9", day2, hasher)
    assert ip_daily("203.0.113.9", day1, hasher) == ip_daily("203.0.113.9", day1_later, hasher)


def _create_account(harness: Harness) -> dict[str, str]:
    response = post_json_with_csrf(harness.client, PROFILES_PATH)
    assert response.status_code == 201
    return response.json()


def _login(harness: Harness, login_id: str, password: str) -> Response:
    return harness.client.post(
        LOGIN_PATH,
        json={"login_id": login_id, "password": password},
        headers=csrf_headers(harness.client),
    )


def _recover(harness: Harness, recovery_key: str) -> Response:
    return harness.client.post(
        RECOVERY_PATH,
        json={"recovery_key": recovery_key, "new_password": RECOVERED_PASSWORD},
        headers=csrf_headers(harness.client),
    )


def _change(harness: Harness, current_password: str) -> Response:
    return harness.client.post(
        PASSWORD_PATH,
        json={"current_password": current_password, "new_password": CHANGED_PASSWORD},
        headers=csrf_headers(harness.client),
    )


def test_every_security_event_is_recorded_with_allowed_fields_only(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """主要フローで8種すべてが記録され、各行の項目が許可リスト内であること。"""
    caplog.set_level(logging.INFO, logger="app.security")

    account = _create_account(harness)  # profile_created
    assert _login(harness, account["login_id"], WRONG_PASSWORD).status_code == 401  # login_failed
    assert _login(harness, account["login_id"], account["initial_password"]).status_code == 204
    harness.client.post(  # csrf_failed（CSRF ヘッダーなし）
        RECOVERY_PATH,
        json={"recovery_key": "2" * 32, "new_password": RECOVERED_PASSWORD},
    )
    assert _recover(harness, "2" * 32).status_code == 400  # recovery_failed
    changed = _change(harness, account["initial_password"])  # password_changed
    assert changed.status_code == 200
    assert _recover(harness, changed.json()["recovery_key"]).status_code == 200  # succeeded
    for _ in range(PROFILE_CREATE.limit):  # 10回目で rate_limited
        post_json_with_csrf(harness.client, PROFILES_PATH)

    payloads = _security_payloads(caplog)
    assert {payload["event"] for payload in payloads} == ALLOWED_EVENTS
    for payload in payloads:
        assert set(payload) <= SECURITY_EVENT_FIELDS
        assert payload["event"] in ALLOWED_EVENTS
        assert str(payload["ip_daily"])
    rate_limited = [payload for payload in payloads if payload["event"] == "rate_limited"]
    assert rate_limited
    assert all(
        payload["kind"] in {kind.value for kind in RateLimitKind} for payload in rate_limited
    )


def test_raw_ip_is_not_logged(harness: Harness, monkeypatch, caplog) -> None:
    """生IPをログに出さず、`ip_daily` に置き換えること。"""
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    caplog.set_level(logging.INFO, logger="app.security")
    raw_ip = "203.0.113.9"
    account = _create_account(harness)

    harness.client.post(
        LOGIN_PATH,
        json={"login_id": account["login_id"], "password": WRONG_PASSWORD},
        headers={**csrf_headers(harness.client), "X-Forwarded-For": raw_ip},
    )

    payloads = _security_payloads(caplog)
    assert any(payload["event"] == "login_failed" for payload in payloads)
    assert all("ip_daily" in payload for payload in payloads)
    assert raw_ip not in caplog.text


def test_rate_limited_is_recorded_per_exceeded_kind(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """同時に超過した制限ごとに `rate_limited` が1件ずつ、`kind` 付きで記録されること。"""
    caplog.set_level(logging.INFO, logger="app.security")
    monkeypatch.setattr(main, "rate_limiter", InMemoryRateLimiter(SystemClock(), max_keys=1))

    # 1回目: account キーが登録され、IP キーが上限到達で拒否される。
    assert _login(harness, "2" * 24, WRONG_PASSWORD).status_code == 429
    caplog.clear()

    # 2回目: 別 login_id で account / IP の両方が上限到達で拒否される。
    assert _login(harness, "3" * 24, WRONG_PASSWORD).status_code == 429

    kinds = [
        payload["kind"]
        for payload in _security_payloads(caplog)
        if payload["event"] == "rate_limited"
    ]
    assert kinds == ["login_account", "login_ip"]


def test_cap_reached_is_logged_as_an_aggregate(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """上限到達（fail-closed）が `app.limit` に集計値（数値のみ）で出ること。"""
    caplog.set_level(logging.INFO, logger="app.limit")
    monkeypatch.setattr(main, "rate_limiter", InMemoryRateLimiter(SystemClock(), max_keys=1))

    assert _login(harness, "2" * 24, WRONG_PASSWORD).status_code == 429

    payloads = _limit_payloads(caplog)
    assert payloads
    assert payloads[-1]["event"] == CAP_REACHED_EVENT
    assert isinstance(payloads[-1]["count"], int)
    assert payloads[-1]["count"] >= 1
    for payload in payloads:
        assert set(payload) <= CAP_REACHED_FIELDS


def test_rate_limit_cap_logger_emits_delta_and_guards_restart() -> None:
    """`RateLimitCapLogger` が増分だけを出し、カウンタ減少時は基準を取り直すこと。"""
    logger = logging.getLogger("tests.cap_logger")
    logger.setLevel(logging.INFO)
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    logger.addHandler(handler)
    try:
        reporter = RateLimitCapLogger(logger)
        reporter.emit_delta(0)
        reporter.emit_delta(3)
        reporter.emit_delta(3)
        reporter.emit_delta(5)
        reporter.emit_delta(1)
    finally:
        logger.removeHandler(handler)

    assert [json.loads(message) for message in messages] == [
        {"event": CAP_REACHED_EVENT, "count": 3},
        {"event": CAP_REACHED_EVENT, "count": 2},
        {"event": CAP_REACHED_EVENT, "count": 1},
    ]
