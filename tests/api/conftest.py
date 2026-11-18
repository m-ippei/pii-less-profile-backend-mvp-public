"""`tests/api` 共通の fixture（C-1）。

実ポートが読む環境変数を、migration 適用済みの一時DBへ向ける。
`tests/api` 配下に限定し、別実行の `tests/e2e` へは波及させない。
"""

from pathlib import Path

import pytest

from tests.support.harness import build_harness
from tests.support.http import Harness


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """環境変数と DB パスをテスト用に設定し、結線済みアプリの `TestClient` を返す。"""
    return build_harness(tmp_path, monkeypatch)
