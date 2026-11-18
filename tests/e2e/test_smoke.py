"""E2E 基盤スモーク（D-02）。

HTTPS・Origin一致・`__Host-csrf` 発行・セキュリティヘッダーを、実ブラウザ経由で1本で確認する。

根拠: plan/archive/proposals/e2e-playwright-design.md §5.6 / design/11-testing.md §12。
"""


def test_healthz_over_https_sets_csrf_cookie(page, context) -> None:
    res = page.goto("/healthz")
    assert res is not None and res.status == 200
    assert res.json() == {"status": "ok"}
    assert res.headers["x-content-type-options"] == "nosniff"

    csrf = {c["name"]: c for c in context.cookies()}["__Host-csrf"]
    assert csrf["secure"] is True
    assert csrf["path"] == "/"
    assert csrf["httpOnly"] is False  # JSが読む設計
