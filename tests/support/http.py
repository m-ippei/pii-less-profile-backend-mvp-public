"""TestClient で CSRF を通すためのヘルパー（design/07-security.md §8 CSRF）。

CSRF 検証は「`Origin` 一致・JSON・`X-CSRF-Token` と `__Host-csrf` cookie の一致」の
3条件を要する。テストは `GET` で cookie を発行させてから、非 `GET` にヘッダーを付ける。

テスト以外から import してはいけない。
"""

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from httpx2 import Response

from src.app.http.cookies import CSRF_COOKIE

TEST_ORIGIN = "https://testserver"


@dataclass
class Harness:
    """結線済み `main.app` の `TestClient` と、その DB パス。"""

    client: TestClient
    db_path: Path


def prime_csrf(client: TestClient) -> str:
    """`__Host-csrf` cookie を発行させ、その値を返す。"""
    client.get("/healthz")
    return client.cookies[CSRF_COOKIE]


def csrf_headers(client: TestClient) -> dict[str, str]:
    """非 `GET` リクエストに付ける CSRF ヘッダー。"""
    return {"Origin": TEST_ORIGIN, "X-CSRF-Token": prime_csrf(client)}


def post_json_with_csrf(client: TestClient, path: str) -> Response:
    """空 JSON `{}` を CSRF ヘッダー付きで POST する。"""
    return client.post(path, json={}, headers=csrf_headers(client))
