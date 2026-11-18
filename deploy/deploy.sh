#!/usr/bin/env bash
# VPS 上で実行する（staging / production 共通）。
#   deploy/deploy.sh deploy <ghcr.io/owner/repo-app@sha256:...>   新しいImageへ切り替え
#   deploy/deploy.sh rollback [--yes]                              直前のImageへ戻す（migrateはしない）
#   deploy/deploy.sh current                                       稼働中のImage参照だけを表示
#   deploy/deploy.sh status                                        状態表示
#   deploy/deploy.sh tune [オプション]                             Argon2ベンチマーク
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE_DIR="$ROOT/.deploy"
DC=(docker compose -f compose.yaml -f compose.deploy.yaml)
IMAGE_RE='^ghcr\.io/[a-z0-9._-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

env_get() { # .env から値を取り出す（引用符なし前提）
  local line
  line="$(grep -E "^$1=" .env | tail -n1 | tr -d '\r' || true)"
  printf '%s' "${line#*=}"
}

set_env() { # KEY VALUE を .env へ反映（権限600を維持）
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  grep -v -E "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" .env
}

preflight() { # 環境の取り違え・設定漏れで事故を起こさないための最低限の検査
  [[ -f .env ]] || die ".env がありません（deploy/*.env.example を参考に作成）"
  [[ "$(stat -c %a .env)" == "600" ]] || die ".env の権限は 600 にしてください（chmod 600 .env）"
  [[ "$(env_get ENV)" == "prod" ]] || die "ENV=prod が必要です（staging も prod で動かす）"
  [[ "$(env_get PUBLIC_BASE_URL)" == https://* ]] || die "PUBLIC_BASE_URL は https:// で始まる必要があります"
  local site pepper
  site="$(env_get SITE_ADDRESS)"
  [[ -n "$site" && "$site" != "localhost" ]] || die "SITE_ADDRESS に実ドメインを設定してください"
  [[ "$(env_get PUBLIC_BASE_URL)" == "https://${site}" ]] || die "PUBLIC_BASE_URL と SITE_ADDRESS が一致しません（CSRFのOrigin検証が失敗します）"
  pepper="$(env_get APP_PEPPER)"
  [[ ${#pepper} -ge 43 && "$pepper" != dummy* ]] || die "APP_PEPPER が未設定・ダミー・短すぎます（32バイト以上のbase64url）"
  [[ -f deploy/proxy/Caddyfile ]] || die "deploy/proxy/Caddyfile がありません"
}

lock() {
  mkdir -p "$STATE_DIR"
  exec 9>"$STATE_DIR/lock"
  flock -n 9 || die "別のデプロイが実行中です"
}

smoke() { # 公開URLの /healthz を確認（DNS・証明書発行待ちを考慮して最大約60秒）
  local url
  url="https://$(env_get SITE_ADDRESS)/healthz"
  for _ in $(seq 1 15); do
    if curl -fsS --max-time 5 "$url" 2>/dev/null | grep -Eq '"status": ?"ok"'; then
      log "smoke OK: $url"
      return 0
    fi
    sleep 4
  done
  log "WARN: $url が応答しません。DNS・証明書発行・FWを確認: docker compose logs reverse-proxy" >&2
  return 1
}

record() { # prev new
  printf '%s %s -> %s\n' "$(date -u +%FT%TZ)" "${1:-none}" "$2" >> "$STATE_DIR/releases.log"
  if [[ -n "${1:-}" ]]; then printf '%s\n' "$1" > "$STATE_DIR/previous"; fi
  printf '%s\n' "$2" > "$STATE_DIR/current"
}

cmd_deploy() {
  local new="${1:-}" prev
  [[ "$new" =~ $IMAGE_RE ]] || die "Image は ghcr.io/<owner>/<name>@sha256:<64桁hex> の形式で指定してください"
  preflight
  lock

  prev="$(env_get APP_IMAGE)"
  [[ "$prev" =~ $IMAGE_RE ]] || prev=""   # 雛形のプレースホルダは無視
  log "deploy: ${prev:-<none>} -> $new"

  log "1/4 pull（失敗してもまだ何も止めない）"
  APP_IMAGE="$new" "${DC[@]}" pull app

  log "2/4 app 停止（単一インスタンスのため数秒〜数十秒のダウンタイム）"
  APP_IMAGE="${prev:-$new}" "${DC[@]}" stop app || true

  log "3/4 migrate（ENV=prod なので適用前に自動バックアップが取られる）"
  if ! APP_IMAGE="$new" "${DC[@]}" run --rm --no-deps app python scripts/migrate.py; then
    log "migrate 失敗。失敗した migration は rollback 済み。旧Imageでの復帰を試みます" >&2
    if [[ -n "$prev" ]]; then
      APP_IMAGE="$prev" "${DC[@]}" up -d --wait --wait-timeout 120 || true
    fi
    exit 1
  fi

  log "4/4 起動（app の healthcheck 通過後に reverse-proxy が起動）"
  set_env APP_IMAGE "$new"
  if ! "${DC[@]}" up -d --wait --wait-timeout 120; then
    log "起動失敗。app のログ末尾:" >&2
    "${DC[@]}" logs --tail=30 app >&2 || true
    if [[ -n "$prev" ]]; then
      log "旧Imageへ自動ロールバックします（適用済みmigrationは戻りません。追加型なら旧Imageでも動く想定）" >&2
      set_env APP_IMAGE "$prev"
      "${DC[@]}" up -d --wait --wait-timeout 120 || true
    fi
    exit 1
  fi

  record "$prev" "$new"
  docker image prune -f --filter "until=336h" > /dev/null || true   # 14日より古い未使用Imageだけ掃除
  smoke || true
  log "done: $new"
}

cmd_rollback() {
  local yes="${1:-}" prev cur
  preflight
  lock
  [[ -f "$STATE_DIR/previous" ]] || die "直前のImage記録（.deploy/previous）がありません"
  prev="$(cat "$STATE_DIR/previous")"
  [[ "$prev" =~ $IMAGE_RE ]] || die "記録されたImageの形式が不正です: $prev"
  cur="$(env_get APP_IMAGE)"
  log "rollback: $cur -> $prev"
  log "注意: migrate は実行しません。DBスキーマが新しい場合は旧Imageとの互換性を確認してください。"
  log "      DBを戻す必要がある場合は deploy/restore.sh を使います。"
  if [[ "$yes" != "--yes" ]]; then
    read -r -p "続行しますか? [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || die "中止しました"
  fi
  APP_IMAGE="$prev" "${DC[@]}" pull app
  set_env APP_IMAGE "$prev"
  "${DC[@]}" up -d --wait --wait-timeout 120
  record "$cur" "$prev"
  smoke || true
}

case "${1:-}" in
  deploy)   shift; cmd_deploy "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  current)  [[ -f .env ]] || die ".env がありません"; printf '%s\n' "$(env_get APP_IMAGE)" ;;
  status)
    [[ -f .env ]] || die ".env がありません"
    printf 'current: %s\n' "$(env_get APP_IMAGE)"
    "${DC[@]}" ps
    tail -n 5 "$STATE_DIR/releases.log" 2>/dev/null || true
    smoke || true
    ;;
  tune)     shift; "${DC[@]}" run --rm --no-deps -T app python scripts/tune_argon2.py "$@" ;;
  *)        sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
