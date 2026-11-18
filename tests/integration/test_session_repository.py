"""`SqlSessionRepository` のテスト（一時ディレクトリの実 SQLite ファイル）。

このファイルが守るべき設計上の約束は次の6点。

1. `save` した Session が `find_by_token_hash` で列欠落なく復元される
   （design/03-domain-model.md §4 Entity `Session` / Port 表）。`revoked_at` が
   NULL の行も保存・取得できる。
2. 該当なしは `None`。0行や例外を「不在」として返さない。
3. `touch` は前回から5分未満なら更新せず、5分以上なら `last_seen_at` が更新される
   （design/03-domain-model.md §4 時刻の意味）。
4. 失効済み・期限切れの Session は `touch` しても更新されない（`Session.is_valid`）。
5. `revoke_all_for_user` は対象 user の未失効 Session だけを失効し、他 user と
   失効済みには触れない（design/08-database.md §9 全Session失効）。
6. 時刻は27文字の固定長ISO 8601で保存し、マイクロ秒まで含めて aware datetime として
   読み戻せる（design/08-database.md §9 共通規約）。

根拠: design/03-domain-model.md §4 / design/08-database.md §9 / design/11-testing.md §12。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.identity.domain.entities import SESSION_TTL, Session
from src.identity.domain.ports import SessionRepository
from src.identity.domain.value_objects import UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.identity.infrastructure.session_repository import SqlSessionRepository
from src.shared.clock.clock import format_timestamp
from src.shared.db import UnitOfWork, connect
from tests.support.sqlite_identity import fake_hash, make_account, make_session, migrated_db_path

USER_ID = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
OTHER_USER_ID = UserId("018f3c2a-7b1c-7abd-8def-0123456789ac")
SESSION_ID = UserId("018f3c2a-7b1c-7abe-8def-0123456789ad")
OTHER_SESSION_ID = UserId("018f3c2a-7b1c-7abf-8def-0123456789ae")
THIRD_SESSION_ID = UserId("018f3c2a-7b1c-7ac0-8def-0123456789af")

TOKEN_HASH = fake_hash("session:token")
OTHER_TOKEN_HASH = fake_hash("session:other_token")
THIRD_TOKEN_HASH = fake_hash("session:third_token")

# マイクロ秒が丸められる formatter を見逃さないよう、意図的に 0 以外を使う。
MOMENT = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=UTC)
ALMOST_FIVE_MINUTES_LATER = MOMENT + timedelta(minutes=5, seconds=-1)
FIVE_MINUTES_LATER = MOMENT + timedelta(minutes=5)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return migrated_db_path(tmp_path)


def seed_user(db_path: Path, user_id: UserId) -> None:
    """`sessions.user_id` の FK に必要な `users` 行を用意する。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlAccountRepository(uow.connection).save(make_account(user_id=user_id, now=MOMENT))


def save(db_path: Path, session: Session) -> None:
    """ユースケースと同じ形で `UnitOfWork` の中で保存する。"""
    with UnitOfWork(connect(db_path)) as uow:
        SqlSessionRepository(uow.connection).save(session)


def find_by_token_hash(db_path: Path, token_hash: str) -> Session | None:
    with UnitOfWork(connect(db_path)) as uow:
        return SqlSessionRepository(uow.connection).find_by_token_hash(token_hash)


def touch(db_path: Path, session: Session, now: datetime) -> None:
    with UnitOfWork(connect(db_path)) as uow:
        SqlSessionRepository(uow.connection).touch(session, now)


def revoke_all_for_user(db_path: Path, user_id: UserId, now: datetime) -> None:
    with UnitOfWork(connect(db_path)) as uow:
        SqlSessionRepository(uow.connection).revoke_all_for_user(user_id, now)


def read_raw(db_path: Path, session_id: UserId) -> dict[str, str | None]:
    """Entity を経由せずに生の列を読む（DB へ実際に書かれた値の観測用）。"""
    reader = connect(db_path)
    try:
        row = reader.execute(
            "SELECT created_at, last_seen_at, expires_at, revoked_at FROM sessions WHERE id = ?",
            (session_id.value,),
        ).fetchone()
        assert row is not None
        return {key: row[key] for key in row.keys()}
    finally:
        reader.close()


def make_active_session(session_id: UserId, token_hash: str) -> Session:
    return make_session(id=session_id, user_id=USER_ID, token_hash=token_hash, now=MOMENT)


def test_save_and_find_by_token_hash_restore_every_column(db_path: Path) -> None:
    """新規 Session が保存され、全列が欠落なく Entity として読み戻せること。

    `Session` は id / user_id / token_hash / created_at / last_seen_at / expires_at /
    revoked_at の7列（design/03-domain-model.md §4）。どれか1列でも欠けると認証経路が
    `is_valid` を誤判定する。`revoked_at` が NULL の行も保存・取得できる。
    """
    seed_user(db_path, USER_ID)
    session = make_active_session(SESSION_ID, TOKEN_HASH)
    save(db_path, session)

    loaded = find_by_token_hash(db_path, TOKEN_HASH)

    assert loaded is not None
    assert loaded.id == SESSION_ID
    assert loaded.user_id == USER_ID
    assert loaded.token_hash == TOKEN_HASH
    assert loaded.created_at == MOMENT
    assert loaded.last_seen_at == MOMENT
    assert loaded.expires_at == MOMENT + SESSION_TTL
    assert loaded.revoked_at is None
    assert loaded.last_seen_at.tzinfo is not None
    assert loaded.last_seen_at.microsecond == MOMENT.microsecond


def test_find_by_token_hash_returns_none_when_nothing_matches(db_path: Path) -> None:
    """該当なしは `None`。0行や例外を「不在」として返さないこと。

    `AuthenticateSession` は `None` を「認証失敗」の分岐に使うので、例外にすると
    誤って 500 になる。SQL断片も値として扱われ、実行されない。
    """
    assert find_by_token_hash(db_path, fake_hash("nothing")) is None
    assert find_by_token_hash(db_path, "' OR '1'='1") is None


def test_save_restores_a_revoked_session_with_revoked_at(db_path: Path) -> None:
    """失効済みの Session も `revoked_at` を保持したまま保存・取得できること。

    ログアウト（`DeleteCurrentSession`）は `revoke` して `save` するだけなので、
    `revoked_at` が書き戻らないと失効が恒久化されない。
    """
    seed_user(db_path, USER_ID)
    session = make_active_session(SESSION_ID, TOKEN_HASH)
    session.revoke(MOMENT + timedelta(minutes=1))
    save(db_path, session)

    loaded = find_by_token_hash(db_path, TOKEN_HASH)

    assert loaded is not None
    assert loaded.revoked_at == MOMENT + timedelta(minutes=1)
    assert loaded.revoked_at is not None


def test_touch_updates_only_after_five_minutes(db_path: Path) -> None:
    """5分未満は DB・Entity とも更新せず、ちょうど5分で両方が更新されること。

    `last_seen_at` は有効な認証済みリクエストで前回から5分以上経過した時だけ更新する
    （design/03-domain-model.md §4 時刻の意味 / design/08-database.md §9 touch）。
    毎リクエスト書き換えると書き込みが増えるだけなので、境界は固定長文字列比較で判定する。
    """
    seed_user(db_path, USER_ID)
    session = make_active_session(SESSION_ID, TOKEN_HASH)
    save(db_path, session)

    touch(db_path, session, ALMOST_FIVE_MINUTES_LATER)

    assert read_raw(db_path, SESSION_ID)["last_seen_at"] == format_timestamp(MOMENT)
    assert session.last_seen_at == MOMENT

    touch(db_path, session, FIVE_MINUTES_LATER)

    assert read_raw(db_path, SESSION_ID)["last_seen_at"] == format_timestamp(FIVE_MINUTES_LATER)
    assert session.last_seen_at == FIVE_MINUTES_LATER


def test_touch_does_not_update_a_revoked_session(db_path: Path) -> None:
    """失効済みの Session は `touch` しても `last_seen_at` が変わらないこと。

    有効性はドメイン（`Session.is_valid`）で判定する（design/08-database.md §9
    「有効性はドメインで判定」）。失効済みを復活させると、ログアウト後も
    セッションが生き続けたかのように見える。
    """
    seed_user(db_path, USER_ID)
    session = make_active_session(SESSION_ID, TOKEN_HASH)
    session.revoke(MOMENT + timedelta(hours=1))
    save(db_path, session)

    touch(db_path, session, MOMENT + timedelta(hours=2))

    assert read_raw(db_path, SESSION_ID)["last_seen_at"] == format_timestamp(MOMENT)
    assert session.last_seen_at == MOMENT


def test_touch_does_not_update_an_expired_session(db_path: Path) -> None:
    """期限切れの Session は `touch` しても `last_seen_at` が変わらないこと。

    `expires_at` は作成時+14日の絶対期限で延長しない（design/03-domain-model.md
    §4 Domain rule）。期限切れの更新も有効化と同等の誤動作になるため弾く。
    """
    seed_user(db_path, USER_ID)
    session = make_active_session(SESSION_ID, TOKEN_HASH)
    save(db_path, session)

    touch(db_path, session, MOMENT + SESSION_TTL + timedelta(days=1))

    assert read_raw(db_path, SESSION_ID)["last_seen_at"] == format_timestamp(MOMENT)
    assert session.last_seen_at == MOMENT


def test_revoke_all_for_user_only_revokes_active_sessions_of_that_user(db_path: Path) -> None:
    """対象 user の未失効 Session だけが `revoked_at` を受け、他は不変であること。

    `UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL`
    （design/08-database.md §9 全Session失効）。他 user を巻き込むと第三者が
    ログアウトさせられ、失効済みの `revoked_at` を上書きすると失効時刻の証跡が消える。
    """
    seed_user(db_path, USER_ID)
    seed_user(db_path, OTHER_USER_ID)
    active = make_active_session(SESSION_ID, TOKEN_HASH)
    already_revoked = make_active_session(OTHER_SESSION_ID, OTHER_TOKEN_HASH)
    already_revoked.revoke(MOMENT + timedelta(hours=1))
    other_users = make_session(
        id=THIRD_SESSION_ID, user_id=OTHER_USER_ID, token_hash=THIRD_TOKEN_HASH, now=MOMENT
    )
    save(db_path, active)
    save(db_path, already_revoked)
    save(db_path, other_users)

    revoke_at = MOMENT + timedelta(hours=2)
    revoke_all_for_user(db_path, USER_ID, revoke_at)

    loaded_active = find_by_token_hash(db_path, TOKEN_HASH)
    assert loaded_active is not None
    assert loaded_active.revoked_at == revoke_at
    assert loaded_active.last_seen_at == MOMENT
    loaded_revoked = find_by_token_hash(db_path, OTHER_TOKEN_HASH)
    assert loaded_revoked is not None
    assert loaded_revoked.revoked_at == MOMENT + timedelta(hours=1)
    loaded_other = find_by_token_hash(db_path, THIRD_TOKEN_HASH)
    assert loaded_other is not None
    assert loaded_other.revoked_at is None


def test_timestamps_are_stored_as_27_characters_and_round_trip(db_path: Path) -> None:
    """時刻が27文字の固定長ISO 8601で保存され、マイクロ秒まで一致すること。

    固定長なので文字列比較が時刻順と一致する（design/08-database.md §9 共通規約）。
    `touch` の閾値判定と `expires_at` 比較はこの文字列比較に依存する。
    """
    seed_user(db_path, USER_ID)
    save(db_path, make_active_session(SESSION_ID, TOKEN_HASH))

    raw = read_raw(db_path, SESSION_ID)

    for text in (raw["created_at"], raw["last_seen_at"], raw["expires_at"]):
        assert text is not None
        assert len(text) == 27
        assert text.endswith("Z")
    assert raw["created_at"] == format_timestamp(MOMENT)
    assert raw["last_seen_at"] == format_timestamp(MOMENT)
    assert raw["expires_at"] == format_timestamp(MOMENT + SESSION_TTL)
    assert raw["revoked_at"] is None


def test_repository_satisfies_the_session_repository_port() -> None:
    """`design/03-domain-model.md` §4 の Port を満たしていること。

    Port は `runtime_checkable` なのでメソッドの形を検証できる。欠けがあっても
    M03 の application テスト（InMemory ダブル）では気づけない。
    """
    repository = SqlSessionRepository(sqlite3.connect(":memory:"))

    assert isinstance(repository, SessionRepository)
