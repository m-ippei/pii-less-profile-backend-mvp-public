"""結線済みアプリの `TestClient` と一時DB を用意する共通ビルダー（C-1）。

`tests/api` と `tests/security` の conftest から使う。テスト以外から import しない。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.main import app
from tests.support import env
from tests.support.http import Harness
from tests.support.sqlite_identity import migrated_db_path


def build_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """環境変数と DB パスをテスト用に設定し、結線済みアプリの `TestClient` を返す。"""
    path = migrated_db_path(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(path))
    for key, value in env.TEST_ENV.items():
        monkeypatch.setenv(key, value)
    return Harness(client=TestClient(app, base_url=env.PUBLIC_BASE_URL), db_path=path)
