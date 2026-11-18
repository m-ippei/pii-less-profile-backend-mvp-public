"""Identity ドメインの Entity / VO のテスト（design/03-domain-model.md §4）。

このファイルが守る約束は 2 点。

1. VO は形式規則を持ち、違反を拒否する。生の秘密値は Entity / VO に保持しない。
2. `identity/domain` は標準ライブラリのみ import する
   （design/02-architecture.md §3 の依存方向）。
"""

from datetime import UTC, datetime

import pytest

from src.identity.domain.entities import (
    LoginCredential,
    RecoveryCredential,
    Session,
    User,
)
from src.identity.domain.value_objects import (
    LoginId,
    Password,
    RecoveryKey,
    SessionToken,
    UserId,
)
from src.shared.security.security import PasswordHasher

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _valid_login_id() -> str:
    return "1" * 24


def _valid_recovery_key() -> str:
    return "1" * 32


def _valid_session_token() -> str:
    return "1" * 32


def test_user_id_accepts_uuidv7() -> None:
    value = UserId("018f3c2a-7b1c-7abc-8def-0123456789ab")
    assert str(value) == "018f3c2a-7b1c-7abc-8def-0123456789ab"


def test_user_id_rejects_non_uuid() -> None:
    with pytest.raises(ValueError):
        UserId("not-a-uuid")
    with pytest.raises(ValueError):
        UserId("018f3c2a-7b1c-6abc-8def-0123456789ab")  # version 6 は不可


def test_login_id_format_is_enforced() -> None:
    assert LoginId(_valid_login_id()).value == _valid_login_id()
    with pytest.raises(ValueError):
        LoginId("1" * 23)  # 短い
    with pytest.raises(ValueError):
        LoginId("0" * 24)  # Base58 に '0' は含まれない
    with pytest.raises(ValueError):
        LoginId("1" * 25)  # 長い


def test_recovery_key_format_is_enforced() -> None:
    assert RecoveryKey(_valid_recovery_key()).value == _valid_recovery_key()
    with pytest.raises(ValueError):
        RecoveryKey("1" * 31)  # 短い
    with pytest.raises(ValueError):
        RecoveryKey("0" * 32)  # Base58 に '0' は含まれない
    with pytest.raises(ValueError):
        RecoveryKey("1" * 33)  # 長い


def test_session_token_format_is_enforced() -> None:
    assert SessionToken(_valid_session_token()).value == _valid_session_token()
    with pytest.raises(ValueError):
        SessionToken("1" * 31)
    with pytest.raises(ValueError):
        SessionToken("0" * 32)


def test_password_is_normalized_and_length_checked() -> None:
    assert Password("a" * 12).value == "a" * 12
    assert Password("ａ" * 12).value == "a" * 12  # NFKC で半角に
    with pytest.raises(ValueError):
        Password("a" * 11)  # 短すぎ
    with pytest.raises(ValueError):
        Password("a" * 129)  # 長すぎ


def test_user_holds_id_and_timestamps() -> None:
    user = User(id=UserId("018f3c2a-7b1c-7abc-8def-0123456789ab"), created_at=NOW, updated_at=NOW)
    assert user.created_at == NOW
    assert user.updated_at == NOW


def test_login_credential_holds_hashes_only() -> None:
    hashed = PasswordHasher().hash("a" * 12)
    cred = LoginCredential(
        user_id=UserId("018f3c2a-7b1c-7abc-8def-0123456789ab"),
        login_id_hash="ab" * 32,
        password_hash=hashed,
        password_changed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    assert cred.password_hash.startswith("$argon2")
    assert not hasattr(cred, "password")


def test_recovery_credential_holds_hash_and_timestamps() -> None:
    cred = RecoveryCredential(
        user_id=UserId("018f3c2a-7b1c-7abc-8def-0123456789ab"),
        recovery_key_hash="cd" * 32,
        created_at=NOW,
        updated_at=NOW,
    )
    assert cred.recovery_key_hash == "cd" * 32
    assert not hasattr(cred, "recovery_key")


def test_session_holds_expected_fields() -> None:
    session = Session(
        id=UserId("018f3c2a-7b1c-7abc-8def-0123456789ab"),
        user_id=UserId("018f3c2a-7b1c-7abc-8def-0123456789ab"),
        token_hash="ef" * 32,
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW,
        revoked_at=None,
    )
    assert session.revoked_at is None
    assert session.token_hash == "ef" * 32


def test_domain_imports_standard_library_only() -> None:
    """`src/identity/domain` の全モジュールが標準ライブラリのみ import する。

    `design/02-architecture.md` §3「domain は標準ライブラリのみ」のテスト。
    直接の import 文だけでなく、同パッケージ外（src/）も拒否する。
    """
    import ast
    from pathlib import Path

    domain = Path("src/identity/domain")
    assert domain.is_dir()
    has_module = False
    for path in domain.rglob("*.py"):
        has_module = True
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("src"), f"{path}: {name}"
                banned = {"fastapi", "sqlite3", "argon2", "pydantic"}
                assert name.split(".")[0] not in banned, f"{path}: {name}"
    assert has_module
