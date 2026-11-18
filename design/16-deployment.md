# 設計 16: デプロイ・リリース・運用基盤

> 関連: [インフラ・運用](10-infra-ops.md) / [ログ](15-logging.md) / [migration運用](09-migrations.md) / [セキュリティ](07-security.md) / [設計決定](13-decisions.md) / [完了定義・変更規約](14-dod-conventions.md)

---

## 18. デプロイ・リリース・運用基盤

**目的**: アプリを staging / production へ安全にビルド・配備し、公開後に監視・保全（バックアップ/復元）・チューニングする基盤を定める。[§11](10-infra-ops.md)（実行モデル・環境変数）の上に「**どうビルドし、どう配り、どう保全するか**」を積む章であり、アプリ本体（`src/`）の仕様は変えない。

**前提**: 単一プロセス（`worker = 1`、D-24）、SQLite（WAL）、リバースプロキシは Caddy。手順の実施主体は人間で、設計はその基準を定める。

> **公開版の記載方針**: ホスト名・ディレクトリ・ユーザー名・ログタグ・systemd unit 名・保持期間・監視閾値などの環境固有値は、公開版では `<...>` のプレースホルダで示す。実際の値は非公開の運用手順で管理する。

### 環境

- **staging / production の2環境**を運用する。**両方とも `ENV=prod`**。
- `ENV` は「**安全性の緩和スイッチ**」であって配備階層ではない。実装上の効果は次の2点のみ（`src/shared/security/security.py`、`src/main.py`）:
  - `test`: Argon2 の下限を緩和（E2E 専用）。`test` 以外は下限を fail-closed で強制する。
  - `dev`: `/_envcheck` を有効化（秘密値は返さない）。
- 配備階層の差は `SITE_ADDRESS` / `PUBLIC_BASE_URL` / `APP_PEPPER` / ホスト設定で表す。`ENV=stag` のような値は設けない。**staging は外部到達可能なため `ENV=prod` とし、prod の挙動（Argon2 下限の強制・`ENV=prod` での `pre-migrate` 自動バックアップ必須）をそのまま検証する**。
- production と staging は**別ドメイン・別 `APP_PEPPER`**。staging はバックアップを無効化する（`BACKUP_ENABLED=0`）。

### リリース（ビルド）

- **手動実行**で GitHub Actions **`release`** が動く: `make check`（任意で `make e2e` を手動実行）→ `runtime` ターゲットを build → GHCR（`ghcr.io/<owner>/<repo>-app`）へ push → digest を Step Summary に表示する。公開アーカイブでは自動実行しない。
- **GHCR パッケージの可視性はリポジトリと独立**。公開リポジトリでもパッケージが private なら VPS に読み取り専用のパッケージトークンが必要。public にする場合は誰でも pull できる点を許容し、公開前に Package Settings で可視性を確認する。
- **配備の単位は tag ではなく digest**（`ghcr.io/<owner>/<repo>-app@sha256:<64桁hex>`）。`provenance: false` で単一マニフェストにし、digest を1つに保つ。`org.opencontainers.image.source` ラベルで GHCR パッケージをリポジトリに紐づける。
- イメージ名は `GITHUB_REPOSITORY` から導出する（リポジトリにハードコードしない）。

### デプロイ

- GitHub Actions **`deploy`**（手動 `workflow_dispatch`）: 入力 `target=staging|production` / `image=<digest>` / `ref` / `skip_staging_check`。
  - **production は「staging で稼働中の digest と同一」であることを要求する**（`deploy.sh current` を staging へ照会。`skip_staging_check` は緊急時のみ）。
  - VPS へは **compose / Caddyfile / スクリプトのみ**を同期する（`compose.yaml` `compose.deploy.yaml` `deploy/proxy` `deploy/*.sh` `deploy/*.example` `deploy/systemd` `deploy/host`）。**ソースコードは置かない**（Image の中にだけ存在する）。
  - `deploy.sh deploy <digest>`: `pull` → `app` 停止 → `migrate`（`ENV=prod` のため適用前に自動バックアップ。§10）→ `up`（healthcheck 通過待ち）→ `smoke`（公開 URL の `/healthz`）。起動失敗時は旧 Image へ自動ロールバックする（適用済み migration は戻らない。追加型なら旧 Image でも動く前提）。
- `deploy.sh` のサブコマンド: `deploy` / `rollback [--yes]` / `current` / `status` / `tune`。`flock` で同時実行を排他し、状態は `.deploy/`（`current` / `previous` / `releases.log`）に持つ。
- 鍵を預けない運用も可: 手元から `scp` ＋ `ssh <DEPLOY_USER>@<HOST> deploy/deploy.sh deploy <image>` で同義。
- **`make` のターゲットは VPS では使わない**（`compose.local.yaml` が重なるため）。

### VPS レイアウト

```text
<APP_DIR>/
  compose.yaml  compose.deploy.yaml
  deploy/ (deploy.sh backup.sh restore.sh monitor.sh proxy/Caddyfile systemd/ host/)
  .env  ops.env        ← VPS にだけ存在（commit しない。chmod 600）
  .deploy/             ← 状態（current / previous / releases.log / last_backup_ok / last_offsite_ok / lock）
<BACKUP_DIR>/          ← ローカルバックアップ（アクセス制限）
```

- 運用ユーザーは `<DEPLOY_USER>`（`docker` / `systemd-journal` / `sudo` グループ）。SSH は**鍵認証のみ**（`PasswordAuthentication no` / `PermitRootLogin no`）。`ufw` は incoming deny で必要なポート（SSH / HTTP / HTTPS）のみ開放する。
- **Docker の公開ポートは ufw をバイパスする**ため、compose の `ports` は HTTP / HTTPS 以外を書かない。
- `compose.deploy.yaml` は `app.image` を `${APP_IMAGE:?}`（digest 必須）に固定し、`app` / `reverse-proxy` の `logging` を journald（下記）にする。`reverse-proxy` の `ports` は **IPv4 を明示**する（IPv6 を docker-proxy 経由で受けるとアプリが見るクライアント IP が同一になり、IP 単位 rate limit が全ユーザーで共有される。§8 / § レート制限）。
- CI 用の SSH 鍵はデプロイ権限を持つため、**環境ごとの専用鍵・最小権限・GitHub アカウントの 2FA** を必須とする。GHCR からの pull は最小スコープのトークン（可能なら fine-grained token / GitHub App）を使い、期限を管理する。

### ログ（journald）

- `compose.deploy.yaml` で `app` / `reverse-proxy` の logging driver を **journald**、tag を `<LOG_TAG_APP>` / `<LOG_TAG_PROXY>` にする。`-t` で絞り込める。**コンテナの stderr は優先度 err 扱いになるため `-p` で絞らない**。
- ホストの journald 設定（`deploy/host/journald-*.conf`）:

```ini
[Journal]
Storage=persistent
MaxRetentionSec=<RETENTION>   # §17 の ip_daily 保持期間と一致
MaxFileSec=<FILE_ROTATION>    # 削除精度（既定の月ローテーションだと最大2か月残る）
SystemMaxUse=<MAX_SIZE>       # 上限超過時は保持期間未満でも古い順に消える（monitor が警告）
SystemKeepFree=<KEEP_FREE>
Compress=yes
```

- アプリは stderr に JSON 1行を出す（§17）。ファイル出力・自前ローテーションはしない。
- Caddy はアクセスログを出さない。エラーログの扱い（保存期間・アクセス制限・必要に応じたマスキング）は運用ポリシーで定める。

### バックアップ・復元

- **日次**: `backup.sh`（systemd timer）。app コンテナ内で `sqlite3 /data/app.db ".backup <tmp>"` → `PRAGMA integrity_check` → 標準出力 → `gzip -9` → **age（公開鍵のみ。秘密鍵は VPS に置かない）** → ローカル（`<BACKUP_DIR>`、`<LOCAL_RETENTION>`。ただし直近3件は日数に関わらず残す）→ **rclone でオフサイト**（`REQUIRE_OFFSITE=1`: 転送失敗は異常終了）。成功を `.deploy/last_backup_ok` / `last_offsite_ok` に記録する。
- staging は `BACKUP_ENABLED=0`（合成データ）。`ops.env` は `<DEPLOY_USER>` のみ読める 600。
- **復元**: `restore.sh`。`--verify-only` は稼働中でも可（`integrity_check` / `foreign_key_check` / テーブル別行数を表示、本番 DB には触れない）。本番置換は `RESTORE` 入力の確認つきで app 停止 → 現 DB を `*.before-restore-<時刻>` へ退避 → 配置 → 起動。**復元予行を初回と月1回**行う。
- **§10 との関係**: `pre-migrate-*`（適用前・最大3世代/30日）は migration 専用で、本節の日次・オフサイトとは別物。混同せず、相互に削除しない。
- **`APP_PEPPER` はバックアップに含まれない**（DB 外の設定値）。**別経路で保管**しないと復元してもログインできない。Caddy の証明書 volume（`caddy-data`）はバックアップ対象外（再発行される）。

### 監視

- `monitor.sh`（systemd timer、日次。実行時刻は運用ポリシー）。ルート FS / Docker 領域の使用率、journald サイズ、app の health・proxy の稼働、DB volume サイズ、バックアップ/オフサイトの最終成功からの経過、証明書残日数、再起動要否を確認する。警告・重大のしきい値は `ops.env` で設定する。
- **通知・ping は差し替え可能な外部エンドポイント**として扱う:
  - `NOTIFY_URL`（異常通知）/ `BACKUP_PING_URL`・`MONITOR_PING_URL`（死活・沈黙検知）は **`ops.env` の設定のみで決まり、空なら送信しない**。設定が無くても backup / monitor は正常動作する。
  - 通知は HTTP POST、死活 ping は HTTP GET の互換インターフェースとして扱い、特定サービスに依存しない。送信形式を `notify()` / `ping()` の1関数に隔離し、その関数だけを差し替える（monitor / backup 本体は変更しない）。
- 本文にはディスク%等のみを含め、**秘密値を含めない**。重大（CRIT）は非0終了し、`systemctl --failed` に出る。
- **外形監視**（VPS の外から `https://<DOMAIN>/healthz`）を必須とする。`monitor.sh` は同一ホストで動くため、VPS 自体の停止を代替できない。

### Argon2 パラメータの調整

- `scripts/tune_argon2.py` を `deploy/deploy.sh tune [--min-ms --max-ms]` で実行する。メモリコスト・時間コストの候補ごとに「1回の hash 中央値」と「`ARGON2_MAX_CONCURRENCY` 並列時の中央値」を測り、**目標範囲（既定 200〜500ms）内でメモリ最大（同じなら time_cost 最大）**を推奨する。
- 非 `test` の下限（`ARGON2_TIME_COST>=2` かつ `ARGON2_MEMORY_KIB>=19456`）は §8（D-08）どおりで、下回ると起動しない。変更が効くのは**新規作成の hash のみ**（既存 hash は自身のパラメータで検証）。利用者が増える前に決める。**実機で測る**。

### HSTS とイメージ固定

- HSTS は Caddyfile（`deploy/proxy/Caddyfile`）で付与する（§8）。staging / production は短い `max-age` から開始し、安定後に**段階的に**延長する（戻せないため）。
- **Caddy イメージは digest 固定**する（マルチアーキ manifest list digest）。更新は手動で digest を確認する。自動化する場合は Dependabot / Renovate を別途有効化する（アプリ Image は既に digest 配備。残る可変は Caddy のみ）。
- DNS は **A レコードのみ**（AAAA は作らない）。伝播前に起動を繰り返すと Let's Encrypt のレート制限に当たる。
- **digest / tune の共有方針**: **アプリ Image digest は staging と production で同一**（`deploy` workflow が強制）。**Caddy digest も共通**で使う。Argon2 の tune 値は CPU 依存のため、staging と production が**同一 VPS プランなら共通**、異なる場合は**各環境で実測**する（公開前に production でも `tune` を1回実行して同程度を確認）。

### セキュリティ

- GitHub アカウントの 2FA を必須とする。GHCR のトークンは最小スコープ（可能なら fine-grained token / GitHub App）とし期限を管理する。CI 用の SSH 鍵はデプロイ権限を持つため、専用鍵・環境分離・最小権限を徹底する。
- 依存とイメージの更新は手動で確認する（自動化する場合は Dependabot / Renovate を別途有効化する）。`cleanup` workflow は GHCR の古いバージョンを削除する（使用中の digest は残す）。

### 付随資料

- 手順書 `docs/deployment.md`（VPS 初期設定・GitHub Secrets・初回デプロイ・通常リリース・ログ運用・バックアップ/復元・監視・`tune`・公開前チェックリスト）。**正本は本節**で、手順書はその実施手順を持つ。

### 対象外

- 宣言的 IaC（Terraform 等）・Kubernetes・複数リージョン・CDN。メトリクスは別途運用設計で扱い、外部送信・分析はプライバシー方針により行わない（§17）。`worker > 1` と rate limit の共有ストア移行（D-24 の前提変更）。
- 実際のドメイン・GHCR owner/repo・各種トークン・鍵の値（環境依存。リポジトリに置かない）。
