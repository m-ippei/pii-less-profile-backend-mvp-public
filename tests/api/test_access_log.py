"""運用ログのテスト（design/15-logging.md §17「ログ」/ design/10-infra-ops.md §11）。

- 各行の項目が許可リスト（request ID・ルートテンプレート・メソッド・status・処理時間・
  安全な内部エラーコード）のみ
- ルートはテンプレートで記録する（生パス・秘密値を出さない）
- uvicorn のアクセスログは無効
- 想定外例外のとき、ログに request 本文・ヘッダー・Cookie が出ない
"""

import json
import logging

import pytest

import src.main as main
from src.app.http.logging import (
    ACCESS_LOGGER_NAME,
    EXCEPTION_LOGGER_NAME,
    LIMIT_LOGGER_NAME,
    OPERATIONAL_FIELDS,
    SECURITY_LOGGER_NAME,
)
from tests.support.http import Harness, csrf_headers


def _access_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage()) for record in caplog.records if record.name == "app.access"
    ]


def test_operational_log_contains_only_allowed_fields(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """運用ログの各行が許可項目だけを含むこと。"""
    caplog.set_level(logging.INFO, logger="app.access")

    harness.client.get("/healthz")

    payloads = _access_payloads(caplog)
    assert payloads
    for payload in payloads:
        assert set(payload) <= OPERATIONAL_FIELDS
    last = payloads[-1]
    assert last["method"] == "GET"
    assert last["status"] == 200
    assert isinstance(last["duration_ms"], (int, float))


def test_operational_log_records_the_route_template(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """既知ルートをテンプレートで記録し、`public_id` などの生値を出さないこと。"""
    caplog.set_level(logging.INFO, logger="app.access")

    harness.client.get("/api/v1/profiles/p_1234567890abcdef")

    assert _access_payloads(caplog)[-1]["route"] == "/api/v1/profiles/{public_id}"


def test_uvicorn_access_logger_is_disabled() -> None:
    """uvicorn のアクセスログが無効化されていること。"""
    assert logging.getLogger("uvicorn.access").disabled is True


def test_logger_channels_follow_the_design() -> None:
    """チャネル（ロガー名）が設計どおり揃っていること（design/15-logging.md §17）。"""
    assert {
        ACCESS_LOGGER_NAME,
        SECURITY_LOGGER_NAME,
        EXCEPTION_LOGGER_NAME,
        LIMIT_LOGGER_NAME,
    } == {"app.access", "app.security", "app.error", "app.limit"}


def test_unhandled_exception_log_does_not_leak_request_data(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """想定外例外のとき、ログに本文・ヘッダー・Cookie の値が出ないこと。"""
    caplog.set_level(logging.DEBUG)
    secret = "sUpErSeCrEtReQuEsTvAlUe"

    class _Boom:
        def execute(self) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "build_create_profile", lambda: _Boom())
    harness.client.cookies.set("note", secret, domain="testserver")

    response = harness.client.post(
        "/api/v1/profiles",
        json={"secret": secret},
        headers={**csrf_headers(harness.client), "X-Secret-Header": secret},
    )

    assert response.status_code == 500
    assert secret not in caplog.text
    assert _access_payloads(caplog)[-1]["error_code"] == "INTERNAL_ERROR"
