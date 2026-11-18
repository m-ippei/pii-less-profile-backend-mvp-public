"""Cookie の発行・削除（design/07-security.md §8 Cookie）。

`__Host-` 接頭辞は「`Secure`・`Path=/`・`Domain` なし」を強制する。開発環境も HTTPS で
動かすため、`Secure` を外す設定・分岐は作らない（§8 不変規則）。

- `__Host-ap_session=<token>`: `Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=1209600`
- `__Host-csrf=<token>`: `Path=/; Secure; SameSite=Lax`（JS が読むため `HttpOnly` なし）

無効 Session 応答・ログアウトでは `ap_session` を削除する。ログイン・パスワード変更・回復・
ログアウト成功時の `csrf` 再発行は `reissue_csrf` を使う（各エンドポイントへの結線は M08）。
"""

from starlette.responses import Response

from src.shared.security.security import SecretGenerator

SESSION_COOKIE = "__Host-ap_session"
CSRF_COOKIE = "__Host-csrf"
# `Max-Age=1209600`（14日。design/07-security.md §8 Cookie）。
SESSION_MAX_AGE_SECONDS = 1209600


def set_session_cookie(response: Response, token: str) -> None:
    """`__Host-ap_session` を §8 の属性で設定する。"""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def delete_session_cookie(response: Response) -> None:
    """`__Host-ap_session` を削除する（無効 Session 応答・ログアウト）。"""
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def set_csrf_cookie(response: Response, token: str) -> None:
    """`__Host-csrf` を §8 の属性で設定する（`HttpOnly` なし）。"""
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )


def reissue_csrf(response: Response, secret_generator: SecretGenerator) -> str:
    """新しい CSRF トークンを生成して設定し、その値を返す。

    ログイン・パスワード変更・回復・ログアウトの成功時に呼ぶ（§8 Cookie）。
    """
    token = secret_generator.csrf_token()
    set_csrf_cookie(response, token)
    return token
