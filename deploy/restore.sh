#!/usr/bin/env bash
# 復元・復元予行。入力は「復号済みの平文 .db」。復号は信頼できる手元PCで行うのが基本:
#   age -d -i age-key.txt app-XXXX.db.gz.age | gunzip > restored.db
# 使い方:
#   deploy/restore.sh <restored.db> --verify-only   検証だけ（整合性・件数。稼働中でも可、本番DBには触れない）
#   deploy/restore.sh <restored.db>                 本番DBを置き換える（app を停止→退避→配置→起動）
# 注意: 復元した時点以降の書き込み（新規プロフィール・パスワード変更等）は失われる。
# 復元後に平文の .db は安全に消すこと。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
DC=(docker compose -f compose.yaml -f compose.deploy.yaml)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

src="${1:-}"; mode="${2:-}"
[[ -f "$src" ]] || die "使い方: restore.sh <restored.db> [--verify-only]"
image="$(grep -E '^APP_IMAGE=' .env | tail -n1 | cut -d= -f2- | tr -d '\r')"
[[ -n "$image" ]] || die ".env に APP_IMAGE がありません"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$src" "$work/restore.db"
chmod 755 "$work"; chmod 666 "$work/restore.db"   # コンテナ内の非rootユーザー(10001)が読み書きできるように

log "検証: integrity_check / foreign_key_check / 件数"
docker run --rm --user 10001:10001 -v "$work:/work" "$image" sh -ec '
  r="$(sqlite3 /work/restore.db "PRAGMA integrity_check;")"
  [ "$r" = "ok" ] || { echo "integrity_check: $r" >&2; exit 1; }
  fk="$(sqlite3 /work/restore.db "PRAGMA foreign_key_check;")"
  [ -z "$fk" ] || { echo "foreign_key_check: $fk" >&2; exit 1; }
  sqlite3 /work/restore.db "SELECT '"'"'users'"'"', count(*) FROM users UNION ALL SELECT '"'"'profiles'"'"', count(*) FROM profiles UNION ALL SELECT '"'"'sessions'"'"', count(*) FROM sessions UNION ALL SELECT '"'"'schema_migrations'"'"', count(*) FROM schema_migrations;"
'
log "検証OK"
[[ "$mode" == "--verify-only" ]] && exit 0

read -r -p "本番DBを置き換えます。続行するには RESTORE と入力: " ans
[[ "$ans" == "RESTORE" ]] || die "中止しました"

vol="$(docker volume ls -q --filter label=com.docker.compose.project=anon-profile --filter label=com.docker.compose.volume=app-data | head -n1)"
[[ -n "$vol" ]] || die "app-data volume が見つかりません"

log "app 停止"
"${DC[@]}" stop app

log "退避して配置（現DBは /data/app.db*.before-restore-<時刻> として volume 内に残る。不要になったら手動で削除）"
docker run --rm --user 10001:10001 -v "$vol:/data" -v "$work:/work:ro" "$image" sh -ec '
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  for s in "" -wal -shm; do
    if [ -f "/data/app.db$s" ]; then mv "/data/app.db$s" "/data/app.db$s.before-restore-$ts"; fi
  done
  cp /work/restore.db /data/app.db
'

log "app 起動"
"${DC[@]}" up -d --wait --wait-timeout 120
log "done。ログインと公開ページの表示を確認してください"
