"""Profile の更新入力の検証ヘルパー（design/05-api-contract.md §6）。

`PATCH /me/profile` の本文を検証し、`ProfileValidationError`（422 `VALIDATION_ERROR`）を
送出する。`identity/presentation/inputs.py` と同じく FastAPI に依存しない純ロジックで、
`run_request_pipeline` の `validate` 段で認証（401）より後に評価される。

- 1項目以上必須（`content` / `status` の少なくとも一方が指定される）
- 指定された項目が `null` は 422（未指定＝変更なしとは区別する）
- `content` は `ProfileContent` の規則（6,000コードポイント以内・制御文字不可）
- `status` は `draft` / `published` のみ

入力値は保持・echo しない（design/05-api-contract.md §6 / design/14-dod-conventions.md §16）。
"""

from src.profile.application.errors import ProfileValidationError
from src.profile.domain.value_objects import ProfileContent, ProfileStatus
from src.shared.web.web import FieldError

_INVALID = "invalid"


def parse_update(
    *,
    content_provided: bool,
    content: str | None,
    status_provided: bool,
    status: str | None,
) -> tuple[ProfileContent | None, ProfileStatus | None]:
    """`PATCH /me/profile` の本文を検証し、更新後の `content` / `status` を返す。

    未指定の項目は `None`（変更なし）。1項目も指定が無い場合、指定された項目が `null` の
    場合、値が規則に違反する場合は `ProfileValidationError` を送出する。
    """
    if not content_provided and not status_provided:
        raise ProfileValidationError()

    errors: list[FieldError] = []
    parsed_content: ProfileContent | None = None
    if content_provided:
        if content is None:
            errors.append(FieldError(field="content", code=_INVALID))
        else:
            parsed_content = _parse_content(content, errors)

    parsed_status: ProfileStatus | None = None
    if status_provided:
        if status is None:
            errors.append(FieldError(field="status", code=_INVALID))
        else:
            parsed_status = _parse_status(status, errors)

    if errors:
        raise ProfileValidationError(tuple(errors))
    return parsed_content, parsed_status


def _parse_content(value: str, errors: list[FieldError]) -> ProfileContent | None:
    """`ProfileContent` の規則に通れば返す。違反は `content` の `field`/`code` だけを足す。"""
    try:
        return ProfileContent(value)
    except ValueError:
        errors.append(FieldError(field="content", code=_INVALID))
        return None


def _parse_status(value: str, errors: list[FieldError]) -> ProfileStatus | None:
    """`draft` / `published` のみを受理する。違反は `status` の `field`/`code` を足す。"""
    try:
        return ProfileStatus(value)
    except ValueError:
        errors.append(FieldError(field="status", code=_INVALID))
        return None
