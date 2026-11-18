"""`tests/security` 共通の fixture（C-1）。

API と同じ結線済み `TestClient`（一時DB）を使う。ビルダーは `tests/support/harness.py`。
"""

from pathlib import Path

import pytest

from tests.support.harness import build_harness
from tests.support.http import Harness


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """環境変数と DB パスをテスト用に設定し、結線済みアプリの `TestClient` を返す。"""
    return build_harness(tmp_path, monkeypatch)
