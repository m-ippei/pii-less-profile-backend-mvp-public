#!/usr/bin/env bash
# 日次バックアップ（systemd timer から実行）。
#   SQLite .backup（稼働中でも整合）→ integrity_check → gzip → age暗号化 → ローカル保管 → オフサイト転送
# ホスト上に平文のバックアップは残さない（本文は利用者の本音の平文。秘密鍵はVPSに置かない）。
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
# shellcheck disable=SC1091
. ./ops.env
set +a

BACKUP_ENABLED="${BACKUP_ENABLED:-1}"
if [[ "$BACKUP_ENABLED" != "1" ]]; then echo "backup disabled (BACKUP_ENABLED=$BACKUP_ENABLED)"; exit 0; fi

: "${AGE_RECIPIENT:?ops.env に AGE_RECIPIENT（age公開鍵）が必要です}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/anon-profile}"
KEEP_LOCAL_DAYS="${KEEP_LOCAL_DAYS:-14}"
REMOTE_KEEP_DAYS="${REMOTE_KEEP_DAYS:-60}"
REQUIRE_OFFSITE="${REQUIRE_OFFSITE:-1}"
STATE_DIR="$ROOT/.deploy"
DC=(docker compose -f compose.yaml -f compose.deploy.yaml)

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
hc_ping() { # healthchecks.io 互換。$1 = "" | /start | /fail
  if [[ -n "${BACKUP_PING_URL:-}" ]]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "${BACKUP_PING_URL}${1:-}" || true
  fi
}

mkdir -p "$BACKUP_DIR" "$STATE_DIR"
chmod 700 "$BACKUP_DIR"
tmp="$(mktemp "$BACKUP_DIR/.tmp.XXXXXX")"
cleanup() {
  local rc=$?
  rm -f "$tmp"
  if [[ $rc -ne 0 ]]; then log "FAILED (rc=$rc)"; hc_ping /fail; fi
}
trap cleanup EXIT

hc_ping /start
name="app-$(date -u +%Y%m%dT%H%M%SZ).db.gz.age"

# コンテナ内で .backup → integrity_check → 標準出力へ。/tmp は tmpfs（read_only コンテナでも書ける）。
# docker cp は tmpfs から取れないため、標準出力経由で受け取る。
log "backup: start"
"${DC[@]}" exec -T app sh -ec '
  f="$(mktemp /tmp/backup.XXXXXX)"
  trap "rm -f $f" EXIT
  sqlite3 -cmd ".timeout 5000" /data/app.db ".backup $f"
  [ "$(sqlite3 "$f" "PRAGMA integrity_check;")" = "ok" ] || { echo "integrity_check failed" >&2; exit 1; }
  cat "$f"
' | gzip -9 | age -r "$AGE_RECIPIENT" -o "$tmp"

[[ "$(stat -c %s "$tmp")" -gt 200 ]] || { log "backup が小さすぎます"; exit 1; }
head -c 21 "$tmp" | grep -q 'age-encryption.org' || { log "age 形式ではありません"; exit 1; }
mv "$tmp" "$BACKUP_DIR/$name"
touch "$STATE_DIR/last_backup_ok"
log "backup: saved $BACKUP_DIR/$name ($(stat -c %s "$BACKUP_DIR/$name") bytes)"

# ローカル保持: KEEP_LOCAL_DAYS より古いものを削除。ただし直近3件は常に残す
# （バックアップが止まっている間に全部消えるのを防ぐ）。
ls -1t "$BACKUP_DIR"/app-*.db.gz.age 2>/dev/null | tail -n +4 | while read -r f; do
  if [[ -n "$(find "$f" -mtime "+$KEEP_LOCAL_DAYS" -print)" ]]; then rm -f "$f"; fi
done

# オフサイト
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  rclone copyto "$BACKUP_DIR/$name" "${RCLONE_REMOTE%/}/$name"
  touch "$STATE_DIR/last_offsite_ok"
  log "offsite: uploaded ${RCLONE_REMOTE%/}/$name"
  # 古いものの削除（バケット側のライフサイクルルールで消す運用なら、REMOTE_KEEP_DAYS を大きくしてよい）
  rclone delete "${RCLONE_REMOTE%/}" --include 'app-*.db.gz.age' --min-age "${REMOTE_KEEP_DAYS}d" || log "WARN: remote 整理に失敗"
elif [[ "$REQUIRE_OFFSITE" == "1" ]]; then
  log "ERROR: RCLONE_REMOTE 未設定（REQUIRE_OFFSITE=1）。同一VPSだけのバックアップは障害時に失われます"
  exit 1
else
  log "WARN: オフサイト転送なし（REQUIRE_OFFSITE=0）"
fi

hc_ping ""
log "backup: done"
