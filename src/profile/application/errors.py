"""Profile application 層のエラー（design/06-errors.md §7）。

HTTP 層はこの例外の `code` を `build_error_body` に渡すだけで、内部理由や入力値を
外へ出さない。非公開・存在しない Profile は同一の `PROFILE_NOT_FOUND` に揃える
（H-1 / H-2）。
"""

from src.shared.errors.errors import ErrorCode
from src.shared.web.web import FieldError


class ProfileApplicationError(Exception):
    """外部へ返す `code` を持つ application エラーの基底。"""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class ProfileNotFoundError(ProfileApplicationError):
    """H-1 / H-2: Profile なし・非公開（区別しない）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.PROFILE_NOT_FOUND)


class ProfileNotPublishableError(ProfileApplicationError):
    """H-15: `content` が空のまま publish しようとした。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.PROFILE_NOT_PUBLISHABLE)


class ProfileValidationError(ProfileApplicationError):
    """H-10: Profile 入力規則違反（`PATCH /me/profile` の本文）。

    `fields` は違反した項目を `field` と `code` だけで運ぶ（入力値は持たない）。
    エラーハンドラが 422 `VALIDATION_ERROR` の `fields` として描画する
    （design/05-api-contract.md §6 / design/06-errors.md §7）。
    """

    def __init__(self, fields: tuple[FieldError, ...] = ()) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR)
        self.fields = fields
