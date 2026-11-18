"""テスト共通の環境変数（design/11-testing.md §12 / design/10-infra-ops.md §11）。

`ENV=test` のときだけ許される軽量 Argon2 とダミー pepper を定義する。
API / 結合テストは `tests/api/conftest.py` の `harness` fixture 経由で環境変数に反映する。
"""

import base64

PEPPER = b"test-pepper-" + b"x" * 32
ENV_PEPPER = base64.urlsafe_b64encode(PEPPER).decode()
PUBLIC_BASE_URL = "https://testserver"

TEST_ENV: dict[str, str] = {
    "APP_PEPPER": ENV_PEPPER,
    "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    "ARGON2_TIME_COST": "1",
    "ARGON2_MEMORY_KIB": "8192",
    "ARGON2_PARALLELISM": "1",
}
