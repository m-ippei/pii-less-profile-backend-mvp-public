"""`shared/security` のテスト。

このファイルが守るべき設計上の約束は次の点。いずれも
`design/07-security.md` §8「秘密値と保存」と
`design/03-domain-model.md` §4「Port」表、
`design/10-infra-ops.md` §11「設定」に由来する。

1. Base58 は Bitcoin alphabet（`0` `O` `I` `l` を含まない）。
   生成値の接頭辞・長さは設計書どおり。
2. `SecretGenerator` が CSPRNG（`secrets.token_bytes`）を使い、
   各秘密値を正しい形式で返す。
3. `IdGenerator` が `uuid.uuid7()` の 36 文字文字列を返す
   （Python 3.14 標準）。
4. `SecretHasher` が `HMAC-SHA256(key=pepper,
   msg=label + ":" + value)` の HEX64（64文字）を返し、
   同じ入力は毎回同じ値、ラベルまたは pepper が変われば値が変わる。
5. `PasswordHasher` が Argon2id（time=3, memory=65536,
   parallelism=4 を既定に、環境変数で上書き可）で
   hash / verify / dummy_verify を提供し、
   `dummy_verify` は処理時間を揃えるためハッシュ探索をしない。
6. `load_app_pepper` が base64url デコードし、32 バイト未満なら
   `ValueError`。
7. 実装は定数表以外から文言を返さない。入力値をエラー応答に
   echo しない（`design/14-dod-conventions.md` §16）。
"""

import base64
import hashlib
import hmac
import os
import uuid

import pytest

from src.shared.security import (
    Base58Alphabet,
    IdGenerator,
    PasswordHasher,
    SecretGenerator,
    SecretHasher,
    load_app_pepper,
)

# テスト用の固定 pepper（32バイトの base64url。"0" * 32 のエンコード）
TEST_PEPPER_B64 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_PEPPER = b"\x00" * 32

# 設計書の生成規則に基づく期待値
EXPECTED_SPECS = {
    "public_id": {"prefix": "p_", "data_len": 16, "total_len": 18},
    "login_id": {"prefix": "", "data_len": 24, "total_len": 24},
    "recovery_key": {"prefix": "", "data_len": 32, "total_len": 32},
    "initial_password": {"prefix": "", "data_len": 16, "total_len": 16},
    "session_token": {"prefix": "", "data_len": 32, "total_len": 32},
}


class TestBase58Alphabet:
    """Base58Alphabet のテスト。"""

    def test_alphabet_excludes_0_o_i_l(self) -> None:
        """Bitcoin alphabet は `0` `O` `I` `l` を含まない。"""
        alphabet = Base58Alphabet.ALPHABET
        assert "0" not in alphabet
        assert "O" not in alphabet
        assert "I" not in alphabet
        assert "l" not in alphabet
        assert len(alphabet) == 58

    def test_encode_decode_round_trip(self) -> None:
        """エンコード・デコードの往復で元のバイト列に戻る。"""
        test_values = [
            b"",
            b"\x00",
            b"\x00\x00",
            b"hello",
            os.urandom(16),
            os.urandom(32),
        ]
        for value in test_values:
            encoded = Base58Alphabet.encode(value)
            decoded = Base58Alphabet.decode(encoded)
            assert decoded == value

    def test_encode_leading_zero_bytes(self) -> None:
        """先頭のゼロバイトがアルファベットの先頭文字（'1'）になる。"""
        # 1バイトゼロ → '1'
        assert Base58Alphabet.encode(b"\x00") == "1"
        # 2バイトゼロ → '11'
        assert Base58Alphabet.encode(b"\x00\x00") == "11"

    def test_decode_invalid_char_raises(self) -> None:
        """アルファベット外の文字を含むと `ValueError`。"""
        with pytest.raises(ValueError):
            Base58Alphabet.decode("0")  # '0' は除外されている
        with pytest.raises(ValueError):
            Base58Alphabet.decode("O")
        with pytest.raises(ValueError):
            Base58Alphabet.decode("I")
        with pytest.raises(ValueError):
            Base58Alphabet.decode("l")


class TestSecretGenerator:
    """SecretGenerator のテスト。"""

    def setup_method(self) -> None:
        self.gen = SecretGenerator()

    def test_generate_public_id(self) -> None:
        """`public_id` は `p_` ＋ Base58 16文字（計18文字）。"""
        for _ in range(10):
            pid = self.gen.public_id()
            assert pid.startswith("p_")
            assert len(pid) == 18
            assert all(c not in pid for c in "0OIl")

    def test_generate_login_id(self) -> None:
        """`login_id` は Base58 24文字（認証対象）。"""
        for _ in range(10):
            lid = self.gen.login_id()
            assert len(lid) == 24
            assert all(c not in lid for c in "0OIl")

    def test_generate_recovery_key(self) -> None:
        """`recovery_key` は Base58 32文字（認証対象）。"""
        for _ in range(10):
            rk = self.gen.recovery_key()
            assert len(rk) == 32
            assert all(c not in rk for c in "0OIl")

    def test_generate_initial_password(self) -> None:
        """初期パスワードは Base58 16文字（接頭辞なし）。"""
        for _ in range(10):
            pw = self.gen.initial_password()
            assert len(pw) == 16
            assert all(c not in pw for c in "0OIl")

    def test_generate_session_token(self) -> None:
        """session token は Base58 32文字（接頭辞なし）。"""
        for _ in range(10):
            token = self.gen.session_token()
            assert len(token) == 32
            assert all(c not in token for c in "0OIl")

    def test_generate_csrf_token(self) -> None:
        """CSRFトークンは 32バイトの base64url（43〜44文字、パディングなし）。"""
        for _ in range(10):
            token = self.gen.csrf_token()
            # base64url: 32バイト → 43文字（=なし）または44文字
            assert 43 <= len(token) <= 44
            assert "=" not in token
            assert "+" not in token
            assert "/" not in token

    def test_all_generated_values_are_unique(self) -> None:
        """同じメソッドを複数回呼んでも衝突しない（確率的だが）。"""
        values = {self.gen.public_id() for _ in range(100)}
        assert len(values) == 100
        values = {self.gen.login_id() for _ in range(100)}
        assert len(values) == 100


class TestIdGenerator:
    """IdGenerator のテスト。"""

    def test_generate_returns_uuid7_string(self) -> None:
        """UUIDv7 の 36 文字文字列を返す。"""
        gen = IdGenerator()
        for _ in range(10):
            uid = gen.generate()
            assert isinstance(uid, str)
            assert len(uid) == 36
            # UUID 形式の簡易検証（8-4-4-4-12）
            parts = uid.split("-")
            assert len(parts) == 5
            assert len(parts[0]) == 8
            assert len(parts[1]) == 4
            assert len(parts[2]) == 4
            assert len(parts[3]) == 4
            assert len(parts[4]) == 12
            # バージョン 7 の簡易チェック（13桁目が '7'）
            # uuid7 は time-based で version=7, variant=RFC4122
            # バイト列では 6番目のバイトの上位4ビットが 7
            # 文字列では 14文字目付近
            parsed = uuid.UUID(uid)
            assert parsed.version == 7


class TestSecretHasher:
    """SecretHasher のテスト。"""

    def setup_method(self) -> None:
        self.hasher = SecretHasher(TEST_PEPPER)

    def test_hash_returns_hex64(self) -> None:
        """HEX64（64文字の小文字16進数）を返す。"""
        for label in ("login_id", "session", "recovery_key", "ratelimit", "ip_daily"):
            h = self.hasher.hash(label, "test-value")
            assert isinstance(h, str)
            assert len(h) == 64
            assert all(c in "0123456789abcdef" for c in h)

    def test_same_input_same_output(self) -> None:
        """同じ入力は毎回同じ値。"""
        h1 = self.hasher.hash("login_id", "my-secret")
        h2 = self.hasher.hash("login_id", "my-secret")
        assert h1 == h2

    def test_different_label_different_hash(self) -> None:
        """ラベルが違うと同じ value でも hash が違う。"""
        h1 = self.hasher.hash("login_id", "same-value")
        h2 = self.hasher.hash("session", "same-value")
        assert h1 != h2

    def test_different_pepper_different_hash(self) -> None:
        """pepper が違うと同じ value でも hash が違う。"""
        hasher2 = SecretHasher(b"\x01" * 32)
        h1 = self.hasher.hash("login_id", "same-value")
        h2 = hasher2.hash("login_id", "same-value")
        assert h1 != h2

    def test_all_valid_labels_work(self) -> None:
        """設計書の5ラベルすべてが動作する。"""
        for label in ("login_id", "session", "recovery_key", "ratelimit", "ip_daily"):
            assert len(self.hasher.hash(label, "x")) == 64

    def test_invalid_label_raises(self) -> None:
        """`[a-z_]+` 以外の label は `ValueError`（区切りの曖昧さを防ぐ）。"""
        with pytest.raises(ValueError):
            self.hasher.hash("bad:label", "x")
        with pytest.raises(ValueError):
            self.hasher.hash("UPPER", "x")
        with pytest.raises(ValueError):
            self.hasher.hash("", "x")


class TestPasswordHasher:
    """PasswordHasher のテスト。"""

    def setup_method(self) -> None:
        # テスト用は軽量パラメータで実行（環境変数で上書き）
        os.environ["ARGON2_TIME_COST"] = "1"
        os.environ["ARGON2_MEMORY_KIB"] = "1024"
        os.environ["ARGON2_PARALLELISM"] = "1"
        self.hasher = PasswordHasher()

    def teardown_method(self) -> None:
        for k in ("ARGON2_TIME_COST", "ARGON2_MEMORY_KIB", "ARGON2_PARALLELISM"):
            os.environ.pop(k, None)

    def test_hash_then_verify_succeeds(self) -> None:
        """`hash` → `verify` が成功する。"""
        pw = "correct-password-123"
        hashed = self.hasher.hash(pw)
        assert self.hasher.verify(hashed, pw) is True

    def test_verify_wrong_password_fails(self) -> None:
        """違う文字列は失敗する。"""
        pw = "correct-password-123"
        hashed = self.hasher.hash(pw)
        assert self.hasher.verify(hashed, "wrong-password") is False

    def test_verify_wrong_hash_fails(self) -> None:
        """違うハッシュ文字列は失敗する（例外ではなく False）。"""
        pw = "correct-password-123"
        hashed = self.hasher.hash(pw)
        # 末尾を変えて無効なハッシュにする
        bad_hash = hashed[:-1] + ("a" if hashed[-1] != "a" else "b")
        assert self.hasher.verify(bad_hash, pw) is False

    def test_dummy_verify_returns_false_and_runs_once(self) -> None:
        """`dummy_verify` は1回検証を実行して False を返し、ハッシュ探索をしない。"""
        # 内部で Argon2 の verify を1回だけ呼ぶことを確認するため、
        # 実行時間の揃いとして「失敗する verify と同程度の時間がかかる」ことを
        # 観測しにくいが、返り値が False で例外を出さないことを確認する。
        result = self.hasher.dummy_verify()
        assert result is False

    def test_dummy_verify_uses_a_real_argon2_hash_with_same_params(self) -> None:
        """タイミング揃えのダミーが、実パラメータの本物の Argon2id ハッシュであること。

        不正な形式のダミー文字列では Argon2 の計算前にデコードで失敗し、処理時間が
        揃わない。ここではエンコード済み Argon2id ハッシュ（設定と同パラメータ）で
        あることを観測する。
        """
        assert self.hasher._dummy_hash.startswith("$argon2id$")
        assert "m=1024,t=1,p=1" in self.hasher._dummy_hash

    def test_hash_uses_nfkc_normalization(self) -> None:
        """パスワードは NFKC 正規化後にハッシュ化される。"""
        # 同じ文字の異なる正規化形（全角・半角数字）
        # U+FF11=全角1, U+FF12=全角2, U+FF13=全角3
        pw1 = "password\uff11\uff12\uff13"  # 全角数字
        pw2 = "password123"  # 半角数字
        # NFKC 正規化後は同じになるため、いずれかでハッシュ化したものを
        # もう一方でも検証できる
        h1 = self.hasher.hash(pw1)
        assert self.hasher.verify(h1, pw2) is True
        h2 = self.hasher.hash(pw2)
        assert self.hasher.verify(h2, pw1) is True


class TestLoadAppPepper:
    """load_app_pepper のテスト。"""

    def test_valid_32_bytes(self) -> None:
        """32バイトの base64url は正常にデコードされる。"""
        # 32バイトの base64url（パディングなし）
        pepper = load_app_pepper(TEST_PEPPER_B64)
        assert isinstance(pepper, bytes)
        assert len(pepper) == 32

    def test_valid_more_than_32_bytes(self) -> None:
        """32バイト以上の base64url は正常にデコードされる。"""
        # 64バイトの base64url
        pepper_b64 = "A" * 86  # base64url で 64バイト ≈ 86文字
        pepper = load_app_pepper(pepper_b64)
        assert len(pepper) == 64

    def test_less_than_32_bytes_raises(self) -> None:
        """32バイト未満の値は `ValueError`。"""
        # 16バイトの base64url（≈22文字）
        with pytest.raises(ValueError):
            load_app_pepper("A" * 22)

    def test_invalid_base64url_raises(self) -> None:
        """不正な base64url は `ValueError`（標準の base64 デコードエラー）。"""
        with pytest.raises(ValueError):
            load_app_pepper("not-base64url!")

    def test_standard_base64_plus_slash_rejected(self) -> None:
        """標準 Base64 の `+` `/` は受理しない（base64url のみ）。"""
        standard = base64.b64encode(b"\xfb" * 32).decode()
        assert "/" in standard or "+" in standard
        with pytest.raises(ValueError):
            load_app_pepper(standard)

    def test_embedded_whitespace_rejected(self) -> None:
        """値の途中に空白・改行が混入したら黙って捨てず `ValueError`。"""
        with pytest.raises(ValueError):
            load_app_pepper("A" * 20 + " " + "A" * 23)


class TestIntegrationWithDesignSpecs:
    """設計書の仕様と整合することを確認する統合的テスト。"""

    def test_secret_generator_specs_match_design(self) -> None:
        """生成仕様が `design/07-security.md` §8 の表と一致する。"""
        gen = SecretGenerator()
        for _ in range(5):
            spec = EXPECTED_SPECS["public_id"]
            v = gen.public_id()
            assert v.startswith(spec["prefix"])
            assert len(v) == spec["total_len"]

            spec = EXPECTED_SPECS["login_id"]
            v = gen.login_id()
            assert v.startswith(spec["prefix"])
            assert len(v) == spec["total_len"]

            spec = EXPECTED_SPECS["recovery_key"]
            v = gen.recovery_key()
            assert v.startswith(spec["prefix"])
            assert len(v) == spec["total_len"]

            spec = EXPECTED_SPECS["initial_password"]
            v = gen.initial_password()
            assert len(v) == spec["total_len"]

            spec = EXPECTED_SPECS["session_token"]
            v = gen.session_token()
            assert len(v) == spec["total_len"]

    def test_secret_hasher_hmac_matches_design(self) -> None:
        """HMAC 計算が `design/07-security.md` §8 の規則どおり。"""
        # 既知の入力で手計算可能な値を用いる
        pepper = b"x" * 32
        hasher = SecretHasher(pepper)
        # label="login_id", value="test"
        # HMAC-SHA256(pepper=b"x"*32, msg=b"login_id:test")
        expected = hmac.new(pepper, b"login_id:test", hashlib.sha256).hexdigest()
        assert hasher.hash("login_id", "test") == expected

    def test_argon2_params_from_env(self) -> None:
        """環境変数で Argon2 パラメータを上書きできる。"""
        os.environ["ARGON2_TIME_COST"] = "2"
        os.environ["ARGON2_MEMORY_KIB"] = "2048"
        os.environ["ARGON2_PARALLELISM"] = "2"
        try:
            hasher = PasswordHasher()
            # パラメータが反映されているかは内部実装依存だが、
            # 少なくともインスタンス化は成功し、hash/verify が動く
            h = hasher.hash("test")
            assert hasher.verify(h, "test")
        finally:
            for k in ("ARGON2_TIME_COST", "ARGON2_MEMORY_KIB", "ARGON2_PARALLELISM"):
                os.environ.pop(k, None)

    def test_weak_argon2_params_rejected_in_prod(self) -> None:
        """`ENV=prod` では OWASP 推奨相当を下回るパラメータを拒否する。"""
        old_env = os.environ.get("ENV")
        os.environ["ENV"] = "prod"
        os.environ["ARGON2_TIME_COST"] = "1"
        os.environ["ARGON2_MEMORY_KIB"] = "8192"
        os.environ["ARGON2_PARALLELISM"] = "1"
        try:
            with pytest.raises(ValueError):
                PasswordHasher()
        finally:
            self._restore_env(old_env)

    def test_weak_argon2_params_rejected_when_env_unset(self) -> None:
        """`ENV` 未設定では fail-closed で弱いパラメータを拒否する。"""
        old_env = os.environ.get("ENV")
        os.environ.pop("ENV", None)
        os.environ["ARGON2_TIME_COST"] = "1"
        os.environ["ARGON2_MEMORY_KIB"] = "8192"
        os.environ["ARGON2_PARALLELISM"] = "1"
        try:
            with pytest.raises(ValueError):
                PasswordHasher()
        finally:
            self._restore_env(old_env)

    def test_weak_argon2_params_rejected_for_unknown_env(self) -> None:
        """`ENV=production` のような表記ゆれも fail-closed で拒否する。"""
        old_env = os.environ.get("ENV")
        os.environ["ENV"] = "production"
        os.environ["ARGON2_TIME_COST"] = "1"
        os.environ["ARGON2_MEMORY_KIB"] = "8192"
        os.environ["ARGON2_PARALLELISM"] = "1"
        try:
            with pytest.raises(ValueError):
                PasswordHasher()
        finally:
            self._restore_env(old_env)

    def test_strong_argon2_params_accepted_in_prod(self) -> None:
        """`ENV=prod` でも推奨相当のパラメータなら動作する。"""
        old_env = os.environ.get("ENV")
        os.environ["ENV"] = "prod"
        os.environ["ARGON2_TIME_COST"] = "2"
        os.environ["ARGON2_MEMORY_KIB"] = "19456"
        os.environ["ARGON2_PARALLELISM"] = "1"
        try:
            hasher = PasswordHasher()
            h = hasher.hash("test")
            assert hasher.verify(h, "test")
        finally:
            self._restore_env(old_env)

    @staticmethod
    def _restore_env(old_env: str | None) -> None:
        if old_env is None:
            os.environ.pop("ENV", None)
        else:
            os.environ["ENV"] = old_env
        for k in ("ARGON2_TIME_COST", "ARGON2_MEMORY_KIB", "ARGON2_PARALLELISM"):
            os.environ.pop(k, None)


# `design/03-domain-model.md` §4「Value Object」の固定長を確認するテスト
class TestValueObjectLengths:
    """VO の固定長が設計書と一致すること。"""

    def test_lengths_match_domain_model(self) -> None:
        gen = SecretGenerator()
        # PublicProfileId: p_ + 16 = 18
        assert len(gen.public_id()) == 18
        # LoginId: 24
        assert len(gen.login_id()) == 24
        # RecoveryKey: 32
        assert len(gen.recovery_key()) == 32
        # SessionToken: 32
        assert len(gen.session_token()) == 32
        # 初期パスワード: 16（VOにはないが生成値として固定）
        assert len(gen.initial_password()) == 16
        # UserId / ProfileId: UUIDv7 36文字
        assert len(IdGenerator().generate()) == 36
