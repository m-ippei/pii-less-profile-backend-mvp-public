"""認証系 Web 画面の HTTP 契約テスト（design/05-api-contract.md §6 Web画面 /
design/11-testing.md §12）。

`T-08-07` / `T-09-03` の完了条件のうち、HTTP 契約（status / content-type / Cache /
Cookie）として観測できるものだけを検証する。HTML/JS の文字列一致や参照の存在確認は
行わない（`design/11-testing.md` §12: `static` は不在検証のみ。画面の振る舞いは e2e）。

- `GET /new` `/login` `/profile-editor` `/account` `/recovery` `/credentials-saved` が
  200・`text/html`・`no-store`
- `GET /profiles/{public_id}`（公開ページの殻）が 200・`text/html`・`no-store` で、
  `public_id` の値にかかわらず同一の殻を返し入力を echo しない（値の判定は JS 側 = T-09-05 e2e）
- `/static` のアセットが配信され、`Cache-Control` は上書きされない（唯一の例外）
- `GET` 応答で `__Host-csrf` cookie が発行される
"""

from fastapi.testclient import TestClient

from src.app.http.cookies import CSRF_COOKIE
from src.main import app
from tests.support.http import TEST_ORIGIN

PAGES = ("/new", "/login", "/profile-editor", "/account", "/recovery", "/credentials-saved")


def _client() -> TestClient:
    return TestClient(app, base_url=TEST_ORIGIN)


def test_pages_return_html_and_no_store() -> None:
    """各画面が 200・`text/html`・`Cache-Control: no-store` で返ること。"""
    client = _client()
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.headers["cache-control"] == "no-store", path


def test_pages_issue_csrf_cookie_on_get() -> None:
    """`GET` 応答で `__Host-csrf` cookie が発行されること（design/07-security.md §8）。"""
    client = _client()
    response = client.get("/login")
    assert response.status_code == 200
    assert any(
        cookie.startswith(f"{CSRF_COOKIE}=") for cookie in response.headers.get_list("set-cookie")
    )


def test_public_profile_page_returns_html_and_no_store() -> None:
    """公開ページの殻 `GET /profiles/{public_id}` が 200・`text/html`・`no-store`。

    `design/05-api-contract.md` §6 Web画面。値の有無の判定は JS が API を呼んで行うため、
    サーバーは常に同じ静的 HTML を返す。
    """
    response = _client().get("/profiles/p_1111111111111111")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


def test_public_profile_page_does_not_echo_public_id() -> None:
    """形式違反・不存在の `public_id` でも同一の殻を返し、入力を echo しない。

    `design/07-security.md` §8 不変規則5。公開ページは静的な殻であり、`public_id` の
    形式・状態の判定は API と JS 側に委ねる（AC-2 は api 層、AC-12 は e2e）。
    """
    client = _client()
    malformed = "not-a-public-id"
    valid_shell = client.get("/profiles/p_1111111111111111")
    malformed_shell = client.get(f"/profiles/{malformed}")

    assert valid_shell.status_code == malformed_shell.status_code == 200
    assert valid_shell.text == malformed_shell.text
    assert malformed not in malformed_shell.text


def test_static_js_is_served() -> None:
    """`/static/js/csrf.js` が JavaScript として配信されること。"""
    response = _client().get("/static/js/csrf.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_static_asset_cache_control_is_not_overridden() -> None:
    """`/static/` のアセットは `no-store` に上書きされないこと（design/07-security.md §8）。"""
    response = _client().get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers.get("cache-control") != "no-store"
