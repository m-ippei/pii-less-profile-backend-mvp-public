"""Profile ドメインの Entity / VO のテスト（design/03-domain-model.md §4）。

守る約束は 3 点。

1. `ProfileContent` は design/01-overview.md §2 の正規化規則を満たす
   （6,000コードポイント上限・空は `""`。AC-10）。
2. `Profile` は state 遷移（draft -> published -> draft）と no-op の規則を守る。
3. `profile/domain` は標準ライブラリのみ import する（design/02-architecture.md §3）。
"""

from datetime import UTC, datetime

import pytest

from src.profile.domain.entities import Profile
from src.profile.domain.errors import (
    InvalidProfileTransitionError,
    ProfileNotPublishableError,
)
from src.profile.domain.value_objects import (
    ProfileContent,
    ProfileId,
    ProfileStatus,
    PublicProfileId,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)
LATER = datetime(2026, 10, 6, 1, tzinfo=UTC)


def _public_id() -> PublicProfileId:
    return PublicProfileId("p_" + "1" * 16)


def _profile(content: str = "本文") -> Profile:
    return Profile.create(
        id=ProfileId("018f3c2a-7b1c-7abc-8def-0123456789ab"),
        user_id="018f3c2a-7b1c-7abc-8def-0123456789ab",
        public_id=_public_id(),
        content=ProfileContent(content),
        now=NOW,
    )


def test_profile_content_normalizes_unicode_and_newlines() -> None:
    # e + combining acute → NFC で合成済みの é
    assert ProfileContent("e\u0301").value == "\u00e9"
    assert ProfileContent("a\r\nb").value == "a\nb"
    assert ProfileContent("a\rb").value == "a\nb"


def test_profile_content_trims_and_whitespace_only_is_empty() -> None:
    assert ProfileContent("  hello \n").value == "hello"
    assert ProfileContent("   \n\t  ").value == ""
    assert ProfileContent("   \n\t  ").is_empty is True
    assert ProfileContent("x").is_empty is False


def test_profile_content_enforces_length_limit() -> None:
    assert len(ProfileContent("a" * 6000).value) == 6000
    with pytest.raises(ValueError):
        ProfileContent("a" * 6001)


def test_profile_content_rejects_forbidden_control_characters() -> None:
    assert ProfileContent("a\nb\tc").value == "a\nb\tc"
    with pytest.raises(ValueError):
        ProfileContent("a\x00b")
    with pytest.raises(ValueError):
        ProfileContent("a\x07b")


def test_profile_id_and_public_profile_id_formats() -> None:
    assert ProfileId("018f3c2a-7b1c-7abc-8def-0123456789ab").value.endswith("89ab")
    with pytest.raises(ValueError):
        ProfileId("018f3c2a-7b1c-6abc-8def-0123456789ab")  # version 6 は不可

    assert PublicProfileId("p_" + "1" * 16).value == "p_" + "1" * 16
    with pytest.raises(ValueError):
        PublicProfileId("p_" + "0" * 16)  # Base58 に '0' は含まれない
    with pytest.raises(ValueError):
        PublicProfileId("1" * 18)  # 接頭辞なし


def test_profile_create_is_draft_without_published_at() -> None:
    profile = _profile()
    assert profile.status is ProfileStatus.DRAFT
    assert profile.published_at is None
    assert profile.created_at == NOW
    assert profile.updated_at == NOW
    assert profile.content.value == "本文"


def test_publish_fails_on_empty_content() -> None:
    profile = _profile("   ")
    with pytest.raises(ProfileNotPublishableError):
        profile.publish(LATER)
    assert profile.status is ProfileStatus.DRAFT


def test_publish_sets_published_at_and_updates_updated_at() -> None:
    profile = _profile("本文")
    assert profile.publish(LATER) is True
    assert profile.status is ProfileStatus.PUBLISHED
    assert profile.published_at == LATER
    assert profile.updated_at == LATER


def test_publish_and_unpublish_reject_invalid_transitions() -> None:
    profile = _profile("本文")
    with pytest.raises(InvalidProfileTransitionError):
        profile.unpublish(LATER)  # draft からの unpublish は不可

    profile.publish(LATER)
    with pytest.raises(InvalidProfileTransitionError):
        profile.publish(LATER)  # published からの publish は不可


def test_unpublish_returns_to_draft_and_clears_published_at() -> None:
    profile = _profile("本文")
    profile.publish(LATER)
    assert profile.unpublish(LATER) is True
    assert profile.status is ProfileStatus.DRAFT
    assert profile.published_at is None


def test_update_while_published_changes_content() -> None:
    profile = _profile("本文")
    profile.publish(NOW)
    assert profile.update(ProfileContent("更新後"), LATER) is True
    assert profile.status is ProfileStatus.PUBLISHED
    assert profile.published_at == NOW  # 再公開ではないので published_at は不変
    assert profile.content.value == "更新後"
    assert profile.updated_at == LATER


def test_update_noop_does_not_change_updated_at() -> None:
    profile = _profile("本文")
    assert profile.update(ProfileContent("  本文  "), LATER) is False
    assert profile.updated_at == NOW


def test_domain_imports_standard_library_only() -> None:
    """`src/profile/domain` が標準ライブラリのみ import する。

    `design/02-architecture.md` §3「domain は標準ライブラリのみ」のテスト。
    同パッケージ外（`src.` 始まり）も拒否し、Context 跨ぎを検出する。
    """
    import ast
    from pathlib import Path

    domain = Path("src/profile/domain")
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
