"""M08 Identity の E2E シナリオ（design/11-testing.md §12 / design/12-ux-secrets.md §13）。

`plan/archive/proposals/e2e-playwright-design.md` §6 の M08 シナリオ①②③④⑤を、
実ブラウザ（コンテナ内 Chromium）で JS 挙動と CSRF 連携として検証する。
API 網羅は `tests/api`（TestClient）に委ね、ここはスモークに留める。

- ① 作成 → `/credentials-saved`。確認チェック前は遷移不可、チェック後に遷移できる
- ② 未ログインで `/account` → `/login` へ遷移する
- ③ `/login` でログイン成功（実 JS の `X-CSRF-Token` 送信）
- ④ パスワード変更 → 保存確認画面（`recovery_key` のみ表示・旧キー失効の明示）
- ⑤ 回復 → 保存確認画面（`login_id` ＋ `recovery_key`）

rate limit 予算（`make e2e` 1回で作成 8件以内）を守るため、作成は各 1 件に抑える。
"""

import re
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect

BASE58 = r"[1-9A-HJ-NP-Za-km-z]"


def _open_authenticated(page: Page, path: str) -> None:
    """auth-guard の probe（`GET /api/v1/me/profile`）完了を待ってから画面を開く。

    `networkidle` は HTTP/2 の keep-alive 接続で解決しないことがあるため、probe の
    応答を明示的に待つ。応答の直後に JS の submit handler が付くので、短い待ちを足す。
    """
    with page.expect_response(lambda response: response.url.endswith("/api/v1/me/profile")):
        page.goto(path)
    page.wait_for_timeout(100)


def test_create_profile_then_credentials_saved_requires_confirmation(
    page: Page, create_profile
) -> None:
    """① 作成後、確認チェック前は遷移不可で、チェック後に次の画面へ進めること。"""
    secrets = create_profile()

    page.goto("/credentials-saved")

    expect(page.locator("#empty-message")).to_be_hidden()
    expect(page.locator("#secret-login-id-block")).to_be_visible()
    expect(page.locator("#secret-password-block")).to_be_visible()
    expect(page.locator("#secret-recovery-block")).to_be_visible()
    expect(page.locator("#old-key-note")).to_be_hidden()

    expect(page.locator("#login-id-value")).to_have_text(secrets["login_id"])
    expect(page.locator("#password-value")).to_have_text(secrets["initial_password"])
    expect(page.locator("#recovery-key-value")).to_have_text(
        f"{secrets['recovery_key']}_RECOVERY-KEY"
    )

    continue_button = page.locator("#continue")
    expect(continue_button).to_be_disabled()

    page.locator("#saved").check()
    expect(continue_button).to_be_enabled()

    continue_button.click()
    page.wait_for_url("**/login")
    expect(page.locator("#login-form")).to_be_visible()


def test_unauthenticated_account_redirects_to_login(page: Page) -> None:
    """② 未ログインで `/account` を開くと auth-guard が `/login` へ遷移すること。"""
    page.goto("/account")

    page.wait_for_url("**/login")
    expect(page.locator("#login-form")).to_be_visible()


def test_login_with_real_csrf_redirects_to_account(page: Page, create_profile) -> None:
    """③ `/login` の実 JS が `X-CSRF-Token` を送り、成功で `/account` へ進むこと。"""
    secrets = create_profile()

    page.goto("/login")
    page.fill("#login_id", secrets["login_id"])
    page.fill("#password", secrets["initial_password"])
    page.locator("#login-form button[type=submit]").click()

    page.wait_for_url("**/account")
    expect(page.locator("#change-password-form")).to_be_visible()


def test_change_password_shows_recovery_key_only(page: Page, create_profile) -> None:
    """④ パスワード変更成功で、`recovery_key` のみ表示し旧キー失効を明示すること（AC-6）。"""
    secrets = create_profile()

    _open_authenticated(page, "/account")
    page.fill("#current_password", secrets["initial_password"])
    page.fill("#new_password", "changed-password-123")
    page.locator("#change-password-form button[type=submit]").click()

    page.wait_for_url("**/credentials-saved")

    expect(page.locator("#secret-login-id-block")).to_be_hidden()
    expect(page.locator("#secret-password-block")).to_be_hidden()
    expect(page.locator("#secret-recovery-block")).to_be_visible()
    expect(page.locator("#old-key-note")).to_be_visible()
    expect(page.locator("#recovery-key-value")).to_have_text(
        re.compile(rf"^{BASE58}{{32}}_RECOVERY-KEY$")
    )

    continue_button = page.locator("#continue")
    expect(continue_button).to_be_disabled()
    page.locator("#saved").check()
    expect(continue_button).to_be_enabled()


def test_recovery_shows_login_id_and_recovery_key(page: Page, create_profile) -> None:
    """⑤ 回復成功で `login_id` と `recovery_key` を表示し、旧キー失効を明示すること（AC-7）。"""
    secrets = create_profile()

    page.goto("/recovery")
    page.fill("#recovery_key", secrets["recovery_key"])
    page.fill("#new_password", "recovered-password-123")
    page.locator("#recovery-form button[type=submit]").click()

    page.wait_for_url("**/credentials-saved")

    expect(page.locator("#secret-login-id-block")).to_be_visible()
    expect(page.locator("#secret-password-block")).to_be_hidden()
    expect(page.locator("#secret-recovery-block")).to_be_visible()
    expect(page.locator("#old-key-note")).to_be_visible()
    expect(page.locator("#login-id-value")).to_have_text(re.compile(rf"^{BASE58}{{24}}$"))
    expect(page.locator("#recovery-key-value")).to_have_text(
        re.compile(rf"^{BASE58}{{32}}_RECOVERY-KEY$")
    )

    expect(page.locator("#continue")).to_be_disabled()


def test_navigation_urls_keep_secrets_out(page: Page, create_profile) -> None:
    """AC-13（e2e 一次証拠）: 主要動線の各遷移後 URL に秘密値が載らない。

    秘密値はクライアントの `sessionStorage`（`secrets.js`）に預け、URL（path・query・
    fragment）には出さない（design/07-security.md §8 不変規則 / design/12-ux-secrets.md §13）。
    """
    secrets = create_profile()
    secret_values = [
        secrets["login_id"],
        secrets["initial_password"],
        secrets["recovery_key"],
    ]

    def stored_secrets() -> str:
        return page.evaluate("() => window.sessionStorage.getItem('ap_secrets') || ''")

    def assert_url_is_clean() -> None:
        parts = urlsplit(page.url)
        assert parts.query == ""
        assert parts.fragment == ""
        for value in secret_values:
            assert value not in page.url

    # 作成直後: 秘密値は sessionStorage にあり、URL には無い。
    assert secrets["recovery_key"] in stored_secrets()
    assert_url_is_clean()

    page.goto("/credentials-saved")
    assert_url_is_clean()
    page.locator("#saved").check()
    page.locator("#continue").click()
    page.wait_for_url("**/login")
    assert_url_is_clean()

    page.fill("#login_id", secrets["login_id"])
    page.fill("#password", secrets["initial_password"])
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_url("**/account")
    expect(page.locator("#change-password-form")).to_be_visible()
    assert_url_is_clean()

    page.fill("#current_password", secrets["initial_password"])
    page.fill("#new_password", "changed-password-123")
    page.locator("#change-password-form button[type=submit]").click()
    page.wait_for_url("**/credentials-saved")
    assert_url_is_clean()
