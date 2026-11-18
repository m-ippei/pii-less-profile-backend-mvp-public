"""shared/web パッケージの公開 API。"""

from src.shared.web.web import (
    ErrorDetail,
    ErrorResponse,
    FieldError,
    FieldErrorBody,
    Principal,
    build_error_body,
)

__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "FieldError",
    "FieldErrorBody",
    "Principal",
    "build_error_body",
]
