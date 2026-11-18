"""判定順（413 → CSRF → 認証 → 入力検証 → rate limit → use case）のテスト
（design/05-api-contract.md §6 / design/06-errors.md §7 / design/07-security.md §8）。

T-07-05 の完了条件を、`src/app/http/pipeline.py` のヘルパー単体（記録用 stub）と、
結線済み `main.app` への実 HTTP の両方で観測する。

- 本文上限違反（413）は CSRF 不正（403）より先
- CSRF 不正は認証（不正 Session を送出するエンドポイント）より先に 403
- 認証失敗で入力検証が呼ばれない
- 入力検証失敗で rate limit が呼ばれない
- rate limit 拒否で use case が呼ばれない
"""

import pytest

import src.main as main
from src.app.errors import AppError
from src.app.http.middleware import MAX_BODY_BYTES
from src.app.http.pipeline import run_request_pipeline
from src.identity.application.errors import InvalidSessionError
from src.shared.errors.errors import ErrorCode
from tests.support.http import Harness, post_json_with_csrf


class _RaisingCreateProfile:
    """`CreateProfile.execute` を差し替え、任意の例外を発生させるスタブ。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def execute(self) -> None:
        raise self._error


def test_pipeline_runs_stages_in_the_designated_order() -> None:
    """成功時は 認証 → 入力検証 → rate limit → use case の順に1回ずつ呼ばれること。"""
    calls: list[str] = []

    def authenticate() -> str:
        calls.append("authenticate")
        return "principal"

    def validate() -> int:
        calls.append("validate")
        return 7

    def rate_limit(auth: str | None, value: int | None) -> None:
        calls.append("rate_limit")

    def execute(auth: str | None, value: int | None) -> str:
        calls.append("execute")
        return f"{auth}:{value}"

    result = run_request_pipeline(
        execute, authenticate=authenticate, validate=validate, rate_limit=rate_limit
    )

    assert calls == ["authenticate", "validate", "rate_limit", "execute"]
    assert result == "principal:7"


def test_authentication_failure_skips_later_stages() -> None:
    """認証失敗で入力検証・rate limit・use case が呼ばれないこと。"""
    calls: list[str] = []

    def authenticate() -> str:
        calls.append("authenticate")
        raise InvalidSessionError()

    def validate() -> int:
        calls.append("validate")
        return 7

    def rate_limit(auth: str | None, value: int | None) -> None:
        calls.append("rate_limit")

    def execute(auth: str | None, value: int | None) -> None:
        calls.append("execute")

    with pytest.raises(InvalidSessionError):
        run_request_pipeline(
            execute, authenticate=authenticate, validate=validate, rate_limit=rate_limit
        )

    assert calls == ["authenticate"]


def test_validation_failure_skips_rate_limit_and_use_case() -> None:
    """入力検証失敗で rate limit と use case が呼ばれないこと。"""
    calls: list[str] = []

    def authenticate() -> str:
        calls.append("authenticate")
        return "principal"

    def validate() -> int:
        calls.append("validate")
        raise AppError(ErrorCode.VALIDATION_ERROR)

    def rate_limit(auth: str | None, value: int | None) -> None:
        calls.append("rate_limit")

    def execute(auth: str | None, value: int | None) -> None:
        calls.append("execute")

    with pytest.raises(AppError):
        run_request_pipeline(
            execute, authenticate=authenticate, validate=validate, rate_limit=rate_limit
        )

    assert calls == ["authenticate", "validate"]


def test_rate_limit_rejection_skips_use_case() -> None:
    """rate limit 拒否（429 相当）で use case が呼ばれないこと。"""
    calls: list[str] = []

    def authenticate() -> str:
        calls.append("authenticate")
        return "principal"

    def validate() -> int:
        calls.append("validate")
        return 7

    def rate_limit(auth: str | None, value: int | None) -> None:
        calls.append("rate_limit")
        raise AppError(ErrorCode.RATE_LIMITED)

    def execute(auth: str | None, value: int | None) -> None:
        calls.append("execute")

    with pytest.raises(AppError):
        run_request_pipeline(
            execute, authenticate=authenticate, validate=validate, rate_limit=rate_limit
        )

    assert calls == ["authenticate", "validate", "rate_limit"]


def test_optional_stages_are_skipped() -> None:
    """認証・入力検証・rate limit が省略可能で、その場合 use case が直接呼ばれること。"""
    calls: list[str] = []

    def execute(auth: object | None, value: object | None) -> str:
        calls.append("execute")
        return "done"

    result = run_request_pipeline(execute)

    assert calls == ["execute"]
    assert result == "done"


def test_body_limit_precedes_csrf_on_oversized_request(harness: Harness) -> None:
    """本文上限違反（413）が CSRF 不正（403）より先に判定されること。"""
    response = harness.client.post(
        "/api/v1/profiles",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {"code": "PAYLOAD_TOO_LARGE", "message": "リクエスト本文が大きすぎます"}
    }


def test_csrf_rejection_precedes_use_case(harness: Harness) -> None:
    """CSRF 不正（403）が use case に到達しないこと。"""
    response = harness.client.post(
        "/api/v1/profiles", json={}, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_csrf_rejection_precedes_authentication(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSRF 不正が認証より先に 403 になること（到達すれば 401 のエンドポイントで確認）。"""
    monkeypatch.setattr(
        main, "build_create_profile", lambda: _RaisingCreateProfile(InvalidSessionError())
    )

    response = harness.client.post(
        "/api/v1/profiles",
        json={},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": "forged"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
    assert "INVALID_SESSION" not in response.text


def test_valid_request_reaches_the_use_case(harness: Harness) -> None:
    """全段を通過したリクエストが use case に到達すること（陽性対照）。"""
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 201
