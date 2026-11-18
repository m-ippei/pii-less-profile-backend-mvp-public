"""M09 Profile の E2E シナリオ（design/11-testing.md §12 / design/05-api-contract.md §6 Web画面）。

`plan/archive/proposals/test-strategy-review.md` §3.4 の M09 分を、実ブラウザ（コンテナ内
Chromium）で検証する。特に AC-12（本文の `<script>` が公開ページで実行されない）は、
静的走査では証明にならないため **e2e を一次証拠**とする（同 proposal §1.4 P-2 / §3.4）。
API 網羅は `tests/api` に委ね、ここは主要動線と UI 分岐に限る。

rate limit 予算（`make e2e` 1回で作成 8件以内）を守るため、作成は各 1 件に抑える。

二重送信防止（送信中のボタン無効化）は、headed（`--slowmo`）で外から disabled を観測すると
遷移と競合するため、ブラウザ内で同期的に `.click()` を2回呼び、1回目直後の `disabled` と
送信回数で確認する。
"""

from playwright.sync_api import Browser, BrowserContext, Page, expect

NOT_FOUND_MESSAGE = "プロフィールが見つかりません"
NOT_PUBLISHABLE_MESSAGE = "公開できる内容がありません"


def _viewer_context(browser: Browser, base_url: str) -> BrowserContext:
    """未ログインの第三者を模す独立コンテキスト（cookie を共有しない）。"""
    return browser.new_context(base_url=base_url, ignore_https_errors=True, locale="ja-JP")


def _open_editor(page: Page) -> None:
    """auth-guard の probe（`GET /api/v1/me/profile`）完了を待ってから編集画面を開く。"""
    with page.expect_response(lambda response: response.url.endswith("/api/v1/me/profile")):
        page.goto("/profile-editor")
    page.wait_for_timeout(100)


def _click_create_twice(page: Page) -> bool:
    """`/new` の作成ボタンを同一ターンで2回押し、1回目で無効化されたかを返す。

    2回目の `.click()` は無効化済みのため無視される（送信は1回だけ）。
    """
    return page.evaluate(
        """() => {
          const button = document.getElementById("create");
          button.click();
          const disabled = button.disabled;
          button.click();
          return disabled;
        }"""
    )


def test_create_publish_and_third_party_view(page: Page, browser: Browser, base_url: str) -> None:
    """主要動線: `/new` 作成 → 保存確認ゲート → 公開 → 第三者が閲覧 → unpublish 即時反映。

    送信中のボタン無効化（二重送信防止・作成と保存）もあわせて観測する。
    """
    post_calls: list[str] = []

    def count_create(route, request) -> None:
        post_calls.append(route.request.url)
        route.continue_()

    def count_update(route, request) -> None:
        route.continue_()

    page.route("**/api/v1/profiles", count_create)
    page.route("**/api/v1/me/profile", count_update)

    page.goto("/new")
    assert _click_create_twice(page) is True
    page.wait_for_url("**/credentials-saved")
    assert len(post_calls) == 1

    continue_button = page.locator("#continue")
    expect(continue_button).to_be_disabled()
    page.locator("#saved").check()
    expect(continue_button).to_be_enabled()

    with page.expect_response(lambda response: response.url.endswith("/api/v1/me/profile")):
        continue_button.click()
    page.wait_for_url("**/profile-editor")
    page.wait_for_timeout(100)

    page.fill("#content", "こんにちは、世界")
    page.locator("#published").check()
    save_button = page.locator("#profile-form button[type=submit]")
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/me/profile") and response.request.method == "PATCH"
        )
    ) as response_info:
        disabled_while_saving = page.evaluate(
            """() => {
              const form = document.getElementById("profile-form");
              const button = form.querySelector("button[type=submit]");
              form.requestSubmit();
              return button.disabled;
            }"""
        )
    assert disabled_while_saving is True
    public_id = response_info.value.json()["public_id"]
    expect(page.locator("#form-status")).to_have_text("保存しました")

    viewer_context = _viewer_context(browser, base_url)
    try:
        viewer = viewer_context.new_page()
        viewer.goto(f"/profiles/{public_id}")
        expect(viewer.locator("#profile")).to_have_text("こんにちは、世界")

        page.locator("#published").uncheck()
        with page.expect_response(
            lambda response: (
                response.url.endswith("/api/v1/me/profile") and response.request.method == "PATCH"
            )
        ):
            save_button.click()
        expect(page.locator("#form-status")).to_have_text("保存しました")

        viewer.reload()
        expect(viewer.locator("#profile-error")).to_have_text(NOT_FOUND_MESSAGE)
        expect(viewer.locator("#profile")).to_be_empty()
    finally:
        viewer_context.close()


def test_script_in_content_is_not_executed_on_public_page(
    page: Page, browser: Browser, base_url: str, create_profile
) -> None:
    """AC-12（一次証拠）: 本文の `<script>` が公開ページで実行されない。

    `textContent` 描画のため、本文中の `<script>` は実行されず文字列のまま表示される。
    """
    create_profile()
    _open_editor(page)

    page.fill("#content", "<script>window.__xss = true;</script>公開本文")
    page.locator("#published").check()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/me/profile") and response.request.method == "PATCH"
        )
    ) as response_info:
        page.locator("#profile-form button[type=submit]").click()
    public_id = response_info.value.json()["public_id"]

    viewer_context = _viewer_context(browser, base_url)
    try:
        viewer = viewer_context.new_page()
        viewer.goto(f"/profiles/{public_id}")
        expect(viewer.locator("#profile")).to_contain_text("公開本文")
        assert viewer.evaluate("() => window.__xss") is None
        assert viewer.locator("#profile script").count() == 0
    finally:
        viewer_context.close()


def test_profile_editor_requires_login(page: Page) -> None:
    """未ログインで `/profile-editor` を開くと auth-guard が `/login` へ遷移する。"""
    page.goto("/profile-editor")

    page.wait_for_url("**/login")
    expect(page.locator("#login-form")).to_be_visible()


def test_publish_empty_content_shows_fixed_message(page: Page, create_profile) -> None:
    """空 content での公開は 422 の固定文言を表示する（H-15、入力値を echo しない）。"""
    create_profile()
    _open_editor(page)

    page.locator("#published").check()
    page.locator("#profile-form button[type=submit]").click()

    error = page.locator("#form-error")
    expect(error).to_be_visible()
    expect(error).to_have_text(NOT_PUBLISHABLE_MESSAGE)
