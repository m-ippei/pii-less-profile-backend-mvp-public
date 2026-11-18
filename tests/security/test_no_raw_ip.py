"""AC-8: IP アドレスの生値が DB に存在しない（`design/07-security.md` §8「不変規則」/
`design/15-logging.md` §17「ログ」/ `design/11-testing.md` §12 security 層 /
`design/08-database.md` §9）。

代表的な動線（作成・ログイン・パスワード変更・回復）を、既知の生IPを `X-Forwarded-For` で
与えて実行し、一時DBダンプにその生IPが現れないことを確認する。あわせてスキーマに IP 列が
無いこと、IP はレート制限のインメモリ key にだけ現れ DB へ書かれないことを確認する。
"""

from pathlib import Path

import src.main as main
from src.shared.db import connect
from tests.support.http import Harness, csrf_headers

RAW_IP = "203.0.113.77"
TABLES = ("users", "login_credentials", "recovery_credentials", "profiles", "sessions")


def _dump_database(db_path: Path) -> str:
    connection = connect(db_path)
    try:
        lines: list[str] = []
        for table in TABLES:
            for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                cells = ("" if value is None else str(value) for value in tuple(row))
                lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        connection.close()


def _columns(db_path: Path) -> list[str]:
    connection = connect(db_path)
    try:
        names: list[str] = []
        for table in TABLES:
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall():
                names.append(str(row[1]))
        return names
    finally:
        connection.close()


def _headers(harness: Harness) -> dict[str, str]:
    """CSRF を通し、生IPを `X-Forwarded-For` で与えるヘッダー。"""
    return {**csrf_headers(harness.client), "X-Forwarded-For": RAW_IP}


def _run_flows(harness: Harness) -> None:
    """作成・ログイン・パスワード変更・回復を、生IP付きで一通り実行する。"""
    client = harness.client
    created_response = client.post("/api/v1/profiles", json={}, headers=_headers(harness))
    assert created_response.status_code == 201
    created = created_response.json()

    login = {"login_id": created["login_id"], "password": created["initial_password"]}
    assert client.post("/api/v1/sessions", json=login, headers=_headers(harness)).status_code == 204

    changed = client.post(
        "/api/v1/me/password",
        json={
            "current_password": created["initial_password"],
            "new_password": "changed-password-123",
        },
        headers=_headers(harness),
    )
    assert changed.status_code == 200

    recovered = client.post(
        "/api/v1/recovery/password-resets",
        json={
            "recovery_key": changed.json()["recovery_key"],
            "new_password": "recovered-password-123",
        },
        headers=_headers(harness),
    )
    assert recovered.status_code == 200


def test_flows_use_the_raw_ip_without_storing_it(harness: Harness, monkeypatch) -> None:
    """動線実行後のDBダンプに生IPが存在しないこと（AC-8）。

    IP はレート制限のインメモリ key にだけ現れ、DB へは書かれない。
    """
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")

    _run_flows(harness)

    assert RAW_IP not in _dump_database(harness.db_path)
    # インメモリのレート制限 key には生IPが使われている（DB ではなくメモリのみ）。
    assert main.rate_limiter.attempt_count(f"profile-create:{RAW_IP}") >= 1


def test_schema_has_no_ip_column(harness: Harness) -> None:
    """どのテーブルにも IP 列が無いこと（AC-8 / design/08-database.md §9）。"""
    columns = _columns(harness.db_path)
    assert columns
    assert [name for name in columns if "ip" in name.lower()] == []
