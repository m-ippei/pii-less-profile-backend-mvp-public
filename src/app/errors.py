"""app 層（Context 跨ぎユースケース）のエラー（design/06-errors.md §7）。

HTTP 層はこの例外の `code` を `build_error_body` に渡すだけで、内部理由や入力値を
外へ出さない。一意制約衝突（H-8）は再試行で吸収し、尽きたときだけ汎用の 500 にする。
"""

from src.shared.errors.errors import ErrorCode


class AppError(Exception):
    """外部へ返す `code` を持つ app エラーの基底。"""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class ProfileCreationFailedError(AppError):
    """H-8: 一意制約衝突が最大回数続いた。

    衝突の詳細（どの UNIQUE か等）は外へ出さず、汎用の 500 `INTERNAL_ERROR` としてだけ
    扱う（design/06-errors.md §7 H-8 / H-9）。
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR)
