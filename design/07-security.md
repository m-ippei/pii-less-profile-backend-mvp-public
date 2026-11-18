# 設計 07: セキュリティ

> 関連: [ドメインモデル](03-domain-model.md) / [API契約](05-api-contract.md) / [エラー処理](06-errors.md) / [データベース](08-database.md) / [インフラ・運用](10-infra-ops.md) / [テスト](11-testing.md)

---

## 8. セキュリティ

### 不変規則

1. `user.id`・`profile.id`・所有者情報を、URL・HTML・公開API・ログに出さない
2. 生password、`login_id`、session token、`recovery_key`、CSRFトークン、`APP_PEPPER`を、DB・ログ・例外・分析・Referer・URLに出さない（作成・回復応答の返却を除く）
3. **URL（path・query・fragment）に認証情報を載せない**。`login_id`だけで認証しない
4. HTTPS、Secure/HttpOnly Cookie、CSRF、統一した認証エラーを維持する
5. 入力値をエラー応答にechoしない
6. IPアドレスの生値をDBへ保存しない
7. AgentやOAuth用のトークン、パスワード・`recovery_key`を受け取るAPIは、MVPでは作らない

### 秘密値と保存

| 値 | 生成（CSPRNG） | 保存 |
|---|---|---|
| `public_id` | `p_`＋Base58・16文字（計18文字） | 平文（公開値） |
| `login_id` | Base58・24文字 | HMAC-SHA-256（`login_id`ラベル）のHEX64 |
| 初期パスワード | Base58・16文字 | Argon2id |
| session token | Base58・32文字 | HMAC-SHA-256（`session`ラベル）のHEX64 |
| `recovery_key` | Base58・32文字 | HMAC-SHA-256（`recovery_key`ラベル）のHEX64 |
| CSRFトークン | 32バイトのbase64url | 保存しない（cookieのみ） |
| 内部ID | UUIDv7 | 平文（外部に出さない） |

- Base58はBitcoin alphabet（`0OIl`を含まない）。UUIDv7は`uuid.uuid7()`（Python 3.14）
- HMAC: `HMAC-SHA256(key=APP_PEPPER, msg=label + ":" + value)`。ラベルは`login_id` / `session` / `recovery_key` / `ratelimit` / `ip_daily`
- 接尾の表示識別文字（`_`以降）は認証対象外。`login_id`・`recovery_key`は「最初の`_`より前」を抽出してから検証・HMAC・rate limitキー計算を行う（抽出を後回しにすると、同一Base58に別suffixでaccount単位レート制限を回避できる）。正規入力は固定長Base58のみ（§6）。「最初の`_`より前」が固定長Base58を成す場合に限り接尾付き入力を受理するのは、誤送信・旧クライアント互換のための**防御的抽出**であり、契約上の入力形式ではない
- **pepper**: 環境変数`APP_PEPPER`（32バイト以上のbase64url）。起動時検証。**MVPはローテーション不可**。紛失・変更すると全login_id・recovery_key・Sessionの照合が不能になる
- DBバックアップにはHMAC化した資格情報とArgon2idハッシュが含まれる。保管先のアクセスを制限し、`APP_PEPPER`とは別経路で保管する。日次バックアップとその保管は [§18](16-deployment.md) を正本とする
- `recovery_key`はCSPRNG生成の高エントロピー値であり、HMACによる直接一致検索でよい（Argon2idの遅延処理は不要）
- Argon2id: `argon2-cffi`の既定（time=3, memory=64MiB, parallelism=4）を起点に、目標約200〜500msへ設定で調整。同時実行は`ARGON2_MAX_CONCURRENCY`（既定2）のsemaphoreで制限。**枠が埋まったリクエストは待たせず、429または503を返す**。**本番相当の環境（`ENV=test`以外）では、OWASP推奨相当（`ARGON2_TIME_COST>=2`かつ`ARGON2_MEMORY_KIB>=19456`）を下回る設定を受け付けず、起動時に失敗する（fail-closed）**。**Argon2 呼び出し（`hash` / `verify` / `dummy_verify`）は DB transaction の外で行い、SQLite の書き込みロックを保持しない**（[§11](10-infra-ops.md)。枠が埋まれば待たせず 429 または 503）

### パスワード規則

`new_password`は12〜128文字（NFKC正規化後のコードポイント）、構成ルールなし、現パスワードと同一不可。ログイン時の`password`は1〜128（DoS抑止のため上限のみ）。ハッシュはNFKC正規化後の値に対して行う。

### 認証フロー

**CreateSession（ログイン）**
1. 入力検証（`login_id`の「最初の`_`より前」を抽出）→ rate limit
2. `HMAC(login_id)`で資格情報を検索
3. パスワードをArgon2検証。**該当なしの場合もダミーハッシュに対して1回検証**して処理時間を揃える
4. 失敗はH-3 / H-4の同一応答
5. 成功: 新Sessionを作成。有効な旧Sessionが提示されていれば失効。session cookieとCSRF cookieを再発行（ローテーション）

**RecoverPassword**
1. 入力検証（`recovery_key`の「最初の`_`より前」を抽出してから形式検証）→ rate limit（IPのみ）
2. `HMAC(recovery_key)`で`recovery_credentials`を直接検索。**ダミー検証は不要**（高エントロピーなCSPRNG値のため）
3. 該当なしはH-7
4. 成功: 新しい`login_id`・新しいパスワード・新しい`recovery_key`を生成し、1 transactionで更新。全Session失効。Sessionは発行せず、Cookieを削除

**ChangePassword**: Session認証 → 入力検証 → rate limit → 現パスワード検証（誤りはH-14）→ 資格情報更新＋新しい`recovery_key`発行 → 新Session発行、CSRFローテーション

### Cookie・CSRF・ヘッダー

**Cookie**（`__Host-`接頭辞でSecure・`Path=/`・Domainなしが強制。開発環境もHTTPSで動かし、`Secure`を外す設定は作らない）

| Cookie | 属性 |
|---|---|
| `__Host-ap_session=<token>` | `Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=1209600` |
| `__Host-csrf=<token>` | `Path=/; Secure; SameSite=Lax`（JSが読むため`HttpOnly`なし） |

無効Session応答・ログアウトでは`__Host-ap_session`を削除。**GET応答（静的ファイル配信を含む）**で`csrf` cookieが無ければ発行。ログイン・パスワード変更・回復・ログアウトの成功時に再発行。

**CSRF**（`GET`以外の全リクエスト）: 次を**すべて**満たす。違反はH-12。

1. `Origin`ヘッダーが`PUBLIC_BASE_URL`のオリジンと一致（欠落も違反）
2. 本文を持つリクエストは`Content-Type: application/json`
3. `X-CSRF-Token`ヘッダーと`__Host-csrf` cookieが両方非空で、定数時間比較で一致

**応答ヘッダー（全応答）**: `X-Content-Type-Options: nosniff` / `Referrer-Policy: no-referrer` / `X-Robots-Tag: noindex, nofollow` / CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`。HSTSはリバースプロキシで付与し、短い `max-age` から段階的に延長する（[§18](16-deployment.md)）。

**Cache-Control**: 秘密値を含む応答・認証系・`/me/*`・認証系HTMLは`no-store`。公開プロフィールも`no-store`（unpublishの即時反映）。

### Rate limit（暫定値。429 `RATE_LIMITED` ＋`Retry-After`）

> **公開版の記載方針**: 以下の回数・窓・key 上限は公開版の既定値の例。本番環境の実値は環境ごとに tune し、非公開の運用手順で管理する。実装の既定値はコードを参照。

| 対象 | 制限 | key |
|---|---|---|
| ログイン（account単位） | 5回/分 | IP ＋ `HMAC(login_id)`（`ratelimit`ラベル） |
| ログイン（IP単位） | 20回/分 | IP のみ |
| 回復 | 5回/分 ＋ 20回/時 | IP のみ |
| パスワード変更 | 5回/分 | session id |
| Profile作成 | 10回/時 | IP |

- **全試行＝入力形式検証を通過した認証試行**を計上する（成功・失敗を区別しない。区別による情報漏えいを避ける）。スライディングウィンドウ。計上・判定は入力検証の後（判定順どおり）
- **ログインのIP単位制限は必須**: 1つのIPからlogin_idを変え続ける総当たり攻撃を防ぐ
- 実装は`RateLimiter`ポートの背後。MVPはプロセス内メモリ（単一worker前提。期限切れkeyは検査時に破棄し、**最大 `RATE_LIMIT_MAX_KEYS`（既定10,000）key**。[§11](10-infra-ops.md)）
- **`worker > 1` を禁止する**: インメモリrate limitがworkerごとに分断され、制限が無効化される
- クライアントIPは`TRUSTED_PROXY_COUNT`個の信頼プロキシ分を`X-Forwarded-For`の右から除いて求める
- **メモリ有界化**: 各 key は窓内のタイムスタンプを最大 `limit` 個（その制限の回数）だけ保持する。窓内で既に `limit` 個に達した key への新たな試行は計上せず拒否する（`key数 × limit` でメモリ上限が定まる）
- **上限到達時の挙動（fail-closed）**: key 数が最大（`RATE_LIMIT_MAX_KEYS`、既定10,000）に達したら新規 key は登録せず拒否し、既存 key の判定は継続する。到達は運用ログで観測する
- **`Retry-After` の正直化**: 「その key の最古の計上が窓から落ちるまでの残り秒」を返す（最低1秒）。上限到達で対象 key が無い場合は窓幅を返す
- **並行性**: エンドポイントは `def`（threadpool）で、インメモリ実装は共有可変状態のため単一 `threading.Lock` で判定・計上を直列化する
- **監視**: `rate_limited` イベントを制限種別（`kind`）ごとに記録する。**上限到達（fail-closed）は運用ログ（`rate_limit_cap_reached`）で数値（`count`）のみ観測**する。**検査時 key 破棄（evicted）の集計は行わない**（日常処理でノイズになりやすく、原因究明への寄与が小さい。必要になれば後から追加する）。限界値（回数・窓・key 上限）は1箇所の設定に集約し変更可能にする（key 上限は `RATE_LIMIT_MAX_KEYS`、[§11](10-infra-ops.md)）

### ログ・漏えい防止

ログの設計（チャネル・項目・形式・相関・保持・運用・プライバシー）は **[ログ設計](15-logging.md) §17 を正本**とする。本節は不変規則のみを定め、詳細は重複させない。

- 秘密値・生IP・`user.id`/`profile.id`・本文・ヘッダー・Cookie・クエリ・例外メッセージをログに出さない（本節「不変規則」）。uvicorn のアクセスログと Caddy のアクセスログは無効化する。
- IP の相関は `ip_daily`（`HMAC-SHA256(key=APP_PEPPER, msg="ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)`、保持30日）のみで行い、日をまたぐ追跡を構造的に不可能にする。
- ログの種類・イベント・項目の追加/変更は設計変更（`design/14-dod-conventions.md` §16 の Security policy）とする。
- 検証は `tests/security` と `tests/api/test_access_log.py` / `tests/security/test_security_event_log.py` が行う。