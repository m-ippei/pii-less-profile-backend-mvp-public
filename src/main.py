"""アプリの結線（design/02-architecture.md §3 / design/10-infra-ops.md §11）。

Context 跨ぎを書いてよいのは `src/app/` とこのファイルだけ。ここでポート一式を組み立てる。

- ポートはリクエストごとに新しい接続で組み立てる（接続はリクエスト単位）。
- Identity が `Principal` を作り、Profile 側へは `user_id` だけを渡す。cookie から
  `Principal` を解決する callable は Identity が提供し（`PrincipalResolver`）、ここで結線する。
- middleware / エラー handler / CSRF は M07、他エンドポイントは M08/M09 のスコープ。
  ここでは `/healthz`・`/_envcheck`・最小の `POST /profiles` を持つ。
"""

import os
import sqlite3
import sys
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from src.app.create_profile import CreateProfile, CreateProfileDependencies
from src.app.errors import ProfileCreationFailedError
from src.app.http.argon2_guard import Argon2Guard, GuardedPasswordHasher
from src.app.http.client_ip import request_client_ip
from src.app.http.cookies import (
    SESSION_COOKIE,
    delete_session_cookie,
    reissue_csrf,
    set_session_cookie,
)
from src.app.http.csrf import CsrfMiddleware
from src.app.http.errors import UnhandledExceptionMiddleware, register_error_handlers
from src.app.http.logging import (
    AccessLogMiddleware,
    RateLimitCapLogger,
    SecurityEvent,
    SecurityEventLogger,
    configure_logging,
)
from src.app.http.middleware import (
    BodySizeLimitMiddleware,
    CommonHeadersMiddleware,
    configure_threadpool,
)
from src.app.http.pipeline import run_request_pipeline
from src.app.http.ratelimit import (
    LOGIN_ACCOUNT,
    LOGIN_IP,
    PASSWORD_CHANGE,
    PROFILE_CREATE,
    RECOVERY_PER_HOUR,
    RECOVERY_PER_MINUTE,
    RateLimit,
    RateLimitExceededError,
    enforce,
)
from src.app.http.worker_guard import ensure_single_worker
from src.identity.application.errors import (
    InvalidCredentialsError,
    InvalidRecoveryCredentialError,
)
from src.identity.application.usecases import (
    SESSION_LABEL,
    ChangedPassword,
    ChangePassword,
    CreateSession,
    DeleteCurrentSession,
    IdentityDependencies,
    IssuedSession,
    RecoveredPassword,
    RecoverPassword,
)
from src.identity.domain.value_objects import LoginId, RecoveryKey, UserId
from src.identity.infrastructure.account_repository import SqlAccountRepository
from src.identity.infrastructure.session_repository import SqlSessionRepository
from src.identity.presentation.auth import PrincipalResolver
from src.identity.presentation.inputs import parse_change, parse_login, parse_recovery
from src.profile.application.usecases import (
    GetMyProfile,
    GetPublicProfile,
    MyProfile,
    ProfileDependencies,
    UpdateMyProfile,
)
from src.profile.domain.value_objects import ProfileContent, ProfileStatus
from src.profile.infrastructure.profile_repository import SqlProfileRepository
from src.profile.presentation.inputs import parse_update
from src.shared.clock.clock import SystemClock, format_timestamp
from src.shared.db import UnitOfWork, connect
from src.shared.errors.errors import ErrorCode
from src.shared.ratelimit.ratelimit import InMemoryRateLimiter, max_keys_from_env
from src.shared.security.security import (
    IdGenerator,
    SecretGenerator,
    SecretHasher,
    load_app_pepper,
)
from src.shared.web.web import Principal, build_error_body


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """起動時の不変条件を検査する（worker=1、threadpool 上限8）。"""
    ensure_single_worker()
    configure_threadpool()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

# レート制限はアプリ全体で1つのインメモリ実装を共有する（リクエストごとに作らない）。
# worker ごとに分断されるため worker=1 を前提とする（D-24。起動時に lifespan で検査）。
# key 上限は env `RATE_LIMIT_MAX_KEYS`（既定10,000）。不正値は起動時に fail-closed で拒否
# （design/07-security.md §8 / design/10-infra-ops.md §11）。
rate_limiter = InMemoryRateLimiter(SystemClock(), max_keys=max_keys_from_env())

# ログ設定（uvicorn アクセスログ無効・アプリログ INFO）と、セキュリティイベントの記録器。
# hasher は遅延評価（APP_PEPPER はリクエスト時に読む。起動時に SecretHasher を作らない）。
configure_logging()
security_events = SecurityEventLogger(lambda: _secret_hasher(), SystemClock())
rate_limit_caps = RateLimitCapLogger()

# Argon2 の同時実行枠（`ARGON2_MAX_CONCURRENCY`、既定2）。アプリ全体で1つ共有し、
# 枠が埋まれば待たずに 503 `SERVICE_UNAVAILABLE` を返す（design/10-infra-ops.md §11）。
argon2_guard = Argon2Guard()

# API は `/api/v1` 配下（design/05-api-contract.md §6 共通規約）。ページ
# （`/healthz`・`/_envcheck`）はルート直下のまま。`include_router` はルート定義の
# 後で呼ぶ（呼び出し時点のルートが複製されるため）。
api_router = APIRouter(prefix="/api/v1")

# 全応答が通る共通処理。登録順は内側→外側。CSRF を最内、本文上限（413）をその外、
# 想定外例外の境界（500）をその外、共通ヘッダーをその外、運用ログ（app.access）を最外に
# して、すべてのエラー応答にもヘッダーを付けつつ判定順を守り、最終 status を1行記録する。
# エラーハンドラは `ExceptionMiddleware` 側に登録する。
app.add_middleware(CsrfMiddleware, security_logger=security_events)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(UnhandledExceptionMiddleware)
app.add_middleware(CommonHeadersMiddleware)
app.add_middleware(AccessLogMiddleware)
register_error_handlers(app)

_NO_STORE = {"Cache-Control": "no-store"}

# レート制限の account 単位 key に使う HMAC ラベル（design/07-security.md §8 の5ラベルの1つ）。
_RATELIMIT_LABEL = "ratelimit"

# 静的アセット（design/05-api-contract.md §6 Web画面）。`__file__` 基準で解決し、
# 実行時の cwd に依存しない（app / test どちらのコンテナでも `/app/static` を指す）。
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _database_path() -> str:
    """`DATABASE_PATH`。未設定は `KeyError`（呼び出し側が 503 等へ変換する）。"""
    return os.environ["DATABASE_PATH"]


def _pepper() -> bytes:
    """`APP_PEPPER` をデコードする。未設定・不正は例外（起動時検証は M12）。"""
    return load_app_pepper(os.environ["APP_PEPPER"])


def _connection() -> sqlite3.Connection:
    """リクエスト1件ぶんの接続（PRAGMA 設定済み）。"""
    return connect(_database_path())


def _secret_hasher() -> SecretHasher:
    return SecretHasher(_pepper())


def build_identity_dependencies() -> IdentityDependencies:
    """Identity ユースケースのポート一式を、新しい接続で組み立てる。"""
    connection = _connection()
    return IdentityDependencies(
        account_repository=SqlAccountRepository(connection),
        session_repository=SqlSessionRepository(connection),
        password_hasher=GuardedPasswordHasher(argon2_guard),
        secret_hasher=_secret_hasher(),
        secret_generator=SecretGenerator(),
        id_generator=IdGenerator(),
        clock=SystemClock(),
        unit_of_work=UnitOfWork(connection),
    )


def build_profile_dependencies() -> ProfileDependencies:
    """Profile ユースケースのポート一式を、新しい接続で組み立てる。"""
    connection = _connection()
    return ProfileDependencies(
        profile_repository=SqlProfileRepository(connection),
        clock=SystemClock(),
        unit_of_work=UnitOfWork(connection),
    )


def build_create_profile_dependencies() -> CreateProfileDependencies:
    """`CreateProfile` の1試行ぶんのポート一式を、新しい接続で組み立てる。"""
    connection = _connection()
    return CreateProfileDependencies(
        account_repository=SqlAccountRepository(connection),
        profile_repository=SqlProfileRepository(connection),
        session_repository=SqlSessionRepository(connection),
        password_hasher=GuardedPasswordHasher(argon2_guard),
        secret_hasher=_secret_hasher(),
        secret_generator=SecretGenerator(),
        id_generator=IdGenerator(),
        clock=SystemClock(),
        unit_of_work=UnitOfWork(connection),
    )


def build_create_profile() -> CreateProfile:
    """Context 跨ぎ `CreateProfile` を、毎試行新しい接続を返すファクトリで組み立てる。"""
    return CreateProfile(build_create_profile_dependencies)


def build_principal_resolver() -> PrincipalResolver:
    """Identity が提供する Principal 解決 callable を、実ポートに結線して返す。"""
    return PrincipalResolver(build_identity_dependencies)


def require_principal(request: Request) -> Principal:
    """Session cookie から `Principal` を解決する。未認証は `InvalidSessionError`。

    エラー応答への変換（401 `INVALID_SESSION`）は M07 の例外 handler が行う。
    """
    return build_principal_resolver()(request.cookies.get(SESSION_COOKIE))


def _enforce(checks: Iterable[tuple[RateLimit, str]], request: Request) -> None:
    """レート制限を判定し、超過なら種別ごとに `rate_limited` を記録して 429 用例外を送出する。

    上限到達（fail-closed）の集計（`app.limit`）も、累積カウンタの増分があれば出す
    （`design/15-logging.md` §17）。
    """
    try:
        enforce(rate_limiter, checks)
    except RateLimitExceededError as error:
        for kind in error.kinds:
            security_events.emit(SecurityEvent.RATE_LIMITED, request=request, kind=kind)
        rate_limit_caps.emit_delta(rate_limiter.cap_reached_count)
        raise


@api_router.post("/profiles", status_code=201)
def create_profile(request: Request) -> JSONResponse:
    """`POST /api/v1/profiles`（design/05-api-contract.md §6）。MVP の入口。

    成功: 201 で `{public_id, login_id, initial_password, recovery_key}` を一度だけ返し、
    Session token を `__Host-ap_session` cookie に載せ、`Cache-Control: no-store` を付ける。
    IP 単位 10回/時（`PROFILE_CREATE`）を use case の前に判定し、超過は 429 `RATE_LIMITED`
    ＋`Retry-After`（H-13、design/07-security.md §8）。H-8（再試行尽き）は 500
    `INTERNAL_ERROR` にし、秘密値・内部理由を出さない（design/06-errors.md §7）。
    """
    _enforce(((PROFILE_CREATE, f"profile-create:{request_client_ip(request)}"),), request)
    try:
        created = build_create_profile().execute()
    except ProfileCreationFailedError:
        return JSONResponse(
            build_error_body(ErrorCode.INTERNAL_ERROR), status_code=500, headers=_NO_STORE
        )
    security_events.emit(SecurityEvent.PROFILE_CREATED, request=request)
    response = JSONResponse(
        {
            "public_id": created.public_id,
            "login_id": created.login_id,
            "initial_password": created.initial_password,
            "recovery_key": created.recovery_key,
        },
        status_code=201,
        headers=_NO_STORE,
    )
    set_session_cookie(response, created.session_token)
    return response


@api_router.get("/profiles/{public_id}")
def get_public_profile(public_id: str) -> JSONResponse:
    """`GET /api/v1/profiles/{public_id}`（design/05-api-contract.md §6）。

    認証不要。公開中のみ 200 で `{public_id, content}` だけを返す（AC-3）。存在しない・
    非公開・`public_id` 形式違反はすべて同一の 404 `PROFILE_NOT_FOUND`（H-1 / H-2）で、
    入力値を echo しない（AC-2）。`Cache-Control: no-store` は共通ミドルウェアが付ける。
    """
    profile = GetPublicProfile(build_profile_dependencies()).execute(public_id=public_id)
    return JSONResponse(
        {"public_id": profile.public_id, "content": profile.content},
        status_code=200,
    )


class _LoginBody(BaseModel):
    """`POST /api/v1/sessions` の本文（design/05-api-contract.md §6）。未知フィールドは422。"""

    model_config = ConfigDict(extra="forbid")

    login_id: str
    password: str


@api_router.post("/sessions", status_code=204)
def create_session(request: Request, body: _LoginBody) -> Response:
    """`POST /api/v1/sessions`（design/05-api-contract.md §6 / design/07-security.md §8）。

    判定順は 入力検証（`parse_login`）→ rate limit → use case（`CreateSession`）。account
    単位（IP ＋ `HMAC(ratelimit, login_id)`、5回/分）と IP 単位（20回/分）の両方を判定し、
    超過は 429 `RATE_LIMITED` ＋ `Retry-After`（H-13）。`login_id` は「最初の `_` より前」を
    抽出済みの `LoginId` を key に使う（suffix 違いで回避できない）。`login_id` 不明と
    password 誤りは同一の 401 `INVALID_CREDENTIALS`（H-3 / H-4）。成功は 204 で
    `__Host-ap_session` を設定し `__Host-csrf` を再発行する。提示された有効な旧Sessionは
    `CreateSession` が失効させる。
    """

    def execute(_auth: object | None, inputs: tuple[LoginId, str] | None) -> IssuedSession:
        assert inputs is not None
        login_id, password = inputs
        return CreateSession(build_identity_dependencies()).execute(
            login_id=login_id.value,
            password=password,
            current_session_token=request.cookies.get(SESSION_COOKIE),
        )

    def rate_limit(_auth: object | None, inputs: tuple[LoginId, str] | None) -> None:
        assert inputs is not None
        login_id, _password = inputs
        ip = request_client_ip(request)
        account = f"{ip}:{_secret_hasher().hash(_RATELIMIT_LABEL, login_id.value)}"
        _enforce(
            ((LOGIN_ACCOUNT, f"login-account:{account}"), (LOGIN_IP, f"login-ip:{ip}")),
            request,
        )

    try:
        issued = run_request_pipeline(
            execute,
            validate=lambda: parse_login(body.login_id, body.password),
            rate_limit=rate_limit,
        )
    except InvalidCredentialsError:
        security_events.emit(SecurityEvent.LOGIN_FAILED, request=request)
        raise
    security_events.emit(SecurityEvent.LOGIN_SUCCEEDED, request=request)
    response = Response(status_code=204)
    set_session_cookie(response, issued.token)
    reissue_csrf(response, SecretGenerator())
    return response


@api_router.delete("/sessions/current", status_code=204)
def delete_current_session(request: Request) -> Response:
    """`DELETE /api/v1/sessions/current`（design/05-api-contract.md §6）。

    任意認証・冪等。未ログイン・無効 Session でも 204 を返し（401 にしない）、
    `__Host-ap_session` を削除し `__Host-csrf` を再発行する。
    """
    DeleteCurrentSession(build_identity_dependencies()).execute(
        token=request.cookies.get(SESSION_COOKIE)
    )
    response = Response(status_code=204)
    delete_session_cookie(response)
    reissue_csrf(response, SecretGenerator())
    return response


class _ChangePasswordBody(BaseModel):
    """`POST /api/v1/me/password` の本文（design/05-api-contract.md §6）。未知フィールドは422。"""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


@api_router.post("/me/password")
def change_password(request: Request, body: _ChangePasswordBody) -> JSONResponse:
    """`POST /api/v1/me/password`（design/05-api-contract.md §6 / design/07-security.md §8）。

    判定順は 認証（401）→ 入力検証（422）→ rate limit → use case。rate limit は Session
    単位（5回/分）で、Session の token hash（`HMAC(session, token)`）を key に使う。成功は
    200 `{recovery_key}` で新しい `recovery_key` を一度だけ返し、全Sessionを失効させて
    新Sessionを発行し `__Host-csrf` を再発行する。現パスワード誤りは 403
    `INVALID_CURRENT_PASSWORD`（H-14。Session は有効なので 401 にしない）。成功・失敗を
    区別せず全試行を計上する。
    """

    def execute(principal: Principal | None, inputs: tuple[str, str] | None) -> ChangedPassword:
        assert principal is not None
        assert inputs is not None
        current_password, new_password = inputs
        return ChangePassword(build_identity_dependencies()).execute(
            user_id=UserId(principal.user_id),
            current_password=current_password,
            new_password=new_password,
        )

    def rate_limit(principal: Principal | None, _inputs: tuple[str, str] | None) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        assert token is not None
        key = f"password-change:{_secret_hasher().hash(SESSION_LABEL, token)}"
        _enforce(((PASSWORD_CHANGE, key),), request)

    changed = run_request_pipeline(
        execute,
        authenticate=lambda: require_principal(request),
        validate=lambda: parse_change(body.current_password, body.new_password),
        rate_limit=rate_limit,
    )
    security_events.emit(SecurityEvent.PASSWORD_CHANGED, request=request)
    response = JSONResponse({"recovery_key": changed.recovery_key}, status_code=200)
    set_session_cookie(response, changed.token)
    reissue_csrf(response, SecretGenerator())
    return response


class _RecoveryBody(BaseModel):
    """`POST /api/v1/recovery/password-resets` の本文（design/05-api-contract.md §6）。"""

    model_config = ConfigDict(extra="forbid")

    recovery_key: str
    new_password: str


@api_router.post("/recovery/password-resets")
def recover_password(request: Request, body: _RecoveryBody) -> JSONResponse:
    """`POST /api/v1/recovery/password-resets`（design/05-api-contract.md §6 /
    design/07-security.md §8）。

    判定順は 入力検証（`parse_recovery`）→ rate limit → use case（`RecoverPassword`）。
    rate limit は IP 単位で 5回/分 と 20回/時 の両方を判定し、超過は 429 `RATE_LIMITED`
    ＋ `Retry-After`（H-13）。成功は 200 `{login_id, recovery_key}` で新しい資格情報を
    一度だけ返し、全Sessionを失効させ、Session は発行せず `__Host-ap_session` を削除する
    （`__Host-csrf` は再発行する）。`recovery_key` は `HMAC` 一致で直接特定し、該当なしは
    400 `INVALID_RECOVERY_CREDENTIAL`（H-7）。高エントロピー値のためダミー検証はしない。
    """

    def execute(_auth: object | None, inputs: tuple[RecoveryKey, str] | None) -> RecoveredPassword:
        assert inputs is not None
        recovery_key, new_password = inputs
        return RecoverPassword(build_identity_dependencies()).execute(
            recovery_key=recovery_key.value,
            new_password=new_password,
        )

    def rate_limit(_auth: object | None, _inputs: tuple[RecoveryKey, str] | None) -> None:
        ip = request_client_ip(request)
        _enforce(
            (
                (RECOVERY_PER_MINUTE, f"recovery-minute:{ip}"),
                (RECOVERY_PER_HOUR, f"recovery-hour:{ip}"),
            ),
            request,
        )

    try:
        recovered = run_request_pipeline(
            execute,
            validate=lambda: parse_recovery(body.recovery_key, body.new_password),
            rate_limit=rate_limit,
        )
    except InvalidRecoveryCredentialError:
        security_events.emit(SecurityEvent.RECOVERY_FAILED, request=request)
        raise
    security_events.emit(SecurityEvent.RECOVERY_SUCCEEDED, request=request)
    response = JSONResponse(
        {"login_id": recovered.login_id, "recovery_key": recovered.recovery_key},
        status_code=200,
        headers=_NO_STORE,
    )
    delete_session_cookie(response)
    reissue_csrf(response, SecretGenerator())
    return response


def _my_profile_body(profile: MyProfile) -> dict[str, str | None]:
    """`GET` / `PATCH /me/profile` の応答本文（design/05-api-contract.md §6）。"""
    published_at = profile.published_at
    return {
        "public_id": profile.public_id,
        "status": profile.status,
        "content": profile.content,
        "published_at": None if published_at is None else format_timestamp(published_at),
    }


@api_router.get("/me/profile")
def get_my_profile(request: Request) -> JSONResponse:
    """`GET /api/v1/me/profile`（design/05-api-contract.md §6 / design/07-security.md §8）。

    認証必須。`Principal` から `user_id` だけを取り出して `GetMyProfile` へ渡す（§3）。
    200 で `{public_id, status, content, published_at}` を返し、`published_at` は
    27文字 ISO 8601 または `null`。未ログイン・無効 Session は 401 `INVALID_SESSION`
    （M07 の handler が `ap_session` を削除）。`no-store` は共通ミドルウェアが付ける。
    """

    def execute(principal: Principal | None, _inputs: object | None) -> MyProfile:
        assert principal is not None
        return GetMyProfile(build_profile_dependencies()).execute(user_id=principal.user_id)

    profile = run_request_pipeline(execute, authenticate=lambda: require_principal(request))
    return JSONResponse(_my_profile_body(profile), status_code=200)


class _UpdateProfileBody(BaseModel):
    """`PATCH /api/v1/me/profile` の本文（design/05-api-contract.md §6）。未知フィールドは422。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    status: str | None = None


@api_router.patch("/me/profile")
def update_my_profile(request: Request, body: _UpdateProfileBody) -> JSONResponse:
    """`PATCH /api/v1/me/profile`（design/05-api-contract.md §6 / design/04-use-cases.md §5）。

    認証必須（401）。判定順は 認証 → 入力検証（422）→ use case。1項目以上必須で、
    `content` 未指定＝変更なし・`""`＝空・`null` は 422。`status` は `draft`/`published`。
    空 content の publish は 422 `PROFILE_NOT_PUBLISHABLE`（H-15）。同状態・同内容は no-op
    成功。200 で `GET /me/profile` と同形の4フィールドを返す（`no-store` は共通ミドルウェア）。
    """
    provided = body.model_fields_set

    def execute(
        principal: Principal | None,
        inputs: tuple[ProfileContent | None, ProfileStatus | None] | None,
    ) -> MyProfile:
        assert principal is not None
        assert inputs is not None
        content, status = inputs
        return UpdateMyProfile(build_profile_dependencies()).execute(
            user_id=principal.user_id,
            content=content,
            status=status,
        )

    profile = run_request_pipeline(
        execute,
        authenticate=lambda: require_principal(request),
        validate=lambda: parse_update(
            content_provided="content" in provided,
            content=body.content,
            status_provided="status" in provided,
            status=body.status,
        ),
    )
    return JSONResponse(_my_profile_body(profile), status_code=200)


app.include_router(api_router)


def _page(filename: str) -> FileResponse:
    """`static/` 配下の素の HTML を返す（design/05-api-contract.md §6 Web画面）。"""
    return FileResponse(STATIC_DIR / filename)


@app.get("/new")
def new_page() -> FileResponse:
    """`GET /new`（design/05-api-contract.md §6 Web画面）。作成画面。

    `new.js` が `POST /api/v1/profiles` を実 CSRF 経路で送り、201 の秘密値を一度だけ
    `/credentials-saved` へ渡す（design/12-ux-secrets.md §13）。
    """
    return _page("new.html")


@app.get("/login")
def login_page() -> FileResponse:
    """`GET /login`（design/05-api-contract.md §6 Web画面）。ログインフォーム。"""
    return _page("login.html")


@app.get("/profile-editor")
def profile_editor_page() -> FileResponse:
    """`GET /profile-editor`（design/05-api-contract.md §6 Web画面）。

    編集・公開/非公開の画面。`auth-guard.js` が `GET /api/v1/me/profile` を probe し、
    未ログインなら `/login` へ遷移する。保存は `PATCH /api/v1/me/profile` を送る。
    """
    return _page("profile-editor.html")


@app.get("/account")
def account_page() -> FileResponse:
    """`GET /account`（design/05-api-contract.md §6 Web画面）。

    `auth-guard.js` が `GET /api/v1/me/profile` を probe し、未ログインなら `/login` へ遷移する。
    """
    return _page("account.html")


@app.get("/recovery")
def recovery_page() -> FileResponse:
    """`GET /recovery`（design/05-api-contract.md §6 Web画面）。回復フォーム。"""
    return _page("recovery.html")


@app.get("/credentials-saved")
def credentials_saved_page() -> FileResponse:
    """`GET /credentials-saved`（design/05-api-contract.md §6 Web画面）。

    作成・パスワード変更・回復の3経路で共用する保存確認画面（design/12-ux-secrets.md §13）。
    秘密値はサーバーから渡さず、クライアントの `sessionStorage` から一度だけ読む。
    """
    return _page("credentials-saved.html")


@app.get("/profiles/{public_id}")
def public_profile_page(public_id: str) -> FileResponse:
    """`GET /profiles/{public_id}`（design/05-api-contract.md §6 Web画面）。

    公開ページの静的な殻。`public_id` の値は使わず、常に同じ HTML を返す（判定は
    `public-profile.js` が `GET /api/v1/profiles/{public_id}` を呼んで行う）。したがって
    形式違反・非公開・不存在でも同じ殻を返し、入力値を echo しない。`no-store`・`noindex`
    は M07 の共通ヘッダーに従う。`/api/v1/profiles/{public_id}` とは別経路。
    """
    return _page("profiles.html")


# `/static` 配下の素の HTML / JS / CSS を配信する。`Cache-Control` は上書きしない
# （`cache_control_for` の唯一の例外。design/07-security.md §8 Cache-Control）。
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """ヘルスチェック。認証・CSRF 不要（AC-14）。"""
    try:
        connection = _connection()
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except KeyError, sqlite3.Error:
        return JSONResponse({"status": "unavailable"}, status_code=503, headers=_NO_STORE)
    return JSONResponse({"status": "ok"}, headers=_NO_STORE)


def _is_writable(path: str) -> bool:
    try:
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


@app.get("/_envcheck")
def envcheck(request: Request) -> JSONResponse:
    """環境確認用。ENV=devのときだけ有効（秘密値は返さない）。"""
    if os.environ.get("ENV") != "dev":
        return JSONResponse({"error": "not found"}, status_code=404, headers=_NO_STORE)

    db_path = os.environ.get("DATABASE_PATH", "")
    journal_mode = None
    try:
        connection = _connection()
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
    except KeyError, sqlite3.Error:
        pass

    return JSONResponse(
        {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "journal_mode": journal_mode,  # 期待値: wal
            "uid": os.getuid(),  # 期待値: 10001（非root） # type: ignore
            "data_dir_writable": _is_writable(os.path.dirname(db_path) or "."),  # 期待値: true
            "app_dir_writable": _is_writable("/app"),  # 期待値: false（read-only rootfs）
            "app_pepper_set": bool(os.environ.get("APP_PEPPER")),  # 期待値: true
            "public_base_url": os.environ.get("PUBLIC_BASE_URL"),
            "x_forwarded_for": request.headers.get("x-forwarded-for"),  # Caddy経由なら値あり
            "scheme_seen_by_app": request.url.scheme,  # http（Caddyが終端するため正常）
        },
        headers=_NO_STORE,
    )
