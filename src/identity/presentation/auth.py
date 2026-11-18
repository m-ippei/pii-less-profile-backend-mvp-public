"""Identity の presentation 補助（design/02-architecture.md §3 / design/07-security.md §8）。

`Principal` は `shared/web` に定義され、Identity が作り、Profile 側は `user_id` だけを
受け取る。`PrincipalResolver` は Session token（`__Host-ap_session` cookie の中身）から
`Principal` を解決する callable で、Identity が提供し `main.py` が結線する。Profile 側の
presentation はこの callable を引数で受け取るため `identity` を import しない。

FastAPI には依存しない。cookie の読み出しと `Request` の組み立ては presentation の
結線側（`main.py`）の責務。
"""

from collections.abc import Callable

from src.identity.application.errors import InvalidSessionError
from src.identity.application.usecases import AuthenticateSession, IdentityDependencies
from src.shared.web.web import Principal


class PrincipalResolver:
    """Session token から `Principal` を解決する callable。

    呼び出しごとに新しい `IdentityDependencies`（新しい接続・`UnitOfWork`）を組み立てる
    ため、依存はファクトリで受け取る（接続はリクエスト単位。design/10-infra-ops.md §11）。
    token が無い場合は `AuthenticateSession` を呼ばずに `InvalidSessionError` にする
    （H-5 / H-6 / H-11 の同一応答。design/06-errors.md §7）。
    """

    def __init__(self, dependencies_factory: Callable[[], IdentityDependencies]) -> None:
        self._dependencies_factory = dependencies_factory

    def __call__(self, token: str | None) -> Principal:
        if token is None:
            raise InvalidSessionError()
        return AuthenticateSession(self._dependencies_factory()).execute(token=token)
