"""Identity の Value Object（design/03-domain-model.md §4 Value Object）。"""

import re
import unicodedata
import uuid

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


class UserId:
    """UUIDv7・36文字。外部非公開。"""

    def __init__(self, value: str) -> None:
        try:
            parsed = uuid.UUID(value)
        except ValueError as e:
            raise ValueError("UserId must be a UUID") from e
        if parsed.version != 7 or len(value) != 36:
            raise ValueError("UserId must be a UUIDv7 in 36-character form")
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UserId) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


class LoginId:
    """Base58固定長24文字（認証対象）。

    表示では `_` 以降に表示識別文字を付与できるが、認証対象は Base58 固定長部のみ。
    """

    def __init__(self, value: str) -> None:
        if len(value) != 24 or not _BASE58_RE.match(value):
            raise ValueError("LoginId must be 24 Base58 chars")
        self._value = value

    @property
    def value(self) -> str:
        return self._value


class RecoveryKey:
    """Base58固定長32文字（認証対象）。

    表示では `_RECOVERY-KEY`＋表示識別文字を付与できるが、認証対象は Base58 固定長部のみ。
    """

    def __init__(self, value: str) -> None:
        if len(value) != 32 or not _BASE58_RE.match(value):
            raise ValueError("RecoveryKey must be 32 Base58 chars")
        self._value = value

    @property
    def value(self) -> str:
        return self._value


class SessionToken:
    """Base58固定長32文字。"""

    def __init__(self, value: str) -> None:
        if len(value) != 32 or not _BASE58_RE.match(value):
            raise ValueError("SessionToken must be 32 Base58 chars")
        self._value = value

    @property
    def value(self) -> str:
        return self._value


class Password:
    """NFKC正規化後12〜128コードポイント。"""

    def __init__(self, value: str) -> None:
        normalized = unicodedata.normalize("NFKC", value)
        if not (12 <= len(normalized) <= 128):
            raise ValueError("Password must be 12-128 code points after NFKC")
        self._value = normalized

    @property
    def value(self) -> str:
        return self._value
