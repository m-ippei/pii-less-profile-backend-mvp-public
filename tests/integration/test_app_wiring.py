"""`src/main.py` の結線を実 SQLite で観測する結合テスト（design/02-architecture.md §3）。

- `build_create_profile()` が Context 跨ぎ `CreateProfile` を実ポートで組み立て、Profile・
  Session・資格情報を実際に永続化できること（design/04-use-cases.md §5）
- Identity が提供する `PrincipalResolver` / `require_principal` が Session cookie から
  `Principal` を解決し、欠落・不正 token では `InvalidSessionError` になること
  （design/07-security.md §8 認証フロー）
- 解決した `user_id` だけを Profile 側ユースケースへ渡せること（Context 境界。§3）

根拠: design/02-architecture.md §3 / design/04-use-cases.md §5 / design/07-security.md §8 /
design/10-infra-ops.md §11。
"""

import base64
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from src.identity.application.errors import InvalidSessionError
from src.identity.presentation.auth import PrincipalResolver
from src.main import (
    SESSION_COOKIE,
    build_create_profile,
    build_identity_dependencies,
    build_profile_dependencies,
    require_principal,
)
from src.profile.application.usecases import GetMyProfile
from src.shared.db import connect
from tests.support.sqlite_identity import migrated_db_path

PEPPER = b"test-pepper-" + b"x" * 32
ENV_PEPPER = base64.urlsafe_b64encode(PEPPER).decode()


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """実ポートが読む環境変数を、migration 適用済みの一時DBへ向ける。"""
    path = migrated_db_path(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("APP_PEPPER", ENV_PEPPER)
    # 実 Argon2id のままテスト用に軽量パラメータへ落とす。
    monkeypatch.setenv("ARGON2_TIME_COST", "1")
    monkeypatch.setenv("ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("ARGON2_PARALLELISM", "1")
    return path


def _request_with_cookie(cookie: str | None) -> Request:
    """指定の Cookie ヘッダーを持つ最小の ASGI Request を組み立てる。"""
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope)


def _user_id_for_public_id(db_path: Path, public_id: str) -> str:
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT user_id FROM profiles WHERE public_id = ?", (public_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row["user_id"])


def test_create_profile_wiring_persists_all_aggregates(db_path: Path) -> None:
    """`build_create_profile()` が生の秘密値を返し、DB には Profile が1行できること。"""
    created = build_create_profile().execute()

    assert created.public_id.startswith("p_") and len(created.public_id) == 18
    assert len(created.login_id) == 24
    assert len(created.initial_password) == 16
    assert len(created.recovery_key) == 32
    assert len(created.session_token) == 32
    assert _user_id_for_public_id(db_path, created.public_id)


def test_valid_session_cookie_resolves_principal(db_path: Path) -> None:
    """有効な Session cookie から `Principal(user_id, ...)` が解決できること。"""
    created = build_create_profile().execute()
    user_id = _user_id_for_public_id(db_path, created.public_id)

    principal = require_principal(_request_with_cookie(f"{SESSION_COOKIE}={created.session_token}"))

    assert principal.user_id == user_id
    assert principal.method == "password"
    assert principal.scopes == ()


def test_missing_or_invalid_session_cookie_is_rejected(db_path: Path) -> None:
    """Cookie 欠落・不明 token では `InvalidSessionError` になり `Principal` を返さない。"""
    build_create_profile().execute()

    with pytest.raises(InvalidSessionError):
        require_principal(_request_with_cookie(None))

    with pytest.raises(InvalidSessionError):
        require_principal(_request_with_cookie(f"{SESSION_COOKIE}={'2' * 32}"))


def test_principal_resolver_is_the_identity_provided_callable(db_path: Path) -> None:
    """`PrincipalResolver` が token から `Principal` を作る callable であること。"""
    created = build_create_profile().execute()
    resolver = PrincipalResolver(build_identity_dependencies)

    principal = resolver(created.session_token)

    assert principal.user_id == _user_id_for_public_id(db_path, created.public_id)
    with pytest.raises(InvalidSessionError):
        resolver(None)


def test_resolved_user_id_is_all_profile_side_needs(db_path: Path) -> None:
    """解決した `user_id` だけを Profile ユースケースへ渡して本人 Profile を取れること。"""
    created = build_create_profile().execute()
    principal = require_principal(_request_with_cookie(f"{SESSION_COOKIE}={created.session_token}"))

    mine = GetMyProfile(build_profile_dependencies()).execute(user_id=principal.user_id)

    assert mine.public_id == created.public_id
    assert mine.status == "draft"
