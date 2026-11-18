"""Identity ドメインの時刻規則のテスト（design/03-domain-model.md §4）。

このファイルが守る設計上の約束:

1. `Session.is_valid(now)` = `revoked_at is None and now < expires_at`。
2. `expires_at` は作成時から14日の絶対期限。延長しない。
3. `last_seen_at` は作成時に設定し、以後は有効な認証済みリクエストで
   前回から5分以上経過した時だけ更新する。
4. `revoked_at` は未失効なら一度だけ設定する。期限到来では設定しない。
5. `password_changed_at` は初期発行・通常変更・回復成功時に設定し、
   `updated_at` は永続的な可変属性が実際に変わった成功時だけ更新する。

時刻はすべて `FakeClock` で固定する（実時間に依存しない）。
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from src.identity.domain.entities import (
    LAST_SEEN_INTERVAL,
    SESSION_TTL,
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.value_objects import UserId
from tests.support.fake_clock import FakeClock

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
LOGIN_ID_HASH = "ab" * 32
NEW_LOGIN_ID_HASH = "aa" * 32
PASSWORD_HASH = "$argon2id$dummy"
NEW_PASSWORD_HASH = "$argon2id$new"
RECOVERY_KEY_HASH = "cd" * 32
NEW_RECOVERY_KEY_HASH = "dd" * 32
TOKEN_HASH = "ef" * 32


def _session(clock: FakeClock) -> Session:
    return Session.create(id=USER_ID, user_id=USER_ID, token_hash=TOKEN_HASH, now=clock.now())


def _login_credential(clock: FakeClock) -> LoginCredential:
    return LoginCredential.issue(
        user_id=USER_ID,
        login_id_hash=LOGIN_ID_HASH,
        password_hash=PASSWORD_HASH,
        now=clock.now(),
    )


def test_session_is_valid_requires_no_revocation_and_no_expiry() -> None:
    """`is_valid(now)` は失効していないことと期限未到来の論理積であること。

    期限の1マイクロ秒前は有効、ちょうど `expires_at` の時は無効
    （`now < expires_at` の狭義比較）。期限後ももちろん無効。
    """
    clock = FakeClock(START)
    session = _session(clock)
    assert session.is_valid(clock.now())
    clock.set(session.expires_at - timedelta(microseconds=1))
    assert session.is_valid(clock.now())
    clock.set(session.expires_at)
    assert not session.is_valid(clock.now())
    clock.advance(timedelta(days=1))
    assert not session.is_valid(clock.now())


def test_session_is_invalid_once_revoked_even_before_expiry() -> None:
    """失効したSessionは期限内でも無効であること。"""
    clock = FakeClock(START)
    session = _session(clock)
    clock.advance(timedelta(days=1))
    session.revoke(clock.now())
    assert session.revoked_at == START + timedelta(days=1)
    assert not session.is_valid(clock.now())
    clock.set(START)
    assert not session.is_valid(clock.now())


def test_session_expires_at_is_created_at_plus_14_days() -> None:
    """`expires_at` は作成時+14日で、`last_seen_at` は作成時に設定されること。"""
    clock = FakeClock(START)
    session = _session(clock)
    assert SESSION_TTL == timedelta(days=14)
    assert session.created_at == START
    assert session.last_seen_at == START
    assert session.expires_at == START + SESSION_TTL


def test_session_expiry_is_never_extended() -> None:
    """14日は絶対期限であり、touch を繰り返しても延長されないこと。"""
    clock = FakeClock(START)
    session = _session(clock)
    clock.advance(timedelta(minutes=30))
    assert session.touch(clock.now())
    assert session.expires_at == START + SESSION_TTL
    clock.advance(timedelta(days=13))
    assert session.touch(clock.now())
    assert session.expires_at == START + SESSION_TTL


def test_session_last_seen_is_not_updated_within_5_minutes() -> None:
    """前回から5分未満では `last_seen_at` を更新しないこと。"""
    clock = FakeClock(START)
    session = _session(clock)
    clock.advance(LAST_SEEN_INTERVAL - timedelta(microseconds=1))
    assert session.touch(clock.now()) is False
    assert session.last_seen_at == START


def test_session_last_seen_is_updated_after_5_minutes() -> None:
    """前回から5分以上経過した時だけ `last_seen_at` を更新すること。

    更新の基準は直前の `last_seen_at` であり、作成時ではない。
    """
    clock = FakeClock(START)
    session = _session(clock)
    clock.advance(LAST_SEEN_INTERVAL)
    assert session.touch(clock.now()) is True
    assert session.last_seen_at == START + LAST_SEEN_INTERVAL
    clock.advance(LAST_SEEN_INTERVAL - timedelta(minutes=1))
    assert session.touch(clock.now()) is False
    assert session.last_seen_at == START + LAST_SEEN_INTERVAL
    clock.advance(timedelta(minutes=1))
    assert session.touch(clock.now()) is True
    assert session.last_seen_at == START + 2 * LAST_SEEN_INTERVAL


def test_session_touch_is_rejected_when_session_is_not_valid() -> None:
    """失効済み・期限切れのSessionの `last_seen_at` は更新しないこと。

    touch は「有効な認証済みリクエストで」だけ更新できる（§4 時刻の意味）。
    """
    clock = FakeClock(START)
    revoked = _session(clock)
    revoked.revoke(clock.now())
    clock.advance(timedelta(days=1))
    assert revoked.touch(clock.now()) is False
    assert revoked.last_seen_at == START

    fresh_clock = FakeClock(START)
    expired = _session(fresh_clock)
    fresh_clock.advance(SESSION_TTL)
    assert expired.is_valid(fresh_clock.now()) is False
    assert expired.touch(fresh_clock.now()) is False
    assert expired.last_seen_at == START


def test_session_revoke_sets_revoked_at_only_once() -> None:
    """`revoked_at` は未失効なら一度だけ設定できること。"""
    clock = FakeClock(START)
    session = _session(clock)
    session.revoke(clock.now())
    with pytest.raises(ValueError):
        session.revoke(clock.now())
    assert session.revoked_at == START


def test_session_expiry_does_not_set_revoked_at() -> None:
    """期限到来は失効（`revoked_at` の設定）を伴わないこと。

    期限切れは `is_valid` の判定だけで表し、`revoked_at` は
    ログアウト・パスワード変更・回復・異常失効の時だけ設定する（§4）。
    """
    clock = FakeClock(START)
    session = _session(clock)
    clock.advance(SESSION_TTL + timedelta(days=1))
    assert session.is_valid(clock.now()) is False
    assert session.revoked_at is None


def test_login_credential_issue_sets_password_changed_at() -> None:
    """初期発行時に `password_changed_at` が設定されること。

    初期発行は「通常変更時・回復成功時」と並ぶ設定タイミングの一つ（§4 時刻の意味）。
    """
    clock = FakeClock(START)
    cred = _login_credential(clock)
    assert cred.created_at == START
    assert cred.updated_at == START
    assert cred.password_changed_at == START


def test_login_credential_change_password_updates_change_and_updated_at() -> None:
    """通常のパスワード変更成功時に `password_changed_at` と `updated_at` が
    変わること。"""
    clock = FakeClock(START)
    cred = _login_credential(clock)
    clock.advance(timedelta(days=1))
    cred.change_password(NEW_PASSWORD_HASH, clock.now())
    assert cred.password_hash == NEW_PASSWORD_HASH
    assert cred.password_changed_at == START + timedelta(days=1)
    assert cred.updated_at == START + timedelta(days=1)


def test_login_credential_reissue_on_recovery_updates_timestamps() -> None:
    """回復成功時に新 `login_id`・新パスワードが発行され、`password_changed_at` と
    `updated_at` が設定されること。"""
    clock = FakeClock(START)
    cred = _login_credential(clock)
    clock.advance(timedelta(days=2))
    cred.reissue(NEW_LOGIN_ID_HASH, NEW_PASSWORD_HASH, clock.now())
    assert cred.login_id_hash == NEW_LOGIN_ID_HASH
    assert cred.password_hash == NEW_PASSWORD_HASH
    assert cred.password_changed_at == START + timedelta(days=2)
    assert cred.updated_at == START + timedelta(days=2)


def test_recovery_credential_issue_and_rotate() -> None:
    """`recovery_key` の差し替え（パスワード変更・回復の成功時）だけが
    `updated_at` を更新すること。

    `recovery_credentials` は1ユーザー1行で、作成・パスワード変更・回復の
    成功時に同じ行を差し替える（design/08-database.md §9 設計メモ）。
    """
    clock = FakeClock(START)
    cred = RecoveryCredential.issue(
        user_id=USER_ID, recovery_key_hash=RECOVERY_KEY_HASH, now=clock.now()
    )
    assert cred.created_at == START
    assert cred.updated_at == START
    clock.advance(timedelta(days=1))
    cred.rotate(NEW_RECOVERY_KEY_HASH, clock.now())
    assert cred.recovery_key_hash == NEW_RECOVERY_KEY_HASH
    assert cred.created_at == START
    assert cred.updated_at == START + timedelta(days=1)


def test_user_has_no_mutable_attributes() -> None:
    """User に永続的な可変属性は無く、`updated_at` は作成時から変わらないこと。

    `updated_at` は「永続的な可変属性が実際に変わった成功時だけ」更新する
    （§4 時刻の意味）。User の可変属性は存在しない。
    """
    assert {field.name for field in dataclasses.fields(User)} == {
        "id",
        "created_at",
        "updated_at",
    }
