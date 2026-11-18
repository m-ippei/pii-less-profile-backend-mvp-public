#!/usr/bin/env bash
# ホスト・コンテナの健全性チェック（systemd timer から日次実行）。
# 異常があれば NOTIFY_URL へ通知し、CRIT があれば非0で終了（systemctl --failed に出る）。
set -uo pipefail   # -e は付けない（1つ失敗しても残りのチェックを続ける）

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
if [[ -f ops.env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./ops.env
  set +a
fi

DISK_WARN_PCT="${DISK_WARN_PCT:-80}"; DISK_CRIT_PCT="${DISK_CRIT_PCT:-90}"
JOURNAL_WARN_MB="${JOURNAL_WARN_MB:-800}"; CERT_WARN_DAYS="${CERT_WARN_DAYS:-14}"
BACKUP_ENABLED="${BACKUP_ENABLED:-1}"; REQUIRE_OFFSITE="${REQUIRE_OFFSITE:-1}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/anon-profile}"
STATE_DIR="$ROOT/.deploy"
DC=(docker compose -f compose.yaml -f compose.deploy.yaml)

problems=(); crit=0
add() { # LEVEL name detail
  printf '%-4s %s: %s\n' "$1" "$2" "$3"
  if [[ "$1" != "OK" ]]; then problems+=("$1 $2: $3"); fi
  if [[ "$1" == "CRIT" ]]; then crit=1; fi
  return 0
}
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r'; }

check_pct() { # name path
  local pct
  pct="$(df --output=pcent "$2" 2>/dev/null | tail -n1 | tr -dc '0-9')"
  [[ -n "$pct" ]] || { add WARN "$1" "使用率を取得できません ($2)"; return; }
  if   (( pct >= DISK_CRIT_PCT )); then add CRIT "$1" "${pct}% 使用 ($2)"
  elif (( pct >= DISK_WARN_PCT )); then add WARN "$1" "${pct}% 使用 ($2)"
  else add OK "$1" "${pct}% 使用 ($2)"; fi
}

check_age() { # name file max_hours
  local mtime age_h
  if [[ ! -f "$2" ]]; then add CRIT "$1" "記録がありません（一度も成功していない）"; return; fi
  mtime="$(stat -c %Y "$2")"; age_h=$(( ( $(date +%s) - mtime ) / 3600 ))
  if (( age_h > $3 )); then add CRIT "$1" "最終成功から ${age_h} 時間経過"; else add OK "$1" "最終成功 ${age_h} 時間前"; fi
}

# --- ディスク -------------------------------------------------------------
check_pct "disk:/" /
docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
if [[ -n "$docker_root" ]]; then check_pct "disk:docker" "$docker_root"; else add CRIT docker "docker info に失敗"; fi

if [[ -d /var/log/journal ]]; then
  jmb="$(du -sm /var/log/journal 2>/dev/null | cut -f1)"
  if [[ -z "$jmb" ]]; then add WARN journal "サイズ取得不可（deploy を systemd-journal グループへ）"
  elif (( jmb >= JOURNAL_WARN_MB )); then add WARN journal "${jmb}MB（上限 SystemMaxUse 付近。30日未満で古いログが消え始める）"
  else add OK journal "${jmb}MB"; fi
else
  add WARN journal "/var/log/journal がありません（Storage=persistent を確認）"
fi

# --- コンテナ・DB ----------------------------------------------------------
if [[ -f .env ]]; then
  app_cid="$("${DC[@]}" ps -q app 2>/dev/null || true)"
  proxy_cid="$("${DC[@]}" ps -q reverse-proxy 2>/dev/null || true)"
  health="$([[ -n "$app_cid" ]] && docker inspect -f '{{.State.Health.Status}}' "$app_cid" 2>/dev/null || echo none)"
  if [[ "$health" == "healthy" ]]; then add OK app "healthy"; else add CRIT app "health=$health"; fi
  running="$([[ -n "$proxy_cid" ]] && docker inspect -f '{{.State.Running}}' "$proxy_cid" 2>/dev/null || echo false)"
  if [[ "$running" == "true" ]]; then add OK proxy "running"; else add CRIT proxy "停止中"; fi
  dbsize="$("${DC[@]}" exec -T app du -sm /data 2>/dev/null | cut -f1 || true)"
  add OK "volume:app-data" "${dbsize:-?}MB（DB本体・WAL・pre-migrate バックアップ）"
else
  add CRIT env ".env がありません"
fi

# --- バックアップ ----------------------------------------------------------
if [[ "$BACKUP_ENABLED" == "1" ]]; then
  check_age backup "$STATE_DIR/last_backup_ok" 30
  if [[ "$REQUIRE_OFFSITE" == "1" ]]; then check_age offsite "$STATE_DIR/last_offsite_ok" 30; fi
  if [[ -d "$BACKUP_DIR" ]]; then add OK "backup-dir" "$(du -sm "$BACKUP_DIR" | cut -f1)MB / $(ls -1 "$BACKUP_DIR"/app-*.db.gz.age 2>/dev/null | wc -l) 件"; fi
fi

# --- 証明書 ----------------------------------------------------------------
site="$(env_get SITE_ADDRESS)"
if [[ -n "$site" ]]; then
  end="$(echo | timeout 10 openssl s_client -connect "${site}:443" -servername "$site" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
  if [[ -n "$end" ]]; then
    days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
    if (( days < CERT_WARN_DAYS )); then add CRIT cert "残り ${days} 日（自動更新の失敗を疑う: docker compose logs reverse-proxy）"; else add OK cert "残り ${days} 日"; fi
  else
    add WARN cert "${site}:443 から証明書を取得できません"
  fi
fi

# --- その他 ----------------------------------------------------------------
if [[ -f /var/run/reboot-required ]]; then add WARN reboot "再起動が必要（カーネル更新等）。メンテナンス時間に reboot"; fi
echo "--- docker system df ---"; docker system df 2>/dev/null || true

# --- 通知 ------------------------------------------------------------------
if (( ${#problems[@]} > 0 )) && [[ -n "${NOTIFY_URL:-}" ]]; then
  body="$(printf '%s\n' "${problems[@]}")"
  curl -fsS -m 10 -H "Title: anon-profile $(hostname)" -d "$body" "$NOTIFY_URL" > /dev/null || echo "WARN: 通知に失敗"
fi
if [[ -n "${MONITOR_PING_URL:-}" ]]; then curl -fsS -m 10 --retry 3 -o /dev/null "$MONITOR_PING_URL" || true; fi
exit "$crit"
