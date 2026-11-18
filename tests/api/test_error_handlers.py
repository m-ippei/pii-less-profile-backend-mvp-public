"""独自エラーハンドラの結合テスト（design/06-errors.md §7 /
design/05-api-contract.md §6 / design/07-security.md §8）。

T-07-04 の完了条件を、結線済み `main.app`（実ポート・CSRF・共通ヘッダー込み）と、
実バリデーションを起こす最小アプリで `TestClient` により観測する。

- 入力不正は FastAPI 標準形式ではなく `{"error":{"code":"VALIDATION_ERROR",...,"fields":[...]}}`
- `fields` は `field` / `code` だけで、入力値を echo しない
- 未知ルート 404 `NOT_FOUND`・メソッド不一致 405 `METHOD_NOT_ALLOWED`（同一 JSON 形式）
- 不正 Session は 401 `INVALID_SESSION` かつ `__Host-ap_session` を削除
- DB busy は 503 `SERVICE_UNAVAILABLE` + `Retry-After`（H-17）
- 想定外例外は 500 `INTERNAL_ERROR` で内部情報を漏らさない
- エラー応答にも共通ヘッダーと `Cache-Control: no-store` が付く
"""

import os
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import src.main as main
from src.app.errors import AppError
from src.app.http.cookies import SESSION_COOKIE
from src.app.http.errors import register_error_handlers
from src.app.http.middleware import REQUEST_ID_HEADER, CommonHeadersMiddleware
from src.identity.application.errors import InvalidSessionError
from src.shared.errors.errors import ErrorCode
from tests.support.http import Harness, post_json_with_csrf


class _RaisingCreateProfile:
    """`CreateProfile.execute` を差し替え、任意の例外を発生させるスタブ。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def execute(self) -> None:
        raise self._error


class _NewPasswordBody(BaseModel):
    """422 を実バリデーションで起こすための最小モデル（`new_password` のみ）。"""

    new_password: str = Field(min_length=12)


def _validation_app() -> FastAPI:
    """`register_error_handlers` を通した最小アプリ（共通ヘッダー付き）。"""
    local = FastAPI()
    local.add_middleware(CommonHeadersMiddleware)
    register_error_handlers(local)

    @local.post("/validate")
    def validate(body: _NewPasswordBody) -> dict[str, bool]:
        return {"ok": True}

    return local


def _raise(harness: Harness, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """`POST /profiles` が `error` を送出するように差し替える。"""
    monkeypatch.setattr(main, "build_create_profile", lambda: _RaisingCreateProfile(error))


def test_validation_error_uses_unified_format_and_hides_input() -> None:
    """H-10: 422 が独自形式で、`fields` に入力値が現れないこと。"""
    secret = "s3cr3t"
    client = TestClient(_validation_app(), base_url="https://testserver")

    response = client.post("/validate", json={"new_password": secret})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "入力内容に誤りがあります"
    fields = body["error"]["fields"]
    assert fields
    assert set(fields[0]) == {"field", "code"}
    assert fields[0]["field"] == "new_password"
    assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_validation_error_is_wrapped_by_common_headers() -> None:
    """422 応答にも共通ヘッダーと `X-Request-ID` が付くこと。"""
    client = TestClient(_validation_app(), base_url="https://testserver")

    response = client.post("/validate", json={"new_password": "short"})

    assert response.status_code == 422
    assert response.headers["x-content-type-options"] == "nosniff"
    assert REQUEST_ID_HEADER in response.headers


def test_unknown_route_is_404_not_found(harness: Harness) -> None:
    """H-18: 未知ルートが 404 `NOT_FOUND` の同一 JSON 形式で返ること。"""
    response = harness.client.get("/no-such-route")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "リソースが見つかりません"}
    }
    assert "fields" not in response.json()["error"]


def test_method_mismatch_is_405_method_not_allowed(harness: Harness) -> None:
    """H-18: メソッド不一致が 405 `METHOD_NOT_ALLOWED` の同一 JSON 形式で返ること。"""
    response = harness.client.get("/api/v1/profiles")

    assert response.status_code == 405
    assert response.json() == {
        "error": {"code": "METHOD_NOT_ALLOWED", "message": "許可されていないメソッドです"}
    }


def test_invalid_session_is_401_and_deletes_session_cookie(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-5 / H-6 / H-11: 401 `INVALID_SESSION` かつ `ap_session` を削除すること。"""
    _raise(harness, monkeypatch, InvalidSessionError())

    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

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


def test_application_error_uses_code_status(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """アプリケーションエラーが `code` に対応する status と固定文言で返ること。"""
    _raise(harness, monkeypatch, AppError(ErrorCode.PROFILE_NOT_PUBLISHABLE))

    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "PROFILE_NOT_PUBLISHABLE", "message": "公開できる内容がありません"}
    }


def test_db_busy_is_503_with_retry_after(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """H-17: DB busy が 503 `SERVICE_UNAVAILABLE` + `Retry-After`（1以上の整数秒）になること。"""
    _raise(harness, monkeypatch, sqlite3.OperationalError("database is locked"))

    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "SERVICE_UNAVAILABLE", "message": "サービスを一時的に利用できません"}
    }
    retry_after = response.headers["retry-after"]
    assert retry_after.isdigit()
    assert int(retry_after) >= 1


def test_non_busy_sqlite_error_is_500(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """H-9: busy 以外の `OperationalError` は 500 `INTERNAL_ERROR` になること。"""
    _raise(harness, monkeypatch, sqlite3.OperationalError("no such table: users"))

    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_unhandled_exception_is_500_with_headers_without_leaking(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-9: 想定外例外が 500 `INTERNAL_ERROR` で、内部情報を漏らさず共通ヘッダーが付くこと。"""
    secret = "internal-error-secret-detail"
    _raise(harness, monkeypatch, RuntimeError(secret))

    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "内部エラーが発生しました"}
    }
    assert secret not in response.text
    assert str(harness.db_path) not in response.text
    assert os.environ["APP_PEPPER"] not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert REQUEST_ID_HEADER in response.headers
