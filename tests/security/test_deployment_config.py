"""デプロイ構成のセキュリティ不変条件（Docker / reverse proxy / graceful shutdown / デプロイ基盤）。

根拠: design/10-infra-ops.md §11「実行モデルとDB」「Docker」/ design/07-security.md §8
「応答ヘッダー（HSTS はリバースプロキシで付与）」/ design/16-deployment.md §18
「デプロイ・リリース・運用基盤」/ design/15-logging.md §17「journald の tag・保持」。

設定ファイルは実行時に test コンテナへ read-only でマウントされる。ここでは値の整合を
検証する（不在検証を含む）。`src/` の挙動テストではない。

観測すること:
1. Dockerfile: 非root・`TZ=UTC`・runtime が dev 依存を含まない
2. compose.yaml: `read_only`・`cap_drop: [ALL]`・`no-new-privileges`・SQLite が named volume
3. Caddyfile（proxy / e2e）: HSTS を付与し、アクセスログ（`log` ディレクティブ）を出さない
4. graceful shutdown: `stop_grace_period` が uvicorn の `--timeout-graceful-shutdown` より長い
5. compose.deploy.yaml: `app.image` が digest 必須（`${APP_IMAGE:?}`）・journald tag・IPv4 明示
6. workflows: release が runtime を build し digest を表示、deploy が staging 一致を要求
7. deploy スクリプト: 存在・実行ビット・通知/ping が空設定で無効
8. `.gitattributes`: デプロイ資産（sh/yaml/service/timer/conf/example/Caddyfile）を LF 固定
9. compose.yaml: Caddy イメージが digest 固定（TODO なし）、Dependabot が docker ecosystem を監視
10. journald ホスト設定: persistent・`MaxRetentionSec=30day`・`SystemMaxUse=1G` 等
11. systemd timer: backup / monitor が日次で対応 service を起動する
12. backup.sh: `.backup`/`integrity_check`/`gzip`/`age`/`rclone`・`REQUIRE_OFFSITE` の fail-closed
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "compose.yaml"
CADDYFILES = (
    REPO_ROOT / "deploy" / "proxy" / "Caddyfile",
    REPO_ROOT / "deploy" / "e2e" / "Caddyfile",
)
COMPOSE_DEPLOY = REPO_ROOT / "compose.deploy.yaml"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
DEPLOY_SCRIPTS = (
    REPO_ROOT / "deploy" / "deploy.sh",
    REPO_ROOT / "deploy" / "backup.sh",
    REPO_ROOT / "deploy" / "restore.sh",
    REPO_ROOT / "deploy" / "monitor.sh",
)
JOURNALD_CONF = REPO_ROOT / "deploy" / "host" / "journald-anon-profile.conf"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "backup.sh"
RESTORE_SCRIPT = REPO_ROOT / "deploy" / "restore.sh"

SYSTEMD_UNITS = (
    ("anon-profile-backup.timer", "anon-profile-backup.service", "deploy/backup.sh"),
    ("anon-profile-monitor.timer", "anon-profile-monitor.service", "deploy/monitor.sh"),
)

USER_NON_ROOT = "USER 10001:10001"
CADDY_ACCESS_LOG = re.compile(r"(?m)^\s*log(\s|$)")
GRACEFUL_TIMEOUT = re.compile(r'"--timeout-graceful-shutdown",\s*"(\d+)"')
STOP_GRACE = re.compile(r"stop_grace_period:\s*(\d+)s")
IMAGE_REQUIRED = re.compile(r"\$\{APP_IMAGE:\?")
CADDY_IMAGE_PINNED = re.compile(r"(?m)^\s*image:\s*caddy:[^\s#]*@sha256:[0-9a-f]{64}\s*$")
LF_FIXED_PATTERNS = (
    "*.sh",
    "*.yaml",
    "*.yml",
    "*.service",
    "*.timer",
    "*.conf",
    "*.example",
    "Caddyfile",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def runtime_stage(dockerfile: str) -> str:
    """`runtime` ステージの本文（`base AS runtime` から次の `FROM` まで）。"""
    return dockerfile.split("FROM base AS runtime", 1)[1].split("\nFROM ", 1)[0]


def test_dockerfile_runs_non_root_with_utc_and_no_dev_dependencies() -> None:
    dockerfile = read(DOCKERFILE)
    assert "TZ=UTC" in dockerfile
    assert USER_NON_ROOT in dockerfile

    runtime = runtime_stage(dockerfile)
    assert USER_NON_ROOT in runtime
    assert "requirements-dev.txt" not in runtime


def test_compose_hardens_the_app_service() -> None:
    compose = read(COMPOSE)
    assert "read_only: true" in compose
    assert "cap_drop: [ALL]" in compose
    assert "no-new-privileges:true" in compose
    assert "app-data:/data" in compose


def test_compose_pins_the_caddy_image_by_digest() -> None:
    compose = read(COMPOSE)
    # Caddy は可変タグではなく digest 固定（design/16 §18「HSTS とイメージ固定」）。
    assert "TODO" not in compose, "compose.yaml に TODO が残っている（Caddy の digest 固定）"
    assert CADDY_IMAGE_PINNED.search(compose) is not None, (
        "reverse-proxy.image を caddy:<tag>@sha256:<64桁hex> で固定する"
    )


def test_dependabot_watches_docker_for_caddy_digest_updates() -> None:
    dependabot = read(DEPENDABOT)
    assert "package-ecosystem: docker" in dependabot, "Caddy digest の更新を Dependabot で受ける"


def test_caddy_sets_hsts_and_disables_access_log() -> None:
    for path in CADDYFILES:
        caddy = read(path)
        assert "Strict-Transport-Security" in caddy, path
        assert CADDY_ACCESS_LOG.search(caddy) is None, path


def test_graceful_shutdown_window_outlives_the_uvicorn_drain() -> None:
    timeout = GRACEFUL_TIMEOUT.search(read(DOCKERFILE))
    stop = STOP_GRACE.search(read(COMPOSE))
    assert timeout is not None
    assert stop is not None
    assert int(stop.group(1)) > int(timeout.group(1))


def test_compose_deploy_pins_digest_and_journald_logging() -> None:
    deploy = read(COMPOSE_DEPLOY)
    assert IMAGE_REQUIRED.search(deploy) is not None, "app.image は digest 必須（${APP_IMAGE:?}）"
    assert "driver: journald" in deploy
    assert "tag: anon-profile-app" in deploy
    assert "tag: anon-profile-proxy" in deploy
    # IPv6 を docker-proxy 経由で受けるとクライアントIPが同一になり IP 単位 rate limit が壊れる。
    assert '"0.0.0.0:80:80"' in deploy
    assert '"0.0.0.0:443:443"' in deploy
    assert '"0.0.0.0:443:443/udp"' in deploy


def test_release_workflow_checks_builds_runtime_and_reports_digest() -> None:
    workflow = read(WORKFLOWS / "release.yaml")
    assert "make check" in workflow
    assert "target: runtime" in workflow
    assert "provenance: false" in workflow
    assert "steps.push.outputs.digest" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow


def test_deploy_workflow_guards_production_with_staging_digest() -> None:
    workflow = read(WORKFLOWS / "deploy.yaml")
    assert "workflow_dispatch" in workflow
    assert "skip_staging_check" in workflow
    assert "deploy.sh current" in workflow
    assert "deploy.sh deploy" in workflow


def test_cleanup_workflow_and_dependabot_present() -> None:
    cleanup = read(WORKFLOWS / "cleanup.yaml")
    assert "workflow_dispatch" in cleanup
    assert "delete-package-versions" in cleanup
    assert "github-actions" in read(DEPENDABOT)


def test_deploy_scripts_exist_and_are_executable() -> None:
    for path in DEPLOY_SCRIPTS:
        assert path.is_file(), path
        assert os.access(path, os.X_OK), path


def test_alerting_is_disabled_when_urls_are_unset() -> None:
    backup = read(REPO_ROOT / "deploy" / "backup.sh")
    monitor = read(REPO_ROOT / "deploy" / "monitor.sh")
    # 空なら送信しない（設定が無くても backup / monitor は正常動作する）。
    assert "${BACKUP_PING_URL:-}" in backup
    assert "${NOTIFY_URL:-}" in monitor
    assert "${MONITOR_PING_URL:-}" in monitor


def test_gitattributes_pins_lf_for_deploy_assets() -> None:
    attributes = read(GITATTRIBUTES)
    for pattern in LF_FIXED_PATTERNS:
        expected = re.compile(rf"(?m)^{re.escape(pattern)}\s+text\s+eol=lf\s*$")
        assert expected.search(attributes) is not None, pattern


def test_journald_conf_matches_designed_retention() -> None:
    conf = read(JOURNALD_CONF)
    # 保持は ip_daily の30日と一致（design/16 §18「ログ（journald）」/ design/15 §17）。
    for setting in (
        "Storage=persistent",
        "MaxRetentionSec=30day",
        "MaxFileSec=1day",
        "SystemMaxUse=1G",
        "SystemKeepFree=1G",
        "Compress=yes",
    ):
        assert setting in conf, setting


def test_systemd_timers_wire_daily_backup_and_monitor() -> None:
    for timer_name, service_name, exec_target in SYSTEMD_UNITS:
        timer = read(SYSTEMD_DIR / timer_name)
        service = read(SYSTEMD_DIR / service_name)
        assert "OnCalendar=" in timer, timer_name
        assert "WantedBy=timers.target" in timer, timer_name
        assert f"/opt/anon-profile/{exec_target}" in service, service_name


def test_backup_script_encrypts_with_age_and_requires_offsite() -> None:
    script = read(BACKUP_SCRIPT)
    # 稼働中でも整合する .backup と integrity_check、gzip、age（公開鍵のみ）で暗号化する。
    assert ".backup" in script
    assert "integrity_check" in script
    assert "gzip" in script
    assert "age -r " in script
    # オフサイトは rclone。REQUIRE_OFFSITE=1 で RCLONE_REMOTE 未設定なら異常終了（fail-closed）。
    assert "rclone copyto" in script
    assert 'elif [[ "$REQUIRE_OFFSITE" == "1" ]]' in script


def test_restore_script_supports_verify_only_without_touching_prod() -> None:
    script = read(RESTORE_SCRIPT)
    assert "--verify-only" in script
    assert "integrity_check" in script
    assert "foreign_key_check" in script
