"""【環境確認用】Image内で言語・DB・依存が揃っているかを確認する最小テスト。"""

import sqlite3
import sys
import uuid

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from src.main import app


def test_runtime_versions() -> None:
    assert sys.version_info >= (3, 14)
    assert sqlite3.sqlite_version_info >= (3, 37, 0)  # STRICT表に必要
    assert hasattr(uuid, "uuid7")  # Python 3.14標準


def test_argon2_works() -> None:
    ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    assert ph.verify(ph.hash("dummy-password"), "dummy-password")


def test_healthz_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    client = TestClient(app, base_url="https://testserver")
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert res.headers["cache-control"] == "no-store"


def test_healthz_unavailable_without_db_path(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/healthz").status_code == 503
