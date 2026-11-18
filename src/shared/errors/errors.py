"""共有エラー定数（`design/06-errors.md` §7 の正本の写し）。

以後のすべての層がエラーを返すとき、この定数表以外から文言を返さない。
入力値・内部理由・秘密値は含めない（`design/14-dod-conventions.md` §16）。
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """外部へ露出する code 14種（`design/06-errors.md` §7 の code 列）。"""

    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_SESSION = "INVALID_SESSION"
    INVALID_RECOVERY_CREDENTIAL = "INVALID_RECOVERY_CREDENTIAL"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CSRF_FAILED = "CSRF_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_CURRENT_PASSWORD = "INVALID_CURRENT_PASSWORD"
    PROFILE_NOT_PUBLISHABLE = "PROFILE_NOT_PUBLISHABLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"


ERROR_MESSAGES: dict[str, str] = {
    "PROFILE_NOT_FOUND": "プロフィールが見つかりません",
    "INVALID_CREDENTIALS": "ログインIDまたはパスワードが正しくありません",
    "INVALID_SESSION": "セッションが無効です",
    "INVALID_RECOVERY_CREDENTIAL": "回復用キーが正しくありません",
    "INTERNAL_ERROR": "内部エラーが発生しました",
    "VALIDATION_ERROR": "入力内容に誤りがあります",
    "CSRF_FAILED": "セキュリティチェックに失敗しました",
    "RATE_LIMITED": "リクエストが多すぎます。しばらくしてから再試行してください",
    "INVALID_CURRENT_PASSWORD": "現在のパスワードが正しくありません",
    "PROFILE_NOT_PUBLISHABLE": "公開できる内容がありません",
    "PAYLOAD_TOO_LARGE": "リクエスト本文が大きすぎます",
    "SERVICE_UNAVAILABLE": "サービスを一時的に利用できません",
    "NOT_FOUND": "リソースが見つかりません",
    "METHOD_NOT_ALLOWED": "許可されていないメソッドです",
}

ERROR_STATUS: dict[str, int] = {
    "PROFILE_NOT_FOUND": 404,
    "INVALID_CREDENTIALS": 401,
    "INVALID_SESSION": 401,
    "INVALID_RECOVERY_CREDENTIAL": 400,
    "INTERNAL_ERROR": 500,
    "VALIDATION_ERROR": 422,
    "CSRF_FAILED": 403,
    "RATE_LIMITED": 429,
    "INVALID_CURRENT_PASSWORD": 403,
    "PROFILE_NOT_PUBLISHABLE": 422,
    "PAYLOAD_TOO_LARGE": 413,
    "SERVICE_UNAVAILABLE": 503,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
}

# H-1〜H-18 → 外部用 code。H-8 は外部へ露出しないので `None`。
# H-18 だけは 404/405 の2 code を持つ（未知ルート・メソッド不一致で使い分ける）。
HANDLER_ID: dict[str, str | tuple[str, str] | None] = {
    "H-1": "PROFILE_NOT_FOUND",
    "H-2": "PROFILE_NOT_FOUND",
    "H-3": "INVALID_CREDENTIALS",
    "H-4": "INVALID_CREDENTIALS",
    "H-5": "INVALID_SESSION",
    "H-6": "INVALID_SESSION",
    "H-7": "INVALID_RECOVERY_CREDENTIAL",
    "H-8": None,
    "H-9": "INTERNAL_ERROR",
    "H-10": "VALIDATION_ERROR",
    "H-11": "INVALID_SESSION",
    "H-12": "CSRF_FAILED",
    "H-13": "RATE_LIMITED",
    "H-14": "INVALID_CURRENT_PASSWORD",
    "H-15": "PROFILE_NOT_PUBLISHABLE",
    "H-16": "PAYLOAD_TOO_LARGE",
    "H-17": "SERVICE_UNAVAILABLE",
    "H-18": ("NOT_FOUND", "METHOD_NOT_ALLOWED"),
}


def message_for(code: ErrorCode | str) -> str:
    """code に対応する日本語固定文言を返す。未知の code は `KeyError` で落とす。"""
    key = code.value if isinstance(code, ErrorCode) else code
    return ERROR_MESSAGES[key]


def status_for(code: ErrorCode | str) -> int:
    """code に対応する HTTP status を返す。未知の code は `KeyError` で落とす。"""
    key = code.value if isinstance(code, ErrorCode) else code
    return ERROR_STATUS[key]
