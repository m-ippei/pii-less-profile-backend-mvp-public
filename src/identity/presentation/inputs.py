"""Identity の秘密値入力の抽出・検証ヘルパー（design/05-api-contract.md §6 /
design/07-security.md §8）。

ログイン（`CreateSession`）と回復（`RecoverPassword`）で共通の前処理を1箇所にまとめる。

- `login_id` / `recovery_key` は「最初の `_` より前」を認証対象とする。接尾の表示識別
  文字を落としてから形式検証・HMAC・rate limit key 計算へ渡す（抽出を後回しにすると、
  同一 Base58 に別 suffix を付けて account 単位のレート制限を回避できる）。
- 違反は `FieldError(field, code)` の一覧にして `ValidationError` に載せる。M07 の
  エラーハンドラが 422 `VALIDATION_ERROR` の `fields` として描画する。入力値は保持・
  echo しない（design/05-api-contract.md §6 / design/14-dod-conventions.md §16）。
- FastAPI に依存しない純ロジック。`run_request_pipeline` の `validate` 段で呼ぶ前提で、
  認証（401）より後に評価される。
"""

import unicodedata

from src.identity.application.errors import ValidationError
from src.identity.domain.value_objects import LoginId, RecoveryKey
from src.shared.web.web import FieldError

LOGIN_ID_LENGTH = 24
RECOVERY_KEY_LENGTH = 32
LOGIN_PASSWORD_MIN = 1
LOGIN_PASSWORD_MAX = 128
NEW_PASSWORD_MIN = 12
NEW_PASSWORD_MAX = 128

_TOO_SHORT = "too_short"
_TOO_LONG = "too_long"
_INVALID = "invalid"


def extract_base58(value: str) -> str:
    """値の「最初の `_` より前」を返す（`_` が無ければ全体）。空の結果もあり得る。"""
    return value.split("_", 1)[0]


def parse_login(login_id: str, password: str) -> tuple[LoginId, str]:
    """ログイン入力を抽出・検証し、`LoginId` と NFKC 正規化済み password を返す。

    違反があれば `ValidationError`（422 `VALIDATION_ERROR`）を送出する。
    """
    base58 = extract_base58(login_id)
    normalized_password = _normalize(password)
    errors = _collect(
        _base58_error("login_id", base58, LOGIN_ID_LENGTH, LoginId),
        _password_error("password", normalized_password, LOGIN_PASSWORD_MIN, LOGIN_PASSWORD_MAX),
    )
    if errors:
        raise ValidationError(tuple(errors))
    return LoginId(base58), normalized_password


def parse_recovery(recovery_key: str, new_password: str) -> tuple[RecoveryKey, str]:
    """回復入力を抽出・検証し、`RecoveryKey` と NFKC 正規化済み new_password を返す。"""
    base58 = extract_base58(recovery_key)
    normalized_password = _normalize(new_password)
    errors = _collect(
        _base58_error("recovery_key", base58, RECOVERY_KEY_LENGTH, RecoveryKey),
        _password_error("new_password", normalized_password, NEW_PASSWORD_MIN, NEW_PASSWORD_MAX),
    )
    if errors:
        raise ValidationError(tuple(errors))
    return RecoveryKey(base58), normalized_password


def parse_change(current_password: str, new_password: str) -> tuple[str, str]:
    """パスワード変更入力を検証し、NFKC 正規化済みの現・新パスワードを返す。"""
    normalized_current = _normalize(current_password)
    normalized_new = _normalize(new_password)
    errors = _collect(
        _password_error(
            "current_password", normalized_current, LOGIN_PASSWORD_MIN, LOGIN_PASSWORD_MAX
        ),
        _password_error("new_password", normalized_new, NEW_PASSWORD_MIN, NEW_PASSWORD_MAX),
    )
    if errors:
        raise ValidationError(tuple(errors))
    return normalized_current, normalized_new


def _normalize(value: str) -> str:
    """NFKC 正規化（ハッシュ・長さ判定は正規化後のコードポイントで行う）。"""
    return unicodedata.normalize("NFKC", value)


def _base58_error(
    field: str,
    value: str,
    length: int,
    vo_type: type[LoginId] | type[RecoveryKey],
) -> FieldError | None:
    """Base58 固定長の違反を `field` と `code` だけで表す。問題なければ `None`。"""
    size = len(value)
    if size != length:
        return FieldError(field=field, code=_TOO_SHORT if size < length else _TOO_LONG)
    try:
        vo_type(value)
    except ValueError:
        return FieldError(field=field, code=_INVALID)
    return None


def _password_error(field: str, value: str, minimum: int, maximum: int) -> FieldError | None:
    """パスワード長の違反を `field` と `code` だけで表す。問題なければ `None`。"""
    size = len(value)
    if size < minimum:
        return FieldError(field=field, code=_TOO_SHORT)
    if size > maximum:
        return FieldError(field=field, code=_TOO_LONG)
    return None


def _collect(*errors: FieldError | None) -> list[FieldError]:
    """`None` を除いた違反一覧を、検査した順のまま返す。"""
    return [error for error in errors if error is not None]
