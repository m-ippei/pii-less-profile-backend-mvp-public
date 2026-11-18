"""API パス接頭辞 `/api/v1` の回帰テスト（design/05-api-contract.md §6 共通規約）。

T-08-01 の完了条件を、結線済み `main.app` への実 HTTP で観測する。

- API は `/api/v1` 配下（`POST /api/v1/profiles` が 201）
- 旧接頭辞なしの `POST /profiles` は 404 `NOT_FOUND`（同一 JSON 形式）
- ページ扱いの `GET /healthz` はルート直下のまま 200
"""

from tests.support.http import Harness, post_json_with_csrf


def test_api_is_served_under_api_v1(harness: Harness) -> None:
    """`POST /api/v1/profiles` が 201 で到達すること。"""
    response = post_json_with_csrf(harness.client, "/api/v1/profiles")

    assert response.status_code == 201
    assert set(response.json()) == {
        "public_id",
        "login_id",
        "initial_password",
        "recovery_key",
    }


def test_legacy_unprefixed_path_is_not_found(harness: Harness) -> None:
    """旧 `POST /profiles` が 404 `NOT_FOUND`（同一 JSON 形式）になること。"""
    response = post_json_with_csrf(harness.client, "/profiles")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "リソースが見つかりません"}
    }
    assert response.headers["cache-control"] == "no-store"


def test_healthz_stays_at_root(harness: Harness) -> None:
    """`GET /healthz` はページ扱いでルート直下のまま 200 であること（AC-14）。"""
    response = harness.client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
