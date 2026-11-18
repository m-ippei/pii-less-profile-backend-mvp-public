"""T-13-03: 到達可能なルート一覧が API 契約（design/05-api-contract.md §6）と一致し、
設計に無い API・画面や MVP外機能の API が存在しないことを**構造的**に監査する。

- 設計に無い API・画面が無い（無断追加・MVP外の不在）
- 設計にある API・画面が存在する
- `/api/v1` 配下が API、ルート直下がページという規約を維持している

MVP の振る舞い（status/body/Cookie）は `api` / `e2e` 層が担う。ここでは「どのルートが
存在するか」だけを確認する（`design/11-testing.md` §12 の architecture 層）。
`design/01-overview.md` §2 MVP外 / `design/14-dod-conventions.md` §16。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace

from src.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
API_CONTRACT = REPO_ROOT / "design" / "05-api-contract.md"

API_PREFIX = "/api/v1"

# 設計の API 契約表・Web画面表に無いが許容する運用用エンドポイント（開発専用の環境診断）。
# `src/main.py` で `ENV=dev` 以外は 404。API 契約ではないためページ側から除外して扱う
# （`plan/log/decisions.md`「ページの扱い」）。
OPERATIONAL_ENDPOINTS = frozenset({("GET", "/_envcheck")})

# MVP外機能がルートに現れないことの確認に使う語（`design/01-overview.md` §2 MVP外）。
FORBIDDEN_PATH_TOKENS = (
    "offer",
    "oauth",
    "openid",
    "passkey",
    "mcp",
    "agent",
    "admin",
    "report",
    "block",
    "follow",
    "reply",
    "message",
    "invite",
    "enterprise",
    "username",
    "email",
    "search",
)

# `design/05-api-contract.md` §6 の表の行（`` `METHOD /path` ``）。
_ROW = re.compile(r"^\|\s*`(GET|POST|PATCH|PUT|DELETE)\s+(/[^\s`]*)`\s*\|", re.MULTILINE)

Route = tuple[str, str]


def _read_contract() -> str:
    return API_CONTRACT.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """`heading` から次の見出し（`##` / `###`）までの本文を返す。"""
    start = text.index(heading)
    rest = text[start + len(heading) :]
    match = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest if match is None else rest[: match.start()]


def parse_contract(text: str) -> tuple[frozenset[Route], frozenset[Route]]:
    """設計書の API 表（`/api/v1` 接頭辞を補う）と Web画面表を行の集合で返す。"""
    api = frozenset(
        (method, API_PREFIX + path)
        for method, path in _ROW.findall(_section(text, "### エンドポイント"))
    )
    pages = frozenset(_ROW.findall(_section(text, "### Web画面")))
    return api, pages


def iter_routes(
    routes: Iterable[object], seen: set[int] | None = None
) -> Iterator[tuple[str, str]]:
    """ルート表を走査する。`include_router` で内包されたルーター（`original_router`）も辿る。

    メソッドを持たない `Mount`（`/static`）は対象外。`HEAD` / `OPTIONS` は公開契約に
    含めない。
    """
    seen = set() if seen is None else seen
    for route in routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or ()
        if path and methods:
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                yield method, path
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            child_routes = getattr(child, "routes", None) if child is not None else None
            if not child_routes or id(child) in seen:
                continue
            seen.add(id(child))
            yield from iter_routes(child_routes, seen)


def _app_routes() -> frozenset[Route]:
    return frozenset(iter_routes(app.routes))


def missing_routes(expected: Iterable[Route], actual: Iterable[Route]) -> list[str]:
    """設計にあるが実装に無いルートを返す。"""
    actual_set = set(actual)
    return sorted(
        f"{method} {path}" for method, path in expected if (method, path) not in actual_set
    )


def unexpected_api_routes(actual: Iterable[Route], expected: Iterable[Route]) -> list[str]:
    """`/api/v1` 配下で、設計の API 表に無いルートを返す。"""
    expected_set = set(expected)
    return sorted(
        f"{method} {path}"
        for method, path in actual
        if path.startswith(API_PREFIX) and (method, path) not in expected_set
    )


def unexpected_page_routes(actual: Iterable[Route], expected: Iterable[Route]) -> list[str]:
    """設計の Web画面表にも運用エンドポイントにも無い、API 以外のルートを返す。"""
    expected_set = set(expected)
    return sorted(
        f"{method} {path}"
        for method, path in actual
        if not path.startswith(API_PREFIX)
        and not path.startswith("/static")
        and (method, path) not in expected_set
        and (method, path) not in OPERATIONAL_ENDPOINTS
    )


def test_api_routes_match_the_contract() -> None:
    """`/api/v1` 配下のルートが設計の API 表と過不足なく一致すること。"""
    api, _ = parse_contract(_read_contract())
    actual = _app_routes()
    assert missing_routes(api, actual) == []
    assert unexpected_api_routes(actual, api) == []


def test_page_routes_match_the_contract() -> None:
    """API 以外のルートが設計の Web画面表と過不足なく一致すること。"""
    _, pages = parse_contract(_read_contract())
    actual = _app_routes()
    assert missing_routes(pages, actual) == []
    assert unexpected_page_routes(actual, pages) == []


def test_no_mvp_out_feature_routes() -> None:
    """MVP外機能の語を含むルートが存在しないこと（`design/01-overview.md` §2）。"""
    offenders = [
        f"{method} {path}"
        for method, path in _app_routes()
        if any(token in path.lower() for token in FORBIDDEN_PATH_TOKENS)
    ]
    assert offenders == []


def test_operational_endpoint_is_outside_the_api_contract() -> None:
    """`/_envcheck` は存在するが、設計の API/画面のいずれにも属さない運用用であること。"""
    api, pages = parse_contract(_read_contract())
    assert ("GET", "/_envcheck") in _app_routes()
    assert ("GET", "/_envcheck") not in api
    assert ("GET", "/_envcheck") not in pages


def test_detects_missing_and_unexpected_routes() -> None:
    """陽性対照: 欠落ルートと設計外 API を検出する。"""
    expected = frozenset({("GET", "/api/v1/a")})
    actual = frozenset({("GET", "/api/v1/b")})
    assert missing_routes(expected, actual) == ["GET /api/v1/a"]
    assert unexpected_api_routes(actual, expected) == ["GET /api/v1/b"]


def test_iter_routes_recurses_into_included_routers() -> None:
    """陽性対照: 内包ルーター（`original_router`）と `Mount` の扱い。"""
    leaf = SimpleNamespace(path="/api/v1/x", methods={"GET"})
    original = SimpleNamespace(routes=[leaf])
    included = SimpleNamespace(path=None, methods=None, original_router=original)
    mount = SimpleNamespace(path="/static", methods=None)
    assert list(iter_routes([included, mount])) == [("GET", "/api/v1/x")]
