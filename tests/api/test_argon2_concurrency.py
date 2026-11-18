"""Argon2 実行枠のノンブロッキング制御のテスト（`design/07-security.md` §8 /
`design/10-infra-ops.md` §11 / `design/06-errors.md` §7 H-17）。

- `ARGON2_MAX_CONCURRENCY`（既定2）を設定から読む。不正は起動失敗（fail-closed）
- 枠が埋まっていれば待たずに拒否し、空けば通常処理に戻る
- パスワード検証の経路（ログイン）が枠を消費し、埋まれば 503 `SERVICE_UNAVAILABLE` ＋ `Retry-After`
"""

import pytest

import src.main as main
from src.app.http.argon2_guard import (
    DEFAULT_ARGON2_MAX_CONCURRENCY,
    Argon2Guard,
    Argon2SaturatedError,
    max_concurrency_from_env,
)
from tests.support.http import Harness, csrf_headers

LOGIN_PATH = "/api/v1/sessions"


def test_default_max_concurrency_is_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ARGON2_MAX_CONCURRENCY` 未設定時の既定が設計どおり2であること。"""
    monkeypatch.delenv("ARGON2_MAX_CONCURRENCY", raising=False)
    assert DEFAULT_ARGON2_MAX_CONCURRENCY == 2
    assert max_concurrency_from_env() == 2


def test_max_concurrency_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ARGON2_MAX_CONCURRENCY` を設定から参照すること。"""
    monkeypatch.setenv("ARGON2_MAX_CONCURRENCY", "5")
    assert max_concurrency_from_env() == 5
    assert Argon2Guard().limit == 5


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_invalid_max_concurrency_fails_closed(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """不正な `ARGON2_MAX_CONCURRENCY` は起動時に拒否されること（fail-closed）。"""
    monkeypatch.setenv("ARGON2_MAX_CONCURRENCY", bad)
    with pytest.raises(ValueError):
        max_concurrency_from_env()
    with pytest.raises(ValueError):
        Argon2Guard()


def test_guard_rejects_when_full_and_recovers_after_release() -> None:
    """枠を使い切ると待たずに拒否し、解放後に再び使えること。"""
    guard = Argon2Guard(1)
    assert guard.try_acquire()
    with pytest.raises(Argon2SaturatedError):
        with guard.slot():
            pass
    guard.release()
    with guard.slot():
        pass


def test_guard_releases_the_slot_on_exception() -> None:
    """枠内で例外が起きても枠が解放されること。"""
    guard = Argon2Guard(1)
    with pytest.raises(RuntimeError):
        with guard.slot():
            raise RuntimeError("boom")
    with guard.slot():
        pass


def test_argon2_saturation_returns_503_without_waiting(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """枠が埋まっていると、ログインのパスワード検証が待たずに 503 を返すこと。

    枠が空けば通常どおり処理され（この場合は資格情報不一致の 401）、枠が解放される。
    """
    monkeypatch.setattr(main, "argon2_guard", Argon2Guard(1))
    assert main.argon2_guard.try_acquire()  # 枠を使い切る

    login_id = "2" * 24
    body = {"login_id": login_id, "password": "whatever-password-1"}

    saturated = harness.client.post(LOGIN_PATH, json=body, headers=csrf_headers(harness.client))
    assert saturated.status_code == 503
    assert saturated.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert int(saturated.headers["Retry-After"]) >= 1

    main.argon2_guard.release()
    recovered = harness.client.post(LOGIN_PATH, json=body, headers=csrf_headers(harness.client))
    assert recovered.status_code == 401
