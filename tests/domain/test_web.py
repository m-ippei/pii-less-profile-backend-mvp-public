"""`shared/web` のテスト。

このファイルが守るべき設計上の約束は次の5点。いずれも `design/02-architecture.md` §3、
`design/05-api-contract.md` §6、`design/06-errors.md` §7、`design/14-dod-conventions.md`
§16 に由来する。

1. `Principal` は Context 間の受け渡しの型で、`user_id` / `method` / `scopes` の3項目だけを持ち、
   生成後に変更できない。Profile 側へは `user_id` だけを渡す。
2. エラー応答の本文は `{"error":{"code","message"}}` の形で、`message` は設計書の固定文言のみ。
   入力値・内部理由・秘密値を含めない。
3. `fields` は 422 のときだけ現れ、`field` と `code` だけで、入力値を持たない。
4. 表に無い code は `KeyError` で落とし、定数表以外から文言を返す経路を作らない。
5. `shared/web` は FastAPI に依存しない（`JSONResponse` への梱包は presentation 層の責務）。
"""

from dataclasses import FrozenInstanceError, fields

import pytest

from src.shared.errors.errors import ERROR_MESSAGES, ErrorCode
from src.shared.web.web import FieldError, Principal, build_error_body


def test_principal_has_exactly_user_id_method_scopes() -> None:
    """`Principal` が `user_id` / `method` / `scopes` の3項目だけを持つこと。

    Context 間で渡してよい情報を3項目に固定する。ここが増えると、Identity の内部事情が
    Profile 側へ漏れる経路になる（`design/02-architecture.md` §3）。
    """
    assert [field.name for field in fields(Principal)] == ["user_id", "method", "scopes"]


def test_principal_holds_the_given_values() -> None:
    """`Principal` が渡された `user_id` / `method` / `scopes` を保持すること。"""
    principal = Principal(user_id="u_0001", method="session", scopes=("profile:read",))
    assert principal.user_id == "u_0001"
    assert principal.method == "session"
    assert principal.scopes == ("profile:read",)


def test_principal_is_immutable() -> None:
    """`Principal` を生成後に書き換えられないこと。

    認証済みの主体を表す値なので、途中で `user_id` や `method` がすり替わると
    認可判断の前提が崩れる。
    """
    principal = Principal(user_id="u_0001", method="session", scopes=())
    with pytest.raises(FrozenInstanceError):
        principal.user_id = "u_0002"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        principal.method = "api"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        principal.scopes = ("profile:write",)  # type: ignore[misc]


def test_field_error_holds_field_and_code_only() -> None:
    """`FieldError` が `field` と `code` だけを持ち、値を持てないこと。

    入力値をエラー応答へ echo しないための型の保証（`design/05-api-contract.md` §6）。
    """
    assert [field.name for field in fields(FieldError)] == ["field", "code"]
    error = FieldError(field="new_password", code="too_short")
    assert (error.field, error.code) == ("new_password", "too_short")


def test_build_error_body_returns_code_and_fixed_message() -> None:
    """`build_error_body` が `{"error":{"code","message"}}` を返すこと。

    `message` は設計書の固定文言表の値と一致しなければならない
    （`design/06-errors.md` §7）。言い換え・翻訳・追加をしない。
    """
    body = build_error_body(ErrorCode.PROFILE_NOT_FOUND)
    assert body == {
        "error": {
            "code": "PROFILE_NOT_FOUND",
            "message": ERROR_MESSAGES["PROFILE_NOT_FOUND"],
        }
    }


def test_build_error_body_accepts_a_plain_code_string() -> None:
    """`ErrorCode` だけでなく code 文字列も受理すること。

    呼び出し側が定数表の code をそのまま渡せるようにする。
    """
    body = build_error_body("INVALID_SESSION")
    assert body["error"]["code"] == "INVALID_SESSION"
    assert body["error"]["message"] == ERROR_MESSAGES["INVALID_SESSION"]


def test_build_error_body_omits_fields_when_not_passed() -> None:
    """`fields` を渡さない応答に `fields` キーが現れないこと。

    422 以外のエラー（401 / 403 / 404 など）は `fields` を持たない。
    """
    assert "fields" not in build_error_body(ErrorCode.INVALID_CREDENTIALS)["error"]


def test_build_error_body_omits_fields_when_empty() -> None:
    """空の `fields` では `fields` キーを出さないこと。

    field 単位のエラーが無い 422（例: 公開不可）で `"fields":[]` を出さない。
    """
    assert "fields" not in build_error_body(ErrorCode.PROFILE_NOT_PUBLISHABLE, fields=())["error"]


def test_build_error_body_renders_fields_without_input_values() -> None:
    """`fields` が `field` と `code` だけの配列になり、入力値を含まないこと。

    値を持てない `FieldError` だけを受け取り、その2項目だけを写す。
    """
    error = build_error_body(
        ErrorCode.VALIDATION_ERROR,
        fields=(FieldError(field="new_password", code="too_short"),),
    )["error"]
    assert "fields" in error
    assert error["fields"] == [{"field": "new_password", "code": "too_short"}]
    assert all(set(entry) == {"field", "code"} for entry in error["fields"])


def test_build_error_body_preserves_field_order() -> None:
    """複数の `fields` を渡された順に並べること。

    クライアントが先頭のエラーから順に表示できる。
    """
    error = build_error_body(
        ErrorCode.VALIDATION_ERROR,
        fields=(
            FieldError(field="content", code="too_long"),
            FieldError(field="status", code="invalid"),
        ),
    )["error"]
    assert "fields" in error
    assert error["fields"] == [
        {"field": "content", "code": "too_long"},
        {"field": "status", "code": "invalid"},
    ]


def test_build_error_body_raises_key_error_for_unknown_code() -> None:
    """表に無い code で `KeyError` を投げること。

    未知の code を応答へ流す経路を作らないためのガード。`None` も黙って通さない。
    """
    with pytest.raises(KeyError):
        build_error_body("UNKNOWN_CODE")
    with pytest.raises(KeyError):
        build_error_body("")
