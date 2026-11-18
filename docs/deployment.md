# デプロイ運用ガイド（staging / production 共通）

> 用途: 本サービスのデプロイ運用の**実施手順**（VPS 初期設定・GitHub Secrets・初回デプロイ・通常リリース・ログ運用・バックアップ/復元・監視・`tune`・公開前チェックリスト）を持つ。
> **正本は `design/16-deployment.md` §18（デプロイ・リリース・運用基盤）**。本書は手順のみを持ち、環境・リリース/デプロイ方式・バックアップ/監視の仕様は設計章を重複させない。
> 関連: 通常運用（公開停止・削除依頼・DB 直接操作・migration の適用前バックアップと復旧）は `docs/operations.md`、ログの意味・チャネル定義・調査手順は `docs/logging.md`。
>
> **公開版の記載方針**: ホスト名・IP・ユーザー名・ディレクトリ・ログタグ・systemd unit 名などの環境固有値は、公開版では `<...>` のプレースホルダで示します。実際の値は非公開の運用手順で管理してください。

## 0. 全体像

```
手元PC ── 手動で release ──▶ GitHub Actions: release
                             make check → runtime Image build → ghcr.io へ push → digest 表示
                                                │ 同じdigestを使い回す
       Actions: deploy(手動) ─ssh─▶ staging VPS ──検証OK──▶ production VPS
         ・compose/Caddyfile/スクリプトだけを同期（ソースコードは置かない）
         ・deploy.sh: pull → app停止 → migrate → up → smoke
```

VPS 上のレイアウト（ソースコードは無い。Image の中にだけある）:

```
<APP_DIR>/
  compose.yaml  compose.deploy.yaml
  deploy/ (deploy.sh backup.sh restore.sh monitor.sh proxy/Caddyfile systemd/ host/)
  .env  ops.env        ← VPS にだけ存在（commit しない。chmod 600）
  .deploy/             ← 状態（current / previous / releases.log / last_backup_ok …）
```

## 1. VPS 初期設定（両VPSで実施）

```bash
# root で
adduser <DEPLOY_USER>
usermod -aG sudo,docker,systemd-journal <DEPLOY_USER>   # docker グループ ≒ root 権限。鍵認証必須
# /home/<DEPLOY_USER>/.ssh/authorized_keys に公開鍵を2本（人間用 / GitHub Actions 用）。権限 700 / 600

# /etc/ssh/sshd_config.d/10-hardening.conf
#   PasswordAuthentication no
#   PermitRootLogin no
systemctl reload ssh      # 別セッションでログインできることを確認してから元のセッションを閉じる

ufw default deny incoming
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw enable
# ※ Docker の公開ポートは ufw をバイパスする。compose の ports は 80/443 以外を書かない。

apt update && apt install -y unattended-upgrades age rclone jq curl ca-certificates
```

Docker は公式 apt リポジトリから導入（`docker.io` は使わない）:

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
tee /etc/apt/sources.list.d/docker.sources <<EOT
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOT
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

作業ディレクトリ:

```bash
install -d -o <DEPLOY_USER> -g <DEPLOY_USER> <APP_DIR>
install -d -o <DEPLOY_USER> -g <DEPLOY_USER> -m 700 <BACKUP_DIR>
```

## 2. GitHub 側の準備

1. **GHCR からの pull 用トークン**: 可能なら fine-grained token または GitHub App を使い、**対象パッケージの read 権限だけ**を与える（classic PAT は最小スコープ・短い期限で管理する）。VPS の `<DEPLOY_USER>` で:
   `echo <TOKEN> | docker login ghcr.io -u <GitHubユーザー名> --password-stdin`
   （有効期限切れで pull が失敗する。pull は停止前に行うので本番は止まらないが、期限はカレンダーに入れる）
2. **Secrets**（リポジトリの Settings → Secrets and variables → Actions）:

   | 名前 | 値 |
   |---|---|
   | `STAGING_SSH_HOST` / `PROD_SSH_HOST` | VPS のホスト名/IP |
   | `STAGING_SSH_USER` / `PROD_SSH_USER` | `<DEPLOY_USER>` |
   | `STAGING_SSH_KEY` / `PROD_SSH_KEY` | CI 専用の秘密鍵（人間用と分ける。環境ごとに別の鍵） |
   | `STAGING_SSH_KNOWN_HOSTS` / `PROD_SSH_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 <host>` の出力（VPS業者のコンソール等でフィンガープリントを照合） |

   注意: CI 用の鍵はデプロイ権限を持つため、**環境ごとの専用鍵・最小権限・GitHub アカウントの 2FA** を必須にする。
   鍵を預けたくなければ deploy.yaml を使わず、手元から `scp` + `ssh <DEPLOY_USER>@<HOST> deploy/deploy.sh deploy <image>` でも同じ。
3. 初回の release で GHCR パッケージが作られる（`org.opencontainers.image.source` ラベルによりリポジトリに紐づく）。
   > **公開リポジトリでの注意**: GHCR パッケージの可視性はリポジトリと独立です。public リポジトリでもパッケージが private のままなら VPS に読み取り専用のパッケージトークンが必要です。public にする場合は誰でも pull できる点を確認し、GitHub の Package Settings で可視性を明示してください。

## 3. 初回デプロイ（まず staging）

1. **DNS**: A レコードのみ（AAAA は作らない）。伝播後に起動（失敗の連続は Let's Encrypt のレート制限に当たる）。
2. **Actions → release → Run workflow** を実行 → 成功 → Summary の `ghcr.io/...@sha256:...` を控える。
3. VPS に `.env`、`ops.env` を作る（初回は deploy workflow が同期する `deploy/*.example` を元に）:
   ```bash
   cd <APP_DIR>
   cp deploy/staging.env.example .env && chmod 600 .env && nano .env     # pepper を生成して入れる
   cp deploy/ops.env.example ops.env && chmod 600 ops.env && nano ops.env
   ```
   （`.example` が VPS にまだ無ければ、deploy workflow を一度実行する。`.env` が無いので preflight で止まるが、同期は済む）
4. **Actions → deploy → Run workflow**: target=staging、image=控えた値。
5. 確認:
   - `curl -I https://<STAGING_DOMAIN>/healthz`（証明書・HSTS・CSP、`{"status":"ok"}`）
   - ブラウザで 作成 → ログイン → 公開 → パスワード変更 → 回復 まで通す
   - **クライアントIP**: 別回線（スマホ/PC）から `login_failed` を起こし、`ip_daily` が別の値になること。同じ値ならIPが潰れている（IPv6経路やプロキシ設定を疑う）
6. §5〜§7 のホスト設定（journald・timer）を入れる。
7. 同じ digest で production も手順 3〜6（HSTS は短い `max-age` から開始し、安定後に段階的に延長する）。

## 4. 通常のリリース手順

| 段階 | やること |
|---|---|
| 修正 | 手動で release を実行（check→build→push）→ 新 digest |
| 検証 | deploy: target=staging → 修正と退行を確認 |
| 本番 | deploy: target=production、**staging と同じ digest**（不一致は workflow が拒否） |
| 失敗時 | VPS で `deploy/deploy.sh rollback`（migrate は戻らない。DB が必要なら §6 の復元） |

`deploy/deploy.sh status` / `current` で状態確認。`make` のターゲットは VPS では使わない（compose.local.yaml が重なる）。

## 5. ログ運用（journald）

> ログの**チャネル定義・許可項目・目的別の見方・調査手順**は `docs/logging.md`（正本 `design/15-logging.md` §17）を参照。本節は VPS の journald 上での**操作**を持つ。

導入（両VPS）:

```bash
sudo install -d /etc/systemd/journald.conf.d
sudo cp <APP_DIR>/deploy/host/journald-*.conf /etc/systemd/journald.conf.d/<app>.conf
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

- 保持期間・上限・削除精度は `deploy/host/journald-*.conf` で設定する。`ip_daily` の保持期間と一致させ、監視の警告しきい値は `ops.env` で管理する。
- compose が `tag: <LOG_TAG_APP>` / `<LOG_TAG_PROXY>` を付けるので `-t` で絞れる。**コンテナの stderr は優先度 err 扱いになるため `-p` で絞らない**。
- 確認コマンド（`<DEPLOY_USER>` は `systemd-journal` グループ所属）:

```bash
journalctl -t <LOG_TAG_APP> --since "1 hour ago" -o cat | tail            # 直近
journalctl -t <LOG_TAG_APP> -f -o cat                                      # 追尾
# 5xx（app.access）
journalctl -t <LOG_TAG_APP> --since today -o cat | jq -cR 'fromjson? | select((.status // 0) >= 500)'
# request_id で全チャネルを追跡
journalctl -t <LOG_TAG_APP> -o cat | jq -cR 'fromjson? | select(.request_id=="<id>")'
# セキュリティイベント
journalctl -t <LOG_TAG_APP> --since today -o cat | jq -cR 'fromjson? | select(.event=="login_failed")'
# 429 の内訳
journalctl -t <LOG_TAG_APP> --since today -o cat | jq -rR 'fromjson? | select(.event=="rate_limited") | .kind' | sort | uniq -c
# 容量上限到達（fail-closed）
journalctl -t <LOG_TAG_APP> -o cat | grep rate_limit_cap_reached
# Caddy（証明書更新など）
journalctl -t <LOG_TAG_PROXY> --since "1 day ago"
```

- 保守: `journalctl --disk-usage` / `sudo journalctl --vacuum-time=<RETENTION>`（手動で整理）/ `sudo journalctl --verify`。
- Caddy はアクセスログを出さない。Caddy のエラーログの扱い（保存期間・アクセス制限・必要に応じたマスキング）は運用ポリシーで定める。

## 6. バックアップ・復元

> DB の直接操作・migration 適用前の自動バックアップ（`pre-migrate-*`）・`make migrate-check` による検証は `docs/operations.md`（正本 `design/09-migrations.md` §10）を参照。本節はデプロイ基盤の**日次/オフサイトバックアップと復元**を持つ。

方式: `.backup` → `integrity_check` → gzip → **age で暗号化**（VPS には公開鍵のみ。平文はホストに残さない）→ ローカル保持（`<LOCAL_RETENTION>`）→ **オフサイト転送**。

準備（production のみ。staging は `BACKUP_ENABLED=0`）:

```bash
# 手元PCで鍵を作る。age-key.txt（秘密鍵）はパスワードマネージャ＋オフライン媒体に保管。VPS には置かない
age-keygen -o age-key.txt        # 表示される "age1..." が公開鍵 → ops.env の AGE_RECIPIENT
# VPS で rclone のリモートを作る（S3 互換ストレージ等。無料枠・料金は各社で確認）
rclone config                    # → ops.env の RCLONE_REMOTE=<remote>:<bucket>/production
# 任意: 外形監視サービスで check を作り BACKUP_PING_URL へ（バックアップが「走っていない」ことも検知できる）
```

timer の有効化（§7 と合わせて）:

```bash
sudo cp <APP_DIR>/deploy/systemd/*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <BACKUP_UNIT>.timer <MONITOR_UNIT>.timer   # production
sudo systemctl enable --now <MONITOR_UNIT>.timer                              # staging（backup は不要）
sudo systemctl start <BACKUP_UNIT>.service && journalctl -u <BACKUP_UNIT> -n 20   # 初回は手動で試す
```

**復元予行（初回と、その後は月1回）**:

```bash
# 手元PC: 最新のバックアップを取得して復号
rclone copyto <remote>:<bucket>/production/app-XXXX.db.gz.age .
age -d -i age-key.txt app-XXXX.db.gz.age | gunzip > restored.db
# VPS へ送り、検証だけ（本番DBには触れない）
scp restored.db <DEPLOY_USER>@<HOST>:/tmp/ && ssh <DEPLOY_USER>@<HOST> '<APP_DIR>/deploy/restore.sh /tmp/restored.db --verify-only && shred -u /tmp/restored.db'
```

**実際の復元**: 同じく復号した `.db` を VPS へ送り `deploy/restore.sh /tmp/restored.db`（`RESTORE` と入力。app 停止→現DBを `*.before-restore-<時刻>` として退避→配置→起動）。復元時点以降の書き込みは失われる。退避ファイルは volume 内に残るので、確認後に手動で削除。

補足: pepper（`APP_PEPPER`）はバックアップに含まれない。**別経路で保管**しないと復元してもログインできない。Caddy の証明書 volume はバックアップ対象外（再発行される）。

## 7. ディスク・健全性の監視（monitor.sh）

日次で実行する（`<MONITOR_UNIT>.timer`。実行時刻は運用ポリシー）。内容: ルートFS・Docker領域の使用率、journald サイズ、app の health・proxy の稼働、DB volume サイズ、バックアップ/オフサイトの最終成功からの経過、証明書残日数、再起動要否。警告・重大のしきい値は `ops.env` で設定する。

- 異常は `NOTIFY_URL`（通知サービスのエンドポイント。本文にはディスク%等のみで秘密値は含まない）へ POST。重大は非0終了 → `systemctl --failed` に出る。
- 手動実行: `<APP_DIR>/deploy/monitor.sh`、履歴: `journalctl -u <MONITOR_UNIT>`
- 外形監視（VPS の外から `https://<DOMAIN>/healthz`）を必須とする。`monitor.sh` は同一ホストで動くため、VPS 自体の停止を代替できない。
- 容量対策の手動操作: `docker system df`、`docker image prune -f`、`journalctl --vacuum-size=<SIZE>`。

## 8. Argon2 の調整方法

目的: ログイン/作成/パスワード変更の 1回の hash を **約200〜500ms**（設計 §8）にする。VPS の CPU 性能に依存するため **実機で測る**（手元PCの値は使えない）。

```bash
# staging VPS（本番と同じプラン）で、アクセスが少ない時間帯に
<APP_DIR>/deploy/deploy.sh tune
# 目標を変えるなら: deploy/deploy.sh tune --min-ms 250 --max-ms 450
```

出力例の読み方: `single_ms` = 1回の中央値、`concurrent_ms` = `ARGON2_MAX_CONCURRENCY`（設定値）並列時の中央値。目標内の候補のうち **メモリ最大（同じなら time_cost 最大）** が「推奨」として出る。

1. 推奨の `ARGON2_*` 4行を staging の `.env` に反映 → `deploy.sh deploy <同じimage>`（`up` で反映）。
2. 実際の応答で確認: ブラウザでログインして DevTools の Network で `sessions` の所要時間、または
   `journalctl -t <LOG_TAG_APP> --since "10 min ago" -o cat | jq -cR 'fromjson? | select(.method=="POST") | {route,duration_ms}'`
3. 同じ値を production の `.env` に入れる。**production でも公開前に `tune` を1回流して**同程度であることを確認（プランが同じなら再測定は短時間でよい）。
4. 判断基準:
   - 2並列時が約1秒を超える → vCPU 不足。目標msを下げるかプランを上げる（枠が埋まると待たせず 429/503 を返す設計）。
   - メモリ: Argon2 の同時使用 ≒ memory_kib × 同時実行数。VPS の空きメモリの半分以内に（tune は超える候補をスキップする）。
   - 推奨下限: `ENV!=test` では `time_cost>=2` かつ `memory_kib>=19456` を下回ると起動しない。
5. 変更が効くのは**新しく作る hash だけ**。既存の hash は自身に埋め込まれたパラメータで検証される。→ 利用者が増える前に決める。

## 9. 公開前チェックリスト

以下は**リリース前に人間が実施**する。リポジトリ内の構成不変条件（`compose.yaml`/`compose.deploy.yaml`・Caddyfile・workflows・`deploy/*.sh`・journald 設定・systemd unit・`.gitattributes`）は `tests/security/test_deployment_config.py` が `make check` で自動検証する（該当項目に `[auto]`）。実環境の操作（有効化・実測・確認）は人手。

- [ ] production/staging で `ENV=prod`・別々の `APP_PEPPER`・別ドメイン。pepper を別経路に保管
- [ ] DNS は A のみ。`/healthz`・証明書・ヘッダー確認。クライアントIP（`ip_daily`）が回線ごとに別値
- [ ] HSTS を短い `max-age` から段階的に延長
- [ ] Caddy イメージが digest 固定。更新は手動で確認（自動化する場合は Dependabot/Renovate を別途有効化） `[auto]`
- [ ] journald（tag `<LOG_TAG_APP>`/`<LOG_TAG_PROXY>`・保持期間は運用ポリシー）と systemd timer（backup / monitor）を有効化 `[auto: 設定]`
- [ ] 日次バックアップ成功・オフサイト到達・**復元予行**済み。age 秘密鍵を安全に保管 `[auto: age/rclone 構成]`
- [ ] monitor の通知が届く（`NOTIFY_URL`）。外形監視を設定
- [ ] `tune` の結果を staging/production に反映
- [ ] GitHub 2FA、パッケージ取得トークンの期限、依存とイメージの更新を定期的に確認
