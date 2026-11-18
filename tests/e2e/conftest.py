"""E2E（Playwright）の共通 fixture。

根拠: plan/archive/proposals/e2e-playwright-design.md §5.6/§6 / design/11-testing.md §12。
"""

import os
import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Page

CSRF_COOKIE = "__Host-csrf"


@pytest.fixture(scope="session", autouse=True)
def headed_start_delay() -> None:
    """目視確認モードで noVNC 接続の時間を稼ぐための開始遅延（D-05 運用補助）。

    `E2E_HEADED_START_DELAY`（秒）が設定されているときだけ、テスト開始前に待つ。
    `compose.e2e-headed.yaml` が既定値を与え、shell 環境変数で上書きできる。
    """
    raw = (os.environ.get("E2E_HEADED_START_DELAY") or "").strip()
    try:
        delay = float(raw) if raw else 0.0
    except ValueError:
        print(f"E2E_HEADED_START_DELAY を数値として解釈できません: {raw!r}（遅延なしで続行します）")
        delay = 0.0
    if delay > 0:
        time.sleep(delay)


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ["E2E_BASE_URL"]


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    # Caddy local CA の証明書はブラウザに信頼登録しない。HTTPS自体は保たれ、__Host- cookie も有効。
    return {**browser_context_args, "ignore_https_errors": True, "locale": "ja-JP"}


@pytest.fixture()
def create_profile(page: Page) -> Callable[[], dict[str, str]]:
    """ブラウザの実 CSRF 経路で `POST /api/v1/profiles` を叩くヘルパーを返す。

    `/healthz` を開いて `__Host-csrf` cookie を得てから `fetch` で作成し、返った秘密値を
    `secrets.js` の `stashSecrets`（`/recovery` で読み込み）で `sessionStorage` に預ける。
    返り値はアカウント作成応答の本文（`login_id` / `initial_password` / `recovery_key`）。
    `plan/archive/proposals/e2e-playwright-design.md` §6 の方針に従う。
    """

    def _create() -> dict[str, str]:
        page.goto("/healthz")
        # `__Host-csrf` は HttpOnly ではないので JS から読める。`context.cookies()` は
        # `Cookie` TypedDict の任意キーへの `[]` アクセスになり型検査に引っかかるため、
        # ブラウザ内で読む（値は base64url で cookie 安全）。
        result = page.evaluate(
            """async (marker) => {
                 const entry = document.cookie
                   .split("; ")
                   .find((part) => part.startsWith(marker));
                 const csrf = entry ? entry.slice(marker.length) : "";
                 const response = await fetch("/api/v1/profiles", {
                   method: "POST",
                   headers: {
                     "Content-Type": "application/json",
                     "X-CSRF-Token": csrf
                   },
                   body: "{}"
                 });
                 return { status: response.status, body: await response.json() };
               }""",
            f"{CSRF_COOKIE}=",
        )
        assert result["status"] == 201, result
        secrets = result["body"]
        page.goto("/recovery")
        page.evaluate(
            """(secrets) => window.AP.stashSecrets({
                 login_id: secrets.login_id,
                 password: secrets.initial_password,
                 recovery_key: secrets.recovery_key,
                 next: "/login"
               })""",
            secrets,
        )
        return secrets

    return _create
