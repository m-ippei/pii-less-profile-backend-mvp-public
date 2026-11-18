# 設計 11: テスト

> 関連: [概要](01-overview.md) / [ドメインモデル](03-domain-model.md) / [エラー処理](06-errors.md) / [セキュリティ](07-security.md) / [完了定義](14-dod-conventions.md)

---

## 12. テスト

| 層 | 方針 |
|---|---|
| domain | 標準ライブラリのみ。`FakeClock`固定。ルールと時刻属性の更新条件を網羅 |
| repository | 一時ディレクトリの実SQLiteファイル（WAL）。一意制約・CHECK・CASCADE・部分一意索引を検証 |
| migration | 一時ディレクトリの実SQLiteファイルで検証。アップグレード、FK/CASCADE、runner、バックアップ整理 |
| application | 途中失敗でrollbackされること（原子性）、`recovery_key`ローテーション、再試行（H-8）を検証 |
| api | `TestClient`。HTTP契約（status/code/body/header/Cookie）とH表全件を1ケース以上 |
| security | ログ・標準出力を走査、DBダンプにダミー秘密値が無い、エラー応答に入力値が無い、Cookie属性・CSRF・ヘッダー、セキュリティイベントログの許可リスト、`static/`配下のJSの禁止API、静的HTMLの`style=`属性 |
| architecture | 自作テストでimportを走査: domainが標準ライブラリのみ、Profile ↔ Identity相互参照なし、`datetime.now`等の直接呼び出しなし |
| e2e | Playwright＋コンテナ内Chromium。実ブラウザでJS挙動（AC-6/7/12等）とCSRF連携を検証する。**UIを含むACの一次証拠**。API網羅はapi層に委ねる。`make e2e`（`make check`には含めない。`ENV=test`・専用composeプロジェクト）。`tests/e2e/` への混入は architecture テスト（`tests/architecture/`）で検出する |

- 時刻は`Clock`ポートで固定する（`freezegun`等は使わない）
- 必須シナリオ: 公開/非公開、他人の更新拒否（`/me`のみ）、正誤ログイン、期限切れ・失効Session、パスワード変更後の旧Session拒否と`recovery_key`ローテーション、回復成功と旧`login_id`/`recovery_key`の失効、全Session失効、一意衝突再試行、秘密値の漏えい無し

**層の責務（一次検証層の考え方）**

- `api`: HTTP契約（status/code/body/header/Cookie）とH表。
- `static`（`architecture` / `security`）: セキュリティ不変規則の**不在検証のみ**（禁止APIの不使用、CSP、URLに秘密値なし、ログ漏えいなし）。成果物テキストの「存在」確認（HTML/JSの文字列一致）は行わない。
- `e2e`: ユーザー動線・JS挙動・CSRF連携・XSS無効化。UIを含むACの一次証拠。
- `manual`（`make e2e-headed`）: UI主体マイルストーンの目視確認。任意のデバッグではなく**記録必須のゲート**（[完了定義](14-dod-conventions.md) §15）。`page.pause` / `PWDEBUG` を使う。

### 受け入れ基準

「ACに対応するテストがある」とは、**一次検証層に、ACを直接観測するテストがあること**を指す。補助層のテストだけでは充足とみなさない。

| ID | 基準 | 一次検証層 |
|---|---|---|
| AC-1 | 作成時に秘密値が一度だけ返り、以後どのAPIでも生値が再表示されない | api（補助: security） |
| AC-2 | 非公開・存在しない`public_id`は同一の404で区別できない | api |
| AC-3 | 公開DTOは`public_id`,`content`のみ | api |
| AC-4 | 本人以外がプロフィールを変更する経路が無い。未ログインは401 | api（補助: architecture） |
| AC-5 | ログインは`login_id`とパスワードの両方が必要。どちらの誤りも同一応答・同程度の処理時間 | api + application |
| AC-6 | パスワード変更後、旧Sessionは401、新Sessionは有効。新しい`recovery_key`が発行され、旧`recovery_key`は使えない。保存確認画面が表示され、確認チェック前は遷移不可 | e2e（UIゲート）+ api（トランザクション） |
| AC-7 | 回復後、全Sessionが無効。新しい`login_id`・パスワード・`recovery_key`が発行され、旧値はいずれも使えない。保存確認画面が表示され、確認チェック前は遷移不可 | e2e（UIゲート）+ api（トランザクション） |
| AC-8 | 秘密値の平文がDB・ログ・例外・（該当応答以外の）レスポンスに存在しない。IPアドレスの生値がDBに存在しない | api + security |
| AC-9 | rate limit超過は429＋`Retry-After` | api |
| AC-10 | `content`が6,000文字超は422。空（空白のみ含む）での公開は拒否 | api + domain |
| AC-11 | 公開→非公開→再公開で`public_id`は不変、`published_at`は再公開時に再設定 | api |
| AC-12 | `content`に`<script>`等を含んでも公開ページで実行されない | e2e（補助: architecture） |
| AC-13 | URL（path・query・fragment）に認証情報を載せるAPI・画面が存在しない | architecture + e2e |
| AC-14 | `/healthz`が認証・CSRFなしで`{"status":"ok"}`を返す | api + e2e |