"""Identity の Entity（design/03-domain-model.md §4）。

生の秘密値はここに保持しない。永続モデルが持つのはハッシュのみ。
時刻の規則は §4「時刻の意味」に従う。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .value_objects import UserId

SESSION_TTL = timedelta(days=14)
LAST_SEEN_INTERVAL = timedelta(minutes=5)


@dataclass
class User:
    id: UserId
    created_at: datetime
    updated_at: datetime


@dataclass
class LoginCredential:
    user_id: UserId
    login_id_hash: str
    password_hash: str
    password_changed_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def issue(
        cls,
        *,
        user_id: UserId,
        login_id_hash: str,
        password_hash: str,
        now: datetime,
    ) -> LoginCredential:
        """初期発行。`password_changed_at` はここで設定される。"""
        return cls(
            user_id=user_id,
            login_id_hash=login_id_hash,
            password_hash=password_hash,
            password_changed_at=now,
            created_at=now,
            updated_at=now,
        )

    def change_password(self, password_hash: str, now: datetime) -> None:
        """通常のパスワード変更成功時。

        永続的な可変属性が変わったので `password_changed_at` と
        `updated_at` を設定する（§4 時刻の意味）。
        """
        self.password_hash = password_hash
        self.password_changed_at = now
        self.updated_at = now

    def reissue(self, login_id_hash: str, password_hash: str, now: datetime) -> None:
        """回復成功時。新 `login_id` と新パスワードを発行する。

        `password_changed_at` は回復成功時にも設定される（§4 時刻の意味）。
        """
        self.login_id_hash = login_id_hash
        self.password_hash = password_hash
        self.password_changed_at = now
        self.updated_at = now


@dataclass
class RecoveryCredential:
    user_id: UserId
    recovery_key_hash: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def issue(
        cls,
        *,
        user_id: UserId,
        recovery_key_hash: str,
        now: datetime,
    ) -> RecoveryCredential:
        return cls(
            user_id=user_id,
            recovery_key_hash=recovery_key_hash,
            created_at=now,
            updated_at=now,
        )

    def rotate(self, recovery_key_hash: str, now: datetime) -> None:
        """同じ行の `recovery_key_hash` を差し替える（パスワード変更・回復の成功時）。

        可変属性が変わったので `updated_at` を設定する（§4 時刻の意味）。
        """
        self.recovery_key_hash = recovery_key_hash
        self.updated_at = now


@dataclass
class Session:
    id: UserId
    user_id: UserId
    token_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        id: UserId,
        user_id: UserId,
        token_hash: str,
        now: datetime,
    ) -> Session:
        """Session 作成。`expires_at` は作成時+14日の絶対期限。

        延長はしない（§4 Domain rule）。`last_seen_at` は作成時に設定する。
        """
        return cls(
            id=id,
            user_id=user_id,
            token_hash=token_hash,
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_TTL,
            revoked_at=None,
        )

    def is_valid(self, now: datetime) -> bool:
        """失効していないことと期限未到来の論理積（§4 Domain rule）。"""
        return self.revoked_at is None and now < self.expires_at

    def touch(self, now: datetime) -> bool:
        """有効な認証済みリクエストで前回から5分以上経過した時だけ更新する。

        更新したかどうかを返す。失効済み・期限切れ・5分未満は更新しない
        （§4 時刻の意味）。
        """
        if not self.is_valid(now):
            return False
        if now - self.last_seen_at < LAST_SEEN_INTERVAL:
            return False
        self.last_seen_at = now
        return True

    def revoke(self, now: datetime) -> None:
        """失効させる。未失効なら一度だけ設定する。

        ログアウト・パスワード変更・回復・異常失効の時だけ呼ぶ。
        期限到来では設定しない（§4 時刻の意味）。
        """
        if self.revoked_at is not None:
            raise ValueError("session is already revoked")
        self.revoked_at = now


@dataclass
class Account:
    """Account Aggregate の実体（design/02-architecture.md §3）。

    `User` を根に、常に1件の `LoginCredential` と `RecoveryCredential` をまとめる。
    生の秘密値は持たず、ハッシュだけを保持する。Repository の取得・保存単位。
    """

    user: User
    login_credential: LoginCredential
    recovery_credential: RecoveryCredential
