"""レート制限基盤のテスト（design/07-security.md §8 Rate limit /
design/06-errors.md §7 H-13 / design/05-api-contract.md §6 判定順）。

T-10-02 の完了条件のうち次を観測する。

- 5つのレート制限の限界値（回数・窓・key 種別）が1箇所の設定から参照されている
- 429 応答が `RATE_LIMITED` の固定文言と `Retry-After` ヘッダーを持つ
- アプリで共有する単一の `RateLimiter` インスタンスが結線されている（リクエストごとに作らない）

D-14 で追加した key 上限の env 化（`RATE_LIMIT_MAX_KEYS`、既定10,000、不正は起動失敗）
も本ファイルで観測する。
"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.main as main
from src.app.http.errors import register_error_handlers
from src.app.http.ratelimit import (
    ALL_RATE_LIMITS,
    LOGIN_ACCOUNT,
    LOGIN_IP,
    PASSWORD_CHANGE,
    PROFILE_CREATE,
    RECOVERY_PER_HOUR,
    RECOVERY_PER_MINUTE,
    RateLimitExceededError,
    RateLimitKind,
    RateLimitScope,
)
from src.shared.clock.clock import SystemClock
from src.shared.ratelimit.ratelimit import MAX_KEYS, InMemoryRateLimiter, max_keys_from_env


def _app_that_raises(error: Exception) -> FastAPI:
    """指定の例外を送出するだけの検証用アプリ（本番のエラーハンドラを登録する）。"""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise")
    def raise_it() -> None:
        raise error

    return app


def test_login_account_limit_matches_the_design() -> None:
    """ログイン（account単位）: kind=login_account、IP ＋ HMAC(login_id)、5回/分であること。"""
    assert LOGIN_ACCOUNT.kind is RateLimitKind.LOGIN_ACCOUNT
    assert LOGIN_ACCOUNT.scope is RateLimitScope.ACCOUNT
    assert LOGIN_ACCOUNT.limit == 5
    assert LOGIN_ACCOUNT.window == timedelta(minutes=1)


def test_login_ip_limit_matches_the_design() -> None:
    """ログイン（IP単位）: kind=login_ip、IP のみ、20回/分であること（総当たり対策に必須）。"""
    assert LOGIN_IP.kind is RateLimitKind.LOGIN_IP
    assert LOGIN_IP.scope is RateLimitScope.IP
    assert LOGIN_IP.limit == 20
    assert LOGIN_IP.window == timedelta(minutes=1)


def test_recovery_limits_match_the_design() -> None:
    """回復: IP のみ、5回/分 と 20回/時 の2つであること。"""
    assert RECOVERY_PER_MINUTE.kind is RateLimitKind.RECOVERY_MINUTE
    assert RECOVERY_PER_MINUTE.scope is RateLimitScope.IP
    assert RECOVERY_PER_MINUTE.limit == 5
    assert RECOVERY_PER_MINUTE.window == timedelta(minutes=1)
    assert RECOVERY_PER_HOUR.kind is RateLimitKind.RECOVERY_HOUR
    assert RECOVERY_PER_HOUR.scope is RateLimitScope.IP
    assert RECOVERY_PER_HOUR.limit == 20
    assert RECOVERY_PER_HOUR.window == timedelta(hours=1)


def test_password_change_limit_matches_the_design() -> None:
    """パスワード変更: kind=password_change、session id 単位、5回/分であること。"""
    assert PASSWORD_CHANGE.kind is RateLimitKind.PASSWORD_CHANGE
    assert PASSWORD_CHANGE.scope is RateLimitScope.SESSION
    assert PASSWORD_CHANGE.limit == 5
    assert PASSWORD_CHANGE.window == timedelta(minutes=1)


def test_profile_create_limit_matches_the_design() -> None:
    """Profile作成: kind=profile_create、IP 単位、10回/時であること。"""
    assert PROFILE_CREATE.kind is RateLimitKind.PROFILE_CREATE
    assert PROFILE_CREATE.scope is RateLimitScope.IP
    assert PROFILE_CREATE.limit == 10
    assert PROFILE_CREATE.window == timedelta(hours=1)


def test_rate_limit_kinds_are_the_six_design_values() -> None:
    """制限種別が設計どおりの閉じた6値であること（design/07-security.md §8）。"""
    assert {kind.value for kind in RateLimitKind} == {
        "login_account",
        "login_ip",
        "recovery_minute",
        "recovery_hour",
        "password_change",
        "profile_create",
    }


def test_each_limit_has_a_distinct_kind() -> None:
    """5制限（回復は2窓）がそれぞれ異なる種別を持つこと。"""
    kinds = [policy.kind for policy in ALL_RATE_LIMITS]
    assert len(kinds) == len(set(kinds))


def test_all_limits_are_centralized_in_one_place() -> None:
    """5つの制限（回復は2窓）が1箇所の設定に集約されていること。"""
    assert set(ALL_RATE_LIMITS) == {
        LOGIN_ACCOUNT,
        LOGIN_IP,
        RECOVERY_PER_MINUTE,
        RECOVERY_PER_HOUR,
        PASSWORD_CHANGE,
        PROFILE_CREATE,
    }


def test_rate_limit_rejection_returns_429_with_fixed_message_and_retry_after() -> None:
    """H-13: 429 `RATE_LIMITED` の固定文言と `Retry-After` を返すこと。"""
    client = TestClient(_app_that_raises(RateLimitExceededError(retry_after=42)))

    response = client.get("/raise")

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "リクエストが多すぎます。しばらくしてから再試行してください",
        }
    }
    assert response.headers["Retry-After"] == "42"


def test_rate_limit_handler_is_specific_to_the_rejection() -> None:
    """`RateLimitExceededError` が `AppError` 一般のハンドラより優先されること。

    `Retry-After` はこの経路でしか付かない。汎用のアプリケーションエラー応答に
    フォールバックしていないことを、ヘッダーの有無で確認する。
    """
    client = TestClient(_app_that_raises(RateLimitExceededError(retry_after=1)))

    response = client.get("/raise")

    assert response.headers.get("Retry-After") == "1"


def test_shared_rate_limiter_is_wired_once() -> None:
    """アプリが単一のインメモリ `RateLimiter` を共有していること。"""
    assert isinstance(main.rate_limiter, InMemoryRateLimiter)
    assert main.rate_limiter is main.rate_limiter


def test_default_max_keys_is_ten_thousand(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RATE_LIMIT_MAX_KEYS` 未設定時の既定が設計どおり10,000であること（D-14）。"""
    monkeypatch.delenv("RATE_LIMIT_MAX_KEYS", raising=False)
    assert MAX_KEYS == 10_000
    assert max_keys_from_env() == 10_000


def test_max_keys_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RATE_LIMIT_MAX_KEYS` を設定から参照し、実装の key 上限に反映できること（D-14）。"""
    monkeypatch.setenv("RATE_LIMIT_MAX_KEYS", "3")
    assert max_keys_from_env() == 3
    limiter = InMemoryRateLimiter(SystemClock(), max_keys=max_keys_from_env())
    assert limiter.max_keys == 3


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_invalid_max_keys_fails_closed(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """不正な `RATE_LIMIT_MAX_KEYS` は起動時に拒否されること（fail-closed、D-14）。"""
    monkeypatch.setenv("RATE_LIMIT_MAX_KEYS", bad)
    with pytest.raises(ValueError):
        max_keys_from_env()


def test_shared_rate_limiter_uses_the_design_default() -> None:
    """アプリ共有の rate limiter が既定の key 上限10,000で結線されていること（D-14）。"""
    assert main.rate_limiter.max_keys == 10_000
