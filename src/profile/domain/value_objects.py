"""Profile の Value Object（design/03-domain-model.md §4 Value Object）。"""

import re
import unicodedata
import uuid
from enum import StrEnum

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")
MAX_CONTENT_CODE_POINTS = 6000


class ProfileId:
    """UUIDv7・36文字。外部非公開。"""

    def __init__(self, value: str) -> None:
        try:
            parsed = uuid.UUID(value)
        except ValueError as e:
            raise ValueError("ProfileId must be a UUID") from e
        if parsed.version != 7 or len(value) != 36:
            raise ValueError("ProfileId must be a UUIDv7 in 36-character form")
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProfileId) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


class PublicProfileId:
    """`p_`＋Base58固定長16文字（計18文字）。"""

    def __init__(self, value: str) -> None:
        if not value.startswith("p_") or len(value) != 18 or not _BASE58_RE.match(value[2:]):
            raise ValueError("PublicProfileId must be 'p_' + 16 Base58 chars")
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicProfileId) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


class ProfileContent:
    """プロフィール本文の Value Object（design/01-overview.md §2 プロフィール本文）。

    規則: NFC 正規化 → CRLF/LF への改行正規化 → 前後の空白除去 →
    6,000コードポイント以内 → `\\n` `\\t` 以外の制御文字不可。
    空白のみは `""`。正規化後の値を `value` として保持する。
    """

    def __init__(self, value: str) -> None:
        normalized = unicodedata.normalize("NFC", value)
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.strip()
        if len(normalized) > MAX_CONTENT_CODE_POINTS:
            raise ValueError("ProfileContent must be at most 6000 code points")
        for char in normalized:
            if unicodedata.category(char) == "Cc" and char not in ("\n", "\t"):
                raise ValueError("ProfileContent cannot contain control characters")
        self._value = normalized

    @property
    def value(self) -> str:
        return self._value

    @property
    def is_empty(self) -> bool:
        return self._value == ""

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProfileContent) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


class ProfileStatus(StrEnum):
    """公開状態。`draft -> published -> draft` のみ（design/01-overview.md §2）。"""

    DRAFT = "draft"
    PUBLISHED = "published"
