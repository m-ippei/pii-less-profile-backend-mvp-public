"""共有セキュリティ基盤（`design/07-security.md` §8 / `design/03-domain-model.md` §4 Port）。

- `SecretGenerator`: CSPRNG で秘密値を生成（Base58 Bitcoin alphabet。`public_id` のみ `p_` 接頭辞）
- `IdGenerator`: UUIDv7（`uuid.uuid7()`）
- `SecretHasher`: ラベル付き HMAC-SHA256（HEX64）
- `PasswordHasher`: Argon2id（hash / verify / dummy_verify）
- `load_app_pepper`: base64url デコード、32バイト以上検証
"""

import base64
import binascii
import hmac
import os
import re
import secrets
import unicodedata
import uuid
from functools import lru_cache

from argon2 import PasswordHasher as Argon2Hasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError

# Bitcoin Base58 alphabet（`0` `O` `I` `l` を除く）
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_MAP = {char: idx for idx, char in enumerate(BASE58_ALPHABET)}


class Base58Alphabet:
    """Bitcoin Base58 のエンコード・デコード。"""

    ALPHABET = BASE58_ALPHABET

    @staticmethod
    def encode(data: bytes) -> str:
        """バイト列を Base58 文字列にエンコード。"""
        if not data:
            return ""
        # 先頭のゼロバイトの数を数える
        leading_zeros = len(data) - len(data.lstrip(b"\x00"))
        # 整数に変換
        num = int.from_bytes(data, "big")
        encoded = []
        while num > 0:
            num, rem = divmod(num, 58)
            encoded.append(BASE58_ALPHABET[rem])
        # 先頭ゼロを '1' で埋める
        encoded.extend(["1"] * leading_zeros)
        return "".join(reversed(encoded))

    @staticmethod
    def decode(text: str) -> bytes:
        """Base58 文字列をバイト列にデコード。不正文字は `ValueError`。"""
        if not text:
            return b""
        # 先頭の '1' の数を数える
        leading_ones = len(text) - len(text.lstrip("1"))
        # 文字ごとに値を取得し整数を復元
        num = 0
        for char in text:
            if char not in BASE58_MAP:
                raise ValueError(f"invalid base58 character: {char!r}")
            num = num * 58 + BASE58_MAP[char]
        # バイト列に変換
        byte_len = (num.bit_length() + 7) // 8
        data = num.to_bytes(byte_len, "big") if num > 0 else b""
        # 先頭ゼロを復元
        return b"\x00" * leading_ones + data


class SecretGenerator:
    """CSPRNG（`secrets`）を使って秘密値を生成。"""

    def _gen_base58(self, length: int) -> str:
        """Bitcoin Base58 の文字を1文字ずつ一様に選んで `length` 文字を返す。

        バイト列を基数変換すると先頭桁が一様にならず、切り詰めでエントロピーも落ちる。
        `secrets.choice` は棄却サンプリングで偏りなく選ぶため、58^n の一様な空間になる。
        """
        return "".join(secrets.choice(BASE58_ALPHABET) for _ in range(length))

    def public_id(self) -> str:
        """`public_id`: `p_` ＋ Base58 16文字（計18文字）。"""
        return "p_" + self._gen_base58(16)

    def login_id(self) -> str:
        """`login_id`: Base58 24文字（認証対象。表示では `_` 以降に表示識別文字を付与）。"""
        return self._gen_base58(24)

    def recovery_key(self) -> str:
        """`recovery_key`: Base58 32文字（認証対象）。"""
        return self._gen_base58(32)

    def initial_password(self) -> str:
        """初期パスワード: Base58 16文字（接頭辞なし）。"""
        return self._gen_base58(16)

    def session_token(self) -> str:
        """session token: Base58 32文字（接頭辞なし）。"""
        return self._gen_base58(32)

    def csrf_token(self) -> str:
        """CSRFトークン: 32バイトの base64url（パディングなし）。"""
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


class IdGenerator:
    """UUIDv7 生成（Python 3.14 `uuid.uuid7()`）。"""

    def generate(self) -> str:
        return str(uuid.uuid7())


class SecretHasher:
    """ラベル付き HMAC-SHA256（HEX64）。"""

    _LABEL_RE = re.compile(r"[a-z_]+")

    def __init__(self, pepper: bytes) -> None:
        if len(pepper) < 32:
            raise ValueError("pepper must be at least 32 bytes")
        self._pepper = pepper

    def hash(self, label: str, value: str) -> str:
        """`HMAC-SHA256(key=pepper, msg=label + ":" + value)` の HEX64（64文字小文字16進）。

        `label` は `[a-z_]+` に限定する。`:` を含む label を許すと `label + ":" + value` の
        区切りが曖昧になり、別の (label, value) が同じ入力になるため（§8 の5ラベルは全て適合）。
        """
        if self._LABEL_RE.fullmatch(label) is None:
            raise ValueError("label must match [a-z_]+")
        msg = f"{label}:{value}".encode()
        return hmac.digest(self._pepper, msg, "sha256").hex()


# 本番で許容する Argon2id パラメータの下限（OWASP 推奨相当）。
# 検査は fail-closed: 軽量パラメータを許すのは `ENV=test` を明示したときだけ
# （design/10-infra-ops.md §11）。それ以外（`dev`・`prod`・未設定・表記ゆれ）では強制する。
_PROD_MIN_TIME_COST = 2
_PROD_MIN_MEMORY_KIB = 19456
_RELAXED_ENVS = frozenset({"test"})


@lru_cache(maxsize=8)
def _dummy_password_hash(time_cost: int, memory_kib: int, parallelism: int) -> str:
    """タイミング揃え用の本物のダミーハッシュを、同じパラメータで1回だけ生成する。

    `PasswordHasher` はリクエストごとに生成されるため、`lru_cache` でパラメータ単位に
    共有する。不正な形式のダミー文字列では Argon2 の計算が走らず、処理時間が揃わない。
    """
    hasher = Argon2Hasher(
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        type=Type.ID,
    )
    return hasher.hash("dummy-password-for-timing")


class PasswordHasher:
    """Argon2id ハッシュ / 検証 / ダミー検証。

    パラメータは環境変数で上書き可（既定: time=3, memory=65536 KiB, parallelism=4）。
    `ENV=test` のときだけ軽量パラメータを許す（design/10-infra-ops.md §11）。
    それ以外の環境（`dev`・`prod`・未設定・本番相当を含む）では、OWASP 推奨相当を
    下回る設定を拒否する。
    """

    def __init__(self) -> None:
        self._time_cost = int(os.getenv("ARGON2_TIME_COST", "3"))
        self._memory_cost = int(os.getenv("ARGON2_MEMORY_KIB", "65536"))
        self._parallelism = int(os.getenv("ARGON2_PARALLELISM", "4"))
        self._validate_params()
        self._hasher = Argon2Hasher(
            time_cost=self._time_cost,
            memory_cost=self._memory_cost,
            parallelism=self._parallelism,
            type=Type.ID,
        )
        # 実パラメータで生成した本物のダミーハッシュ（パラメータ単位でキャッシュ）。
        self._dummy_hash = _dummy_password_hash(
            self._time_cost, self._memory_cost, self._parallelism
        )

    def _validate_params(self) -> None:
        """緩和を明示した環境以外では、弱すぎるパラメータを拒否する（fail-closed）。

        `ENV` が未設定・表記ゆれ（`production` 等）・本番相当の別名でも、下限を下回れば
        起動時に `ValueError` にする。緩めてよいのは `ENV=test` だけ。
        """
        if os.getenv("ENV") in _RELAXED_ENVS:
            return
        if self._time_cost < _PROD_MIN_TIME_COST or self._memory_cost < _PROD_MIN_MEMORY_KIB:
            raise ValueError("ARGON2 parameters are too weak")

    def hash(self, password: str) -> str:
        """パスワードを NFKC 正規化して Argon2id ハッシュを返す。"""
        normalized = self._normalize(password)
        return self._hasher.hash(normalized)

    def verify(self, hashed: str, password: str) -> bool:
        """ハッシュとパスワード（NFKC 正規化済み）を検証。"""
        normalized = self._normalize(password)
        try:
            self._hasher.verify(hashed, normalized)
            return True
        except VerificationError:
            return False

    def dummy_verify(self) -> bool:
        """ダミー検証: 実パラメータの本物のハッシュに対し1回検証し、常に False を返す。

        ハッシュ探索（ブルートフォース）は行わない。呼び出し側は `login_id` 不在時にも
        これを呼び、存在するユーザーの `verify` と同程度の処理時間にしてタイミング攻撃を防ぐ。
        """
        try:
            self._hasher.verify(self._dummy_hash, self._normalize("wrong-password"))
        except VerificationError, InvalidHashError:
            pass
        return False

    @staticmethod
    def _normalize(password: str) -> str:
        """NFKC 正規化（`design/07-security.md` §8 パスワード規則）。"""
        return unicodedata.normalize("NFKC", password)


_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")


def load_app_pepper(env_value: str) -> bytes:
    """`APP_PEPPER`（base64url）をデコードし、32バイト以上なら返す。

    base64url のアルファベット外の文字（改行・空白・`+` `/` など）や、デコード結果が
    32バイト未満の場合は `ValueError`。標準 base64 の `+` `/` は受理しない。
    """
    value = env_value.strip()
    if _B64URL_RE.fullmatch(value) is None:
        raise ValueError("APP_PEPPER must be valid base64url")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as e:
        raise ValueError("APP_PEPPER must be valid base64url") from e
    if len(decoded) < 32:
        raise ValueError("APP_PEPPER must be at least 32 bytes after decoding")
    return decoded
