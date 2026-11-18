"""AC-4: 本人以外がプロフィールを変更する経路が無い（design/07-security.md §8 /
design/05-api-contract.md §6 / design/11-testing.md §12 architecture 層）。

FastAPI のルート表を走査し、プロフィールの書き込みが `POST /api/v1/profiles`（本人の作成）と
`PATCH /api/v1/me/profile`（本人の更新）だけであり、`user_id` や他人の `public_id` をパスに
取る更新ルートが存在しないことを**構造的**に確認する。認証・所有者境界の実行時検証は api 層
（`tests/api/test_patch_me_profile.py` 等）。
"""

from collections.abc import Iterable

from src.main import app

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# プロフィールに関する書き込みで唯一許す経路（作成は本人、更新は `/me`）。
ALLOWED_PROFILE_WRITES = frozenset(
    {
        ("POST", "/api/v1/profiles"),
        ("PATCH", "/api/v1/me/profile"),
    }
)

Route = tuple[str, frozenset[str]]


def _routes() -> list[Route]:
    """`app.routes` から `(path, methods)` を取り出す（メソッドを持たない Mount は除外）。"""
    routes: list[Route] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        routes.append((path, frozenset(methods)))
    return routes


def _path_param_writes(routes: Iterable[Route]) -> list[str]:
    """パスパラメータ（`{...}`）を取る書き込みルートを返す。"""
    return [
        f"{method} {path}"
        for path, methods in routes
        for method in methods
        if method in WRITE_METHODS and "{" in path
    ]


def _disallowed_profile_writes(routes: Iterable[Route]) -> list[str]:
    """プロフィールへの書き込みのうち、許可された2経路以外を返す。"""
    return [
        f"{method} {path}"
        for path, methods in routes
        if "/profile" in path
        for method in methods
        if method in WRITE_METHODS and (method, path) not in ALLOWED_PROFILE_WRITES
    ]


def test_no_write_route_takes_a_path_identifier() -> None:
    """書き込みルートはパスに識別子（`{public_id}` / `{user_id}`）を取らない。"""
    assert _path_param_writes(_routes()) == []


def test_profile_write_is_create_or_patch_me_only() -> None:
    """プロフィールの書き込みは `POST /profiles` と `PATCH /me/profile` だけ。"""
    assert _disallowed_profile_writes(_routes()) == []


def test_no_route_puts_user_id_in_the_path() -> None:
    """`user_id` を露出するルートが存在しない。"""
    offenders = [
        f"{method} {path}" for path, methods in _routes() for method in methods if "user_id" in path
    ]
    assert offenders == []


def test_detects_identifier_update_route() -> None:
    """陽性対照: パス識別子を取る更新ルートを検出する。"""
    routes: list[Route] = [("/api/v1/profiles/{public_id}", frozenset({"PATCH"}))]
    assert _path_param_writes(routes) == ["PATCH /api/v1/profiles/{public_id}"]
    assert _disallowed_profile_writes(routes) == ["PATCH /api/v1/profiles/{public_id}"]


def test_allows_create_and_patch_me() -> None:
    """陰性対照: 許された2経路（作成・`/me` 更新）は検出しない。"""
    routes: list[Route] = [
        ("/api/v1/profiles", frozenset({"POST"})),
        ("/api/v1/me/profile", frozenset({"PATCH", "GET"})),
    ]
    assert _path_param_writes(routes) == []
    assert _disallowed_profile_writes(routes) == []
