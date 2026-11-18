"""Identity application 層のエラー（design/06-errors.md §7）。

HTTP 層はこの例外の `code` を `build_error_body` に渡すだけで、内部理由や入力値を
外へ出さない。認証系の失敗はすべて `INVALID_*` の同一応答に揃える。
"""

from src.shared.errors.errors import ErrorCode
from src.shared.web.web import FieldError


class IdentityError(Exception):
    """外部へ返す `code` を持つ application エラーの基底。"""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class InvalidCredentialsError(IdentityError):
    """H-3 / H-4: `login_id` 不明または password 誤り（同一応答）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_CREDENTIALS)


class InvalidSessionError(IdentityError):
    """H-5 / H-6 / H-11: Session 期限切れ・失効済み・不明 token。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_SESSION)


class InvalidRecoveryCredentialError(IdentityError):
    """H-7: `recovery_key` 不正または該当アカウントなし。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_RECOVERY_CREDENTIAL)


class InvalidCurrentPasswordError(IdentityError):
    """H-14: `/me/password` の現パスワード誤り。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_CURRENT_PASSWORD)


class ValidationError(IdentityError):
    """H-10: 入力規則違反（例: 形式違反、新パスワードが現パスワードと同一）。

    `fields` は違反した項目を `field` と `code` だけで運ぶ（入力値は持たない）。
    エラーハンドラが 422 `VALIDATION_ERROR` の `fields` として描画する
    （design/05-api-contract.md §6 / design/06-errors.md §7）。
    """

    def __init__(self, fields: tuple[FieldError, ...] = ()) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR)
        self.fields = fields
