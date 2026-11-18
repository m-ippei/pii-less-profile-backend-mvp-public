"""Identity のユースケース（design/04-use-cases.md §5）。

対象 Aggregate の更新は `BEGIN IMMEDIATE` の transaction で行い、書き込みロックを
保持するのは保存操作だけにする。**認証・検証（Argon2 の `hash` / `verify` /
`dummy_verify`）と読みは transaction の外**（リクエスト単位の接続で autocommit）で
行い、書き込みロックを CPU 処理中に保持しない（design/04 §5・design/10 §11・D-37）。
変更系（`ChangePassword`）は、検証〜更新の間で対象が変化していないことを transaction
内で再読して確認し、変化していれば拒否する。

時刻・乱数・ID は `Clock` / `SecretGenerator` / `IdGenerator` ポート経由で取得し、
生の秘密値は戻り値としてだけ外へ出す（Entity には保持しない。
design/14-dod-conventions.md §16）。

`CreateSession` / `AuthenticateSession` は `login_id` 不在でもダミーArgon2検証を
1回実行して処理時間を揃える（H-3 / H-4、design/07-security.md §8 認証フロー）。
"""

from dataclasses import dataclass
from datetime import datetime

from src.identity.domain.entities import Session
from src.identity.domain.ports import AccountRepository, SessionRepository, UnitOfWork
from src.identity.domain.value_objects import RecoveryKey, UserId
from src.shared.clock.clock import Clock
from src.shared.security.security import (
    IdGenerator,
    PasswordHasher,
    SecretGenerator,
    SecretHasher,
)
from src.shared.web.web import Principal

from .errors import (
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidRecoveryCredentialError,
    InvalidSessionError,
    ValidationError,
)

PASSWORD_METHOD = "password"
LOGIN_ID_LABEL = "login_id"
SESSION_LABEL = "session"
RECOVERY_KEY_LABEL = "recovery_key"


@dataclass(frozen=True)
class IdentityDependencies:
    """Identity ユースケースが使うポート一式。`main.py` で結線する。"""

    account_repository: AccountRepository
    session_repository: SessionRepository
    password_hasher: PasswordHasher
    secret_hasher: SecretHasher
    secret_generator: SecretGenerator
    id_generator: IdGenerator
    clock: Clock
    unit_of_work: UnitOfWork


@dataclass(frozen=True)
class IssuedSession:
    """`CreateSession` の結果。生 token は cookie に載せ、ここ以外へ再表示しない。"""

    token: str
    principal: Principal


@dataclass(frozen=True)
class ChangedPassword:
    """`ChangePassword` の結果。新 `recovery_key` と新Sessionの生 token。"""

    recovery_key: str
    token: str
    principal: Principal


@dataclass(frozen=True)
class RecoveredPassword:
    """`RecoverPassword` の結果。再発行した `login_id` と `recovery_key`。"""

    login_id: str
    recovery_key: str


def _principal_for(user_id: UserId) -> Principal:
    return Principal(user_id=user_id.value, method=PASSWORD_METHOD, scopes=())


class CreateSession:
    """`design/04-use-cases.md` §5 CreateSession。認証して新Sessionを作る。"""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def execute(
        self,
        *,
        login_id: str,
        password: str,
        current_session_token: str | None = None,
    ) -> IssuedSession:
        deps = self._deps
        login_id_hash = deps.secret_hasher.hash(LOGIN_ID_LABEL, login_id)
        # account の読みと Argon2 検証は transaction の外（autocommit）。書き込みロックを
        # 保持せず、実行枠が競合すれば `Argon2Guard` の 503 が本来どおり働く（D-37）。
        account = deps.account_repository.find_by_login_id_hash(login_id_hash)
        if account is None:
            # 該当なしでも1回検証して処理時間を揃える（H-3）。
            deps.password_hasher.dummy_verify()
            raise InvalidCredentialsError()
        if not deps.password_hasher.verify(account.login_credential.password_hash, password):
            raise InvalidCredentialsError()

        with deps.unit_of_work:
            now = deps.clock.now()
            if current_session_token is not None:
                self._revoke_presented_session(current_session_token, now)

            token = deps.secret_generator.session_token()
            session = Session.create(
                id=UserId(deps.id_generator.generate()),
                user_id=account.user.id,
                token_hash=deps.secret_hasher.hash(SESSION_LABEL, token),
                now=now,
            )
            deps.session_repository.save(session)
        return IssuedSession(token=token, principal=_principal_for(account.user.id))

    def _revoke_presented_session(self, token: str, now: datetime) -> None:
        """有効な旧Sessionが提示されていれば失効させる（§4 Domain rule）。"""
        deps = self._deps
        session = deps.session_repository.find_by_token_hash(
            deps.secret_hasher.hash(SESSION_LABEL, token)
        )
        if session is not None and session.is_valid(now):
            session.revoke(now)
            deps.session_repository.save(session)


class DeleteCurrentSession:
    """`design/04-use-cases.md` §5 DeleteCurrentSession。現Sessionを冪等に失効させる。"""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def execute(self, *, token: str | None) -> None:
        if token is None:
            return
        deps = self._deps
        token_hash = deps.secret_hasher.hash(SESSION_LABEL, token)
        with deps.unit_of_work:
            session = deps.session_repository.find_by_token_hash(token_hash)
            if session is None or session.revoked_at is not None:
                return
            session.revoke(deps.clock.now())
            deps.session_repository.save(session)


class AuthenticateSession:
    """`design/04-use-cases.md` §5 AuthenticateSession。tokenから `Principal` を作る。"""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def execute(self, *, token: str) -> Principal:
        deps = self._deps
        token_hash = deps.secret_hasher.hash(SESSION_LABEL, token)
        with deps.unit_of_work:
            session = deps.session_repository.find_by_token_hash(token_hash)
            now = deps.clock.now()
            if session is None or not session.is_valid(now):
                raise InvalidSessionError()
            deps.session_repository.touch(session, now)
        return _principal_for(session.user_id)


class ChangePassword:
    """`design/04-use-cases.md` §5 ChangePassword。

    検証（Argon2）は transaction の外で行い、transaction 内では account を再読して
    検証〜更新の間の変化を検知してから、資格情報更新 → recovery_credentials更新 →
    全Session失効 → 新Session作成を行う（D-37・Q2=(a)）。
    """

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def execute(
        self,
        *,
        user_id: UserId,
        current_password: str,
        new_password: str,
    ) -> ChangedPassword:
        deps = self._deps
        # account の読み・検証・同一判定・新パスワードのハッシュは transaction の外
        # （autocommit）。書き込みロックを CPU 処理中に保持しない（D-37）。
        account = deps.account_repository.find_by_id(user_id)
        if account is None:
            raise InvalidSessionError()
        verified_password_hash = account.login_credential.password_hash
        if not deps.password_hasher.verify(verified_password_hash, current_password):
            raise InvalidCurrentPasswordError()
        if deps.password_hasher.verify(verified_password_hash, new_password):
            # 現パスワードと同一不可（§4 Password 規則）。
            raise ValidationError()
        new_password_hash = deps.password_hasher.hash(new_password)

        with deps.unit_of_work:
            # 検証〜更新の間の変化を検知する（Q2=(a)）。再読し、検証に使った
            # `password_hash` と一致しなければ `INVALID_CURRENT_PASSWORD`（H-14）で拒否。
            current = deps.account_repository.find_by_id(user_id)
            if current is None or current.login_credential.password_hash != verified_password_hash:
                raise InvalidCurrentPasswordError()

            now = deps.clock.now()
            current.login_credential.change_password(new_password_hash, now)
            recovery_key = deps.secret_generator.recovery_key()
            current.recovery_credential.rotate(
                deps.secret_hasher.hash(RECOVERY_KEY_LABEL, recovery_key), now
            )
            deps.account_repository.save(current)

            deps.session_repository.revoke_all_for_user(user_id, now)
            token = deps.secret_generator.session_token()
            session = Session.create(
                id=UserId(deps.id_generator.generate()),
                user_id=user_id,
                token_hash=deps.secret_hasher.hash(SESSION_LABEL, token),
                now=now,
            )
            deps.session_repository.save(session)
        return ChangedPassword(
            recovery_key=recovery_key, token=token, principal=_principal_for(user_id)
        )


class RecoverPassword:
    """`design/04-use-cases.md` §5 RecoverPassword。

    `recovery_key_hash` 一致で特定 → login_credentials更新 → recovery_credentials更新
    → 全Session失効。Sessionは発行しない（D-1）。
    """

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def execute(self, *, recovery_key: str, new_password: str) -> RecoveredPassword:
        deps = self._deps
        # 接尾の表示識別文字は VO で排除され、Base58 固定長のみが認証対象（§4 RecoveryKey）。
        normalized = RecoveryKey(recovery_key).value
        recovery_hash = deps.secret_hasher.hash(RECOVERY_KEY_LABEL, normalized)
        # 新パスワードのハッシュ（Argon2）は transaction の外。HMAC 特定・更新・全Session
        # 失効は同じ transaction で行う（D-37）。
        new_password_hash = deps.password_hasher.hash(new_password)
        with deps.unit_of_work:
            account = deps.account_repository.find_by_recovery_key_hash(recovery_hash)
            if account is None:
                # 高エントロピー値のためダミー検証は不要（H-7、design/07-security.md §8）。
                raise InvalidRecoveryCredentialError()

            now = deps.clock.now()
            new_login_id = deps.secret_generator.login_id()
            new_recovery_key = deps.secret_generator.recovery_key()
            account.login_credential.reissue(
                deps.secret_hasher.hash(LOGIN_ID_LABEL, new_login_id),
                new_password_hash,
                now,
            )
            account.recovery_credential.rotate(
                deps.secret_hasher.hash(RECOVERY_KEY_LABEL, new_recovery_key), now
            )
            deps.account_repository.save(account)
            deps.session_repository.revoke_all_for_user(account.user.id, now)
        return RecoveredPassword(login_id=new_login_id, recovery_key=new_recovery_key)
