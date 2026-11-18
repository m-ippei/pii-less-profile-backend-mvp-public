"""Identity の秘密値入力ヘルパーのテスト（design/05-api-contract.md §6 /
design/07-security.md §8 / design/06-errors.md §7）。

T-08-02 の終了条件を純ロジック（テストダブル不要）で観測する。

- `login_id` / `recovery_key` は「最初の `_` より前」を抽出してから検証する
- 長さ・文字種・password 長の違反は、固定文言の 422 `VALIDATION_ERROR` の `fields`
  （`field` と `code` だけ）になり、入力値を echo しない
"""

import json

import pytest

from src.identity.application.errors import ValidationError
from src.identity.presentation.inputs import (
    extract_base58,
    parse_change,
    parse_login,
    parse_recovery,
)
from src.shared.errors.errors import ErrorCode, status_for
from src.shared.web.web import ErrorResponse, build_error_body

VALID_LOGIN = "1" * 24
VALID_RECOVERY = "1" * 32
MIN_NEW_PASSWORD = "abcdefghijkl"  # 12 コードポイント


def _body(exc: ValidationError) -> ErrorResponse:
    """`ValidationError` が独自エラーハンドラへ載る形（422 本文）を組み立てる。"""
    return build_error_body(exc.code, exc.fields)


def _fields(exc: ValidationError) -> list[tuple[str, str]]:
    return [(field.field, field.code) for field in exc.fields]


def test_extract_base58_returns_part_before_first_underscore() -> None:
    """`extract_base58` が「最初の `_` より前」を返すこと（`_` 無しは全体、空もあり得る）。"""
    assert extract_base58(f"{VALID_LOGIN}_my-label") == VALID_LOGIN
    assert extract_base58(f"{VALID_RECOVERY}_RECOVERY-KEY-my-label") == VALID_RECOVERY
    assert extract_base58(VALID_LOGIN) == VALID_LOGIN
    assert extract_base58("a_b_c") == "a"
    assert extract_base58("_label") == ""


def test_parse_login_extracts_base58_and_ignores_suffix() -> None:
    """`login_id` の接尾の表示識別文字を落とし、Base58 24文字だけを VO にすること。"""
    login_id, password = parse_login(f"{VALID_LOGIN}_MY-LABEL", "pw")
    assert login_id.value == VALID_LOGIN
    assert password == "pw"


def test_parse_recovery_extracts_base58_and_ignores_suffix() -> None:
    """`recovery_key` の接尾を落とし、Base58 32文字だけを VO にすること。"""
    recovery_key, new_password = parse_recovery(
        f"{VALID_RECOVERY}_RECOVERY-KEY-my-label", MIN_NEW_PASSWORD
    )
    assert recovery_key.value == VALID_RECOVERY
    assert new_password == MIN_NEW_PASSWORD


def test_parse_login_rejects_length_and_charset_violations() -> None:
    """`login_id` の長さ・文字種違反が `too_short` / `invalid` で報告されること。"""
    with pytest.raises(ValidationError) as short:
        parse_login("1" * 23, "pw")
    assert _fields(short.value) == [("login_id", "too_short")]

    with pytest.raises(ValidationError) as long:
        parse_login("1" * 25, "pw")
    assert _fields(long.value) == [("login_id", "too_long")]

    with pytest.raises(ValidationError) as invalid:
        parse_login("0" * 24, "pw")  # `0` は Base58 に含まれない
    assert _fields(invalid.value) == [("login_id", "invalid")]


def test_parse_login_rejects_password_outside_login_bounds() -> None:
    """ログイン `password` が 1〜128 の範囲外なら `too_short` / `too_long` になること。"""
    with pytest.raises(ValidationError) as empty:
        parse_login(VALID_LOGIN, "")
    assert _fields(empty.value) == [("password", "too_short")]

    with pytest.raises(ValidationError) as over:
        parse_login(VALID_LOGIN, "x" * 129)
    assert _fields(over.value) == [("password", "too_long")]

    accepted = parse_login(VALID_LOGIN, "x" * 128)
    assert accepted[1] == "x" * 128


def test_parse_recovery_rejects_new_password_outside_bounds() -> None:
    """`new_password` が 12〜128 の範囲外なら `too_short` / `too_long` になること。"""
    with pytest.raises(ValidationError) as short:
        parse_recovery(VALID_RECOVERY, "x" * 11)
    assert _fields(short.value) == [("new_password", "too_short")]

    with pytest.raises(ValidationError) as long:
        parse_recovery(VALID_RECOVERY, "x" * 129)
    assert _fields(long.value) == [("new_password", "too_long")]

    accepted = parse_recovery(VALID_RECOVERY, "x" * 12)
    assert accepted[1] == "x" * 12


def test_parse_change_validates_current_and_new_password() -> None:
    """変更入力の現・新パスワードをそれぞれ検証し、違反順に報告すること。"""
    with pytest.raises(ValidationError) as exc:
        parse_change("", "short")
    assert _fields(exc.value) == [("current_password", "too_short"), ("new_password", "too_short")]

    current, new = parse_change("x" * 128, "x" * 12)
    assert (current, new) == ("x" * 128, "x" * 12)


def test_password_length_is_measured_after_nfkc_normalization() -> None:
    """長さ判定が NFKC 正規化後のコードポイントで行われること（design/07-security.md §8）。"""
    # `㍿`（1文字）は NFKC で `株式会社`（4文字）に展開される。3文字で12コードポイント。
    recovery_key, normalized = parse_recovery(VALID_RECOVERY, "\u337f" * 3)
    assert recovery_key.value == VALID_RECOVERY
    assert normalized == "株式会社" * 3
    assert len(normalized) == 12


def test_multiple_violations_are_collected_after_extraction() -> None:
    """抽出後の複数の違反を、検査順の一覧として一度に報告すること。"""
    with pytest.raises(ValidationError) as exc:
        parse_login("bad_login_id", "")  # 抽出すると `bad`（3文字）
    assert _fields(exc.value) == [("login_id", "too_short"), ("password", "too_short")]


def test_violation_renders_as_422_without_echoing_input_values() -> None:
    """違反が固定文言の 422 `VALIDATION_ERROR` になり、入力値を echo しないこと。"""
    secret_login = "sUpErSeCrEt_should-not-leak"
    secret_password = "another-secret-password"

    with pytest.raises(ValidationError) as exc:
        parse_login(secret_login, secret_password)

    body = _body(exc.value)
    assert status_for(exc.value.code) == 422
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert body == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "入力内容に誤りがあります",
            "fields": [{"field": "login_id", "code": "too_short"}],
        }
    }

    serialized = json.dumps(body)
    assert secret_login not in serialized
    assert secret_password not in serialized
