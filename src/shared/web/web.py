"""共有 Web 部品（`design/02-architecture.md` §3、`design/05-api-contract.md` §6）。

- `Principal`: Context 間で渡す認証済み主体。`user_id` / `method` / `scopes` の3項目だけを持ち、
  Profile 側へは `user_id` だけを渡す。
- `FieldError` / `build_error_body`: エラー応答の本文を `design/06-errors.md` §7 の固定文言
  だけから組み立てる。入力値・内部理由・秘密値を含めない。

FastAPI には依存しない。`JSONResponse` へ包むのは presentation 層の責務。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from src.shared.errors.errors import ErrorCode, message_for


class FieldErrorBody(TypedDict):
    """422 応答の `fields` の1要素（`design/05-api-contract.md` §6）。値は持たない。"""

    field: str
    code: str


class ErrorDetail(TypedDict):
    """エラー応答の `error` オブジェクト。`fields` は 422 のときだけ現れる。"""

    code: str
    message: str
    fields: NotRequired[list[FieldErrorBody]]


class ErrorResponse(TypedDict):
    """エラー応答の本文 `{"error":{...}}`（`design/05-api-contract.md` §6）。"""

    error: ErrorDetail


@dataclass(frozen=True)
class Principal:
    """認証済みの主体（`design/02-architecture.md` §3）。

    Identity が作り、Profile 側は `user_id` だけを受け取る。生成後は変更できない。
    `scopes` は将来の認可（`authorization`）用で、MVP では空でもよい。
    """

    user_id: str
    method: str
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldError:
    """422 応答の `fields` の1要素。値を持てず、`field` と `code` だけを持つ。"""

    field: str
    code: str


def _field_body(item: FieldError) -> FieldErrorBody:
    """`FieldError` を応答用の `field` / `code` だけの辞書へ写す（値を含めない）。"""
    return {"field": item.field, "code": item.code}


def build_error_body(
    code: ErrorCode | str,
    fields: Iterable[FieldError] | None = None,
) -> ErrorResponse:
    """エラー応答の本文を `{"error":{"code","message"}}` の形で組み立てる。

    `message` は `design/06-errors.md` §7 の固定文言表からのみ取る。表に無い code は
    `KeyError` で落ちる。`fields` を渡した場合は `field` と `code` だけの配列を足し、
    入力値は写さない。
    """
    key = code.value if isinstance(code, ErrorCode) else code
    error: ErrorDetail = {"code": key, "message": message_for(key)}
    field_list: list[FieldErrorBody] = []
    if fields is not None:
        field_list = [_field_body(item) for item in fields]
    if field_list:
        error["fields"] = field_list
    return {"error": error}
