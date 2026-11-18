# DESIGN — 統合設計書

- version: 1.6.8
- 最終更新: 2026-10-14

---

## 1. 概要

**ビジョン**: 個人が氏名や連絡先を出さずに匿名プロフィール（価値観・悩み・希望）を公開し、興味を持った相手から必要な段階でオファーを受けられる場。

**MVP仮説**: 「個人は匿名プロフィールなら本音を書いて公開でき、第三者はそれを閲覧できる」。Offerの送受信は検証しない。

**MVP完了の定義**: 作成 → 本人ログイン → 編集 → 公開 → 第三者閲覧 → パスワード変更 → リカバリーキーによる復旧が一連で動く。

### 用語

| 用語 | 意味 |
|---|---|
| Owner | プロフィールの所有者 |
| Viewer | 公開プロフィールの閲覧者 |
| `public_id` | 公開URLの識別子。`p_`＋Base58 16文字 |
| `login_id` | ログインID。作成時に生成される秘密値。単体では認証されない。認証対象はBase58 24文字 |
| 初期パスワード | 作成時に生成されるBase58 16文字 |
| `recovery_key` | ログインID・パスワードを再発行するための単一の高エントロピー秘密値。パスワード変更・回復のたびにローテーション。認証対象はBase58 32文字 |
| 表示識別文字 | `login_id`・`recovery_key`の`_`以降に付ける人間可読の管理用ラベル。認証には使われず、クライアント側のみで保持されサーバーには送信されない |
| 回復不能 | `login_id`とパスワードの両方、かつ`recovery_key`も失った状態 |

---

## 2. MVP範囲

### 機能

- 匿名プロフィールの作成・編集・公開/非公開・公開閲覧
- `login_id`＋パスワードによる本人ログイン、ログアウト
- パスワード変更（`recovery_key`の再発行を伴う）、`recovery_key`によるログインID・パスワードの再設定
- 管理者による削除は運用上の直接DB操作のみ。削除API・管理画面・管理者ロールは作らない
- 不適切投稿への対応は運用手順のみ。通報API・管理画面は作らない

### プロフィール本文

- `content`という単一の任意テキスト。NFC正規化後のUnicodeコードポイント数で**6,000文字以内**
- 前後の空白は除去して保存。空白のみは空。改行はLFに正規化。`\n` `\t` 以外の制御文字は不可
- 意図的に単一の自由記述。項目分割は行わず、フロントエンド側の入力補助に委ねる
- 公開には`content`が非空であることが必要。公開中も編集でき、編集後も公開状態を保つ。履歴は持たない

### 状態遷移

`draft -> published -> draft` のみ。1人1プロフィール。`public_id`は作成時に確定し不変。

### 閲覧経路

Ownerが共有した公開URLのみ。一覧・検索は無い。

### UI範囲

最小の静的HTML画面＋外部JS。テンプレートエンジン・SPAフレームワークは使わない。公開ページは`noindex`。

### MVP外

Offer（検索・送付・受信・返信）、メールログイン、ユーザー名、複数プロフィール、企業確認、アプリ内通報・ブロック、パスキー、OAuth/MCP/Agent向けトークン、フォロー、リプライ、内製メッセージング、個人情報の保存、外部IDプロバイダー連携。

---

## 3. アーキテクチャ

### Contextと依存

```
Identity Context                          Profile Context
User(Account Aggregate) -- user_id -->    Profile Aggregate
Session Aggregate ------- user_id -->     (所有者を参照するだけ)
```

| Context | 責務 |
|---|---|
| Identity | 所有者、ログインID・パスワード・回復用の鍵、認証、回復、セッション |
| Profile | 匿名本文、公開状態、公開表示 |

- Context間は`user_id`だけで参照。Entityを共有・ネストしない。**Profile ↔ Identityの相互importを禁止**
- Context跨ぎのuse case（CreateProfile）は`src/app/`に置く
- 現在ユーザーの解決: Identityが`Principal(user_id, method, scopes)`を作り、Profile側は`user_id`だけを受け取る（結線は`main.py`）。**MVPでは`method`・`scopes`は未使用**（Identityが値を設定するが、Profile側は参照せず認可分岐に使わない）。将来の認可は`scopes`を境界として`authorization`に追加する
- 認証（Identity内`authentication`）と将来の認可（`authorization`）は論理分離。MVPは前者のみ

### 構成

```
src/
  main.py                 # 結線（Principal解決、Router、ミドルウェア）
  app/                    # Context跨ぎのuse case（CreateProfile）
  identity/{domain,application,infrastructure,presentation}/
  profile/{domain,application,infrastructure,presentation}/
  shared/{db,security,clock,errors,ratelimit,web}/
static/                   # HTML/JS/CSS（インライン禁止）
migrations/               # NNN_name.sql、順方向のみ
scripts/migrate.py
tests/{domain,application,integration,api,architecture,security,support}/
```

- 依存方向: `presentation → application → domain`、`infrastructure → domain/application`。`domain`は標準ライブラリのみ
- `Principal`（`user_id`,`method`,`scopes`）は`shared/web`に定義。`profile/presentation`は`require_principal`（callable）を引数で受け取り、実装は`identity`が提供して`main.py`で結線
- `tests/support/` はテストダブル（`FakeClock`・InMemory リポジトリ等）の置き場。層ごとのテスト配置は `11-testing.md` §12 に従う

### Aggregate

| Root | 内部要素 / 一貫性 |
|---|---|
| `User`（Account） | `LoginCredential`と`RecoveryCredential`。`RecoveryCredential`は常に1件で、パスワード変更・回復・作成の成功時に原子的に再発行 |
| `Profile` | `user_id`,`public_id`,`content`,状態。本文と公開可否・状態遷移を原子的に守る |
| `Session` | 1ログイン状態。作成・touch・失効・有効性を単独で守る |

**複数Aggregateを同一DB transactionで扱う例外（許容）**: 単一SQLiteで、全体原子性が安全要件のため。

| Use case | 範囲 |
|---|---|
| CreateProfile | User + Profile + RecoveryCredential + Session |
| ChangePassword | Account更新（LoginCredential + RecoveryCredential） + 全Session失効 + 新Session |
| RecoverPassword | Account更新（LoginCredential + RecoveryCredential） + 全Session失効 |

---

## 派生メモ（分割のために書いた要約。新たな規定は含まない）

- `domain` 層で `import fastapi` / `import sqlite3` を書いたらアーキテクチャ違反。
- `profile` から `identity` を `import` した時点で違反。逆も違反。
- Context跨ぎの処理は `src/app/` と `src/main.py` に限定。それ以外が `user_id` を往復させるコードを書かない。
- 上記3件は、いずれも本章「依存方向」「Context間は`user_id`だけで参照」の受け渡しであり、原典の記述。

---

## 4. ドメインモデル

### Entity

| Entity | 属性 |
|---|---|
| User | `id`,`created_at`,`updated_at` |
| LoginCredential | `user_id`,`login_id_hash`,`password_hash`,`password_changed_at`,`created_at`,`updated_at` |
| RecoveryCredential | `user_id`,`recovery_key_hash`,`created_at`,`updated_at` |
| Session | `id`,`user_id`,`token_hash`,`created_at`,`last_seen_at`,`expires_at`,`revoked_at` |
| Profile | `id`,`user_id`,`public_id`,`status`,`content`,`published_at`,`created_at`,`updated_at` |

`Account` は `User` + `LoginCredential` + `RecoveryCredential` を束ねる Aggregate で、`AccountRepository` が取得・保存する単位（`02-architecture.md` §3「User（Account）」参照）。単独の Entity ではない。

### Value Object

| VO | 規則 |
|---|---|
| `Password` | NFKC正規化後12〜128コードポイント。変更時は現パスワードと同一不可（初期パスワードは生成値16文字） |
| `ProfileContent` | NFC、CRLF→LF、前後空白除去、6,000コードポイント以内、`\n``\t`以外の制御文字不可。空白のみ＝`""` |
| `LoginId` | Base58固定長24文字（認証対象）。表示では`_`以降に表示識別文字を付与できる（認証対象外） |
| `RecoveryKey` | Base58固定長32文字（認証対象）。表示では`_RECOVERY-KEY`＋表示識別文字を付与できる（認証対象外） |
| `PublicProfileId` | `p_`＋Base58固定長16文字（計18文字） |
| `SessionToken` | Base58固定長32文字 |
| `UserId` | UUIDv7・36文字。外部非公開 |
| `ProfileId` | UUIDv7・36文字。外部非公開 |

生の秘密値は生成・検証時だけ存在し、Entityや永続モデルに保持しない。

### Domain rule

- `Profile.publish()`は更新後の`content`が非空でなければ失敗。`update()`はpublishedのまま可能。`publish`はdraftから、`unpublish`はpublishedからだけ実行できる（不正遷移はドメイン例外）
- Application層は「目標状態＝現状態」を**no-op成功**として扱う。本文更新と状態変更は1 transaction
- 全項目が同値（正規化後）の更新は書き込まず`updated_at`も更新しない
- `Session.is_valid(now)` = `revoked_at is None and now < expires_at`
- `RecoveryCredential`に有効期限・使用済みフラグは無い。**常に最新の1件だけが有効**で、一致すれば即座に使用できる
- `login_id_hash`・`recovery_key_hash`・`token_hash`・`public_id`は一意
- Sessionは14日の**絶対期限**（延長しない）。ログイン時に有効な旧Sessionが提示されたら失効させる
- 認証対象は「最初の`_`より前」の固定長Base58部。接尾の表示識別文字は認証対象外で、抽出はHMACハッシュ化・rate limitキー計算より前に行う
- Context間で渡す識別子は`user_id`のみ。`Principal`の`method`・`scopes`はMVPでは使用せず、`scopes`による認可分岐は行わない（将来`authorization`の境界。`02-architecture.md` §3）

### 時刻の意味

すべてUTCのtimezone-aware。

| 属性 | 更新条件 |
|---|---|
| `created_at` | 作成時に一度だけ |
| `updated_at` | `User`/`Profile`/`LoginCredential`/`RecoveryCredential`の永続的な可変属性が**実際に変わった成功時だけ** |
| `published_at` | draft→publishedの成功時に設定。unpublishで`null`。再公開で再設定 |
| `password_changed_at` | 初期発行時、通常変更時、回復成功時 |
| `last_seen_at` | Session作成時に設定。以後、有効な認証済みリクエストで前回から5分以上経過した時だけ更新 |
| `revoked_at` | ログアウト・パスワード変更・回復・異常失効時に、未失効なら一度だけ。期限到来では設定しない |
| `expires_at`（Session） | Session作成時14日後。以後変更しない |

### Port

| Port | 責務 | 実装層 |
|---|---|---|
| `AccountRepository` | Account取得（user_id / `login_id_hash` / `recovery_key_hash`）、保存 | infrastructure |
| `ProfileRepository` | user_id / public_idで取得、保存、公開中のみ取得 | infrastructure |
| `SessionRepository` | token hashで取得、保存、touch、user単位の一括失効 | infrastructure |
| `UnitOfWork` | transaction境界（`BEGIN IMMEDIATE`） | infrastructure |
| `PasswordHasher` | Argon2id hash / verify、ダミー検証 | shared/security |
| `SecretHasher` | 用途ラベル付きHMAC-SHA-256（login_id / session / recovery_key / ratelimit / ip_daily） | shared/security |
| `SecretGenerator` | CSPRNGで各秘密値を生成 | shared/security |
| `IdGenerator` | UUIDv7 | shared |
| `Clock` | UTC現在時刻（テストで固定） | shared/clock |
| `RateLimiter` | key＋窓で判定（暫定インメモリ） | shared/ratelimit |

Repositoryは取得・保存・失効の抽象で、SQL・HTTP・Cookie・FastAPIを知らない。`Account` の取得は `user_id` / `login_id_hash` / `recovery_key_hash` の3経路で行い、いずれも一意索引（`user_id` は主キー、`login_id_hash`・`recovery_key_hash` は UNIQUE）を引く。

---

## 5. ユースケースとトランザクション

| Context | Use case | 概要 |
|---|---|---|
| app | CreateProfile | User・Profile・資格情報（login_id/password/recovery_key）・初回Sessionを作成し、初回秘密値だけ返す |
| Profile | GetPublicProfile | publishedだけを公開DTOへ変換（それ以外は「存在しない」） |
| Profile | GetMyProfile / UpdateMyProfile | `user_id`のProfileを取得 / `content`と状態を更新 |
| Identity | CreateSession | `AuthenticationMethod`で認証しSessionを作成 |
| Identity | DeleteCurrentSession | 現Sessionを失効（冪等） |
| Identity | AuthenticateSession | Cookie tokenから`Principal`を作る |
| Identity | ChangePassword | 現パスワード検証 → 資格情報変更 → 新`recovery_key`発行 → 全Session失効 → 新Session |
| Identity | RecoverPassword | `recovery_key`のハッシュ一致でAccountを直接特定 → 新`login_id`・新パスワード・新`recovery_key`を発行 → 全Session失効（Sessionは発行しない） |

`AuthenticationMethod`ポートの実装はMVPでは`PasswordMethod`（login_id + password）のみ。

### Transaction（すべて`BEGIN IMMEDIATE`）

| Use case | 同一transactionで行うこと |
|---|---|
| CreateProfile | `users` → `login_credentials` → `recovery_credentials` → `profiles` → `sessions`。一意制約衝突はrollbackして再生成、最大5回 |
| ChangePassword | 資格情報更新 → `recovery_credentials`更新 → 全Session失効 → 新Session作成 |
| RecoverPassword | `recovery_key_hash`一致でuser_idを特定 → `login_credentials`更新 → `recovery_credentials`更新 → 全Session失効 |
| UpdateMyProfile | `content`更新と状態遷移を1回で行い、実変更がある時だけ`updated_at`（公開時は`published_at`も）を更新 |
| DeleteCurrentSession | 現Sessionに`revoked_at`（未失効なら一度だけ） |
| CreateSession | 新Session作成＋提示された有効な旧Sessionの失効 |

- **認証・検証（Argon2）は transaction の外で行う**: `hash` / `verify` / `dummy_verify` は保存操作ではないため `BEGIN IMMEDIATE` の中に入れない（transaction 中に書き込みロックを保持しない。`design/10-infra-ops.md` §11）。`CreateSession` は「`HMAC(login_id)` での account 取得＋パスワード検証（該当なしはダミー検証）」を外で行い、transaction は新Session作成と旧Session失効だけを囲む。`ChangePassword` は「account 取得＋現パスワード検証＋新パスワードの同一判定・ハッシュ」を外で行い、transaction は資格情報・回復資格情報の更新と全Session失効・新Session作成を囲む。`RecoverPassword` は新パスワードのハッシュだけを外で作る。読み（`SELECT`）は transaction を必須としない（リクエスト単位の接続で autocommit してよい）。
- **変更系の競合検知**: 検証〜更新の間で対象が変化しうるため、`ChangePassword` は transaction 内で account を再読し、検証に使った `password_hash` と一致することを確認する。変化していれば `INVALID_CURRENT_PASSWORD`（H-14）で拒否する（`design/07-security.md` §8・`design/13-decisions.md` §14 D-37）。

---

## 6. API契約

### 共通規約

- APIは`/api/v1`。JSONのみ。未知フィールドは422。本文最大32KB
- 成功以外のエラー形式: `{"error":{"code":"...","message":"日本語の固定文言","fields":[{"field":"new_password","code":"too_short"}]}}`
- `fields`は422のみ。入力値・秘密値はechoしない。FastAPI標準の422応答は使わず独自handler
- 全応答に`X-Request-ID`（サーバー生成）
- `GET`以外はCSRF検証を受ける

### エンドポイント

| API | 認証 | 用途 | リクエスト本文 | 成功応答 |
|---|---|---|---|---|
| `POST /profiles` | 不要 | Profile・秘密値・Session作成 | `{}` | 201 `{public_id, login_id, initial_password, recovery_key}` ＋Set-Cookie ＋`no-store` |
| `GET /profiles/{public_id}` | 不要 | 公開中のみ取得 | — | 200 `{public_id, content}` |
| `GET /me/profile` | 要 | 自分のProfile | — | 200 `{public_id, status, content, published_at}` |
| `PATCH /me/profile` | 要 | 本文・状態の更新 | `{content?, status?}` | 200（`GET /me/profile`と同形） |
| `POST /sessions` | 不要 | ログイン | `{login_id, password}` | 204 ＋Set-Cookie |
| `DELETE /sessions/current` | 任意 | ログアウト（冪等） | — | 204 ＋Cookie削除 |
| `POST /me/password` | 要 | パスワード変更 | `{current_password, new_password}` | 200 `{recovery_key}` ＋新Session |
| `POST /recovery/password-resets` | 不要 | パスワード再設定 | `{recovery_key, new_password}` | 200 `{login_id, recovery_key}` ＋Cookie削除（Session発行なし） |

**PATCH `/me/profile`**: 1項目以上必須。`content`未指定＝変更なし、`""`＝空にする、`null`は422。`status`は`draft`/`published`。同状態指定はno-op成功。公開可否は更新後の`content`で判定。

**入力形式**: `login_id`=Base58 24文字、`recovery_key`=Base58 32文字が**正規入力**（クライアントは固定長Base58のみを送信する）。認証対象はいずれも「最初の`_`より前」の固定長Base58部で、接尾の表示識別文字は無視する。接尾付き入力の受理は誤送信・旧クライアント互換のための**防御的抽出**であり、契約上の入力形式ではない（D-30）。`password`（ログイン）=1〜128、`new_password`=12〜128。形式違反は422。`GET /profiles/{public_id}`の形式違反は404。

### 公開DTOと返却する秘密値

- 公開DTOは`public_id`,`content`のみ。空の本文は`""`
- 秘密値を返してよいのは、作成応答（`login_id`,`initial_password`,`recovery_key`）と、パスワード変更応答（`recovery_key`）と、回復応答（`login_id`,`recovery_key`）だけ。いずれも`Cache-Control: no-store`

### 判定順

`413` → CSRF（`403`）→ 認証（`401`、必要なAPIのみ）→ 入力検証（`422`）→ rate limit（`429`）→ use case

**413がCSRFより前の理由**: 本文を読まずに上限で打ち切り、メモリ消費を防ぐための意図的な優先順位。

### Web画面

| Path | 内容 | Cache |
|---|---|---|
| `GET /new` | 作成画面。完了後に秘密値表示 | `no-store` |
| `GET /login` | `login_id`・パスワードのフォーム | `no-store` |
| `GET /profile-editor` | 編集・公開/非公開（未ログインはJSが`/login`へ遷移） | `no-store` |
| `GET /account` | パスワード変更（未ログインはJSが`/login`へ遷移） | `no-store` |
| `GET /recovery` | 回復フォーム | `no-store` |
| `GET /credentials-saved` | 秘密値保存確認画面（作成・パスワード変更・回復で共通） | `no-store` |
| `GET /profiles/{public_id}` | 公開ページの静的な殻。JSがAPIを呼び描画 | `no-store` |
| `GET /healthz` | ヘルスチェック。認証・CSRF不要。`{"status":"ok"}`のみ | `no-store` |

- 各画面は`static/`配下の素のHTMLファイルを返す。動的な内容はすべてJSが`fetch`でAPIを呼んで描画
- JavaScriptは`/static/`の外部ファイルのみ（インラインスクリプト禁止）。本文の表示は必ず`textContent`を使い、`innerHTML`等の禁止APIは使用しない。**この担保は「静的検査での禁止APIの不在」（`architecture` 層の不在検証）と「E2E での `<script>` 実行不成立」（`e2e` 層・一次証拠）の2段で示す**（[テスト](design/11-testing.md) §12 の一次検証層）
- GET応答で`__Host-csrf` cookieが無ければミドルウェアが発行する
- `PATCH /me/profile`の同時実行は**last-write-wins**。更新は1 transactionで直列化

---

## 7. エラー処理

| ID | 条件 | HTTP / code | 内部処理 |
|---|---|---|---|
| H-1 | Profileなし | 404 `PROFILE_NOT_FOUND` | 秘密情報なしで終了 |
| H-2 | Profile非公開 | H-1と同じ404 | 存在・状態を漏らさない |
| H-3 | login_id不明 | 401 `INVALID_CREDENTIALS` | ダミー検証で同一処理時間。rate limit計上 |
| H-4 | password誤り | H-3と同じ401 | rate limit計上 |
| H-5 | Session期限切れ | 401 `INVALID_SESSION` | Cookie削除。`revoked_at`は更新しない |
| H-6 | Session失効済み | H-5と同じ401 | Cookie削除 |
| H-7 | recovery_key不正／該当アカウントなし | 400 `INVALID_RECOVERY_CREDENTIAL` | 存在有無を漏らさない。rate limit計上 |
| H-8 | 一意制約衝突 | 外部へ露出しない | 再生成・再挿入を最大5回、各回rollback。尽きれば500 |
| H-9 | DBエラー | 500 `INTERNAL_ERROR` | rollback。request IDと安全な内部情報だけ記録 |
| H-10 | 入力不正 | 422 `VALIDATION_ERROR` | 文字数・形式・パスワード規則。値をechoしない |
| H-11 | Cookieなし／不明token | 401 `INVALID_SESSION` | Cookie削除 |
| H-12 | CSRF失敗 | 403 `CSRF_FAILED` | — |
| H-13 | rate limit超過 | 429 `RATE_LIMITED` ＋`Retry-After` | — |
| H-14 | `/me/password`の現パスワード誤り | 403 `INVALID_CURRENT_PASSWORD` | rate limit計上（Sessionは有効なので401にしない） |
| H-15 | `content`が空でpublish | 422 `PROFILE_NOT_PUBLISHABLE` | — |
| H-16 | 本文過大 | 413 `PAYLOAD_TOO_LARGE` | — |
| H-17 | DB busy（timeout） | 503 `SERVICE_UNAVAILABLE` ＋`Retry-After` | rollback |
| H-18 | 未知ルート・メソッド | 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED` | 同一JSON形式 |

### エラーコードと固定文言

| code | message（日本語固定文言） |
|---|---|
| `PROFILE_NOT_FOUND` | プロフィールが見つかりません |
| `INVALID_CREDENTIALS` | ログインIDまたはパスワードが正しくありません |
| `INVALID_SESSION` | セッションが無効です |
| `INVALID_RECOVERY_CREDENTIAL` | 回復用キーが正しくありません |
| `INTERNAL_ERROR` | 内部エラーが発生しました |
| `VALIDATION_ERROR` | 入力内容に誤りがあります |
| `CSRF_FAILED` | セキュリティチェックに失敗しました |
| `RATE_LIMITED` | リクエストが多すぎます。しばらくしてから再試行してください |
| `INVALID_CURRENT_PASSWORD` | 現在のパスワードが正しくありません |
| `PROFILE_NOT_PUBLISHABLE` | 公開できる内容がありません |
| `PAYLOAD_TOO_LARGE` | リクエスト本文が大きすぎます |
| `SERVICE_UNAVAILABLE` | サービスを一時的に利用できません |
| `NOT_FOUND` | リソースが見つかりません |
| `METHOD_NOT_ALLOWED` | 許可されていないメソッドです |

実装はこの表の定数からのみ返す。入力値・内部理由・秘密値を含めない。

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
- DBバックアップにはHMAC化した資格情報とArgon2idハッシュが含まれる。保管先のアクセスを制限し、`APP_PEPPER`とは別経路で保管する。日次バックアップとその保管は [§18](design/16-deployment.md) を正本とする
- `recovery_key`はCSPRNG生成の高エントロピー値であり、HMACによる直接一致検索でよい（Argon2idの遅延処理は不要）
- Argon2id: `argon2-cffi`の既定（time=3, memory=64MiB, parallelism=4）を起点に、目標約200〜500msへ設定で調整。同時実行は`ARGON2_MAX_CONCURRENCY`（既定2）のsemaphoreで制限。**枠が埋まったリクエストは待たせず、429または503を返す**。**本番相当の環境（`ENV=test`以外）では、OWASP推奨相当（`ARGON2_TIME_COST>=2`かつ`ARGON2_MEMORY_KIB>=19456`）を下回る設定を受け付けず、起動時に失敗する（fail-closed）**。**Argon2 呼び出し（`hash` / `verify` / `dummy_verify`）は DB transaction の外で行い、SQLite の書き込みロックを保持しない**（[§11](design/10-infra-ops.md)。枠が埋まれば待たせず 429 または 503）

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

**応答ヘッダー（全応答）**: `X-Content-Type-Options: nosniff` / `Referrer-Policy: no-referrer` / `X-Robots-Tag: noindex, nofollow` / CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`。HSTSはリバースプロキシで付与し、短い `max-age` から段階的に延長する（[§18](design/16-deployment.md)）。

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
- 実装は`RateLimiter`ポートの背後。MVPはプロセス内メモリ（単一worker前提。期限切れkeyは検査時に破棄し、**最大 `RATE_LIMIT_MAX_KEYS`（既定10,000）key**。[§11](design/10-infra-ops.md)）
- **`worker > 1` を禁止する**: インメモリrate limitがworkerごとに分断され、制限が無効化される
- クライアントIPは`TRUSTED_PROXY_COUNT`個の信頼プロキシ分を`X-Forwarded-For`の右から除いて求める
- **メモリ有界化**: 各 key は窓内のタイムスタンプを最大 `limit` 個（その制限の回数）だけ保持する。窓内で既に `limit` 個に達した key への新たな試行は計上せず拒否する（`key数 × limit` でメモリ上限が定まる）
- **上限到達時の挙動（fail-closed）**: key 数が最大（`RATE_LIMIT_MAX_KEYS`、既定10,000）に達したら新規 key は登録せず拒否し、既存 key の判定は継続する。到達は運用ログで観測する
- **`Retry-After` の正直化**: 「その key の最古の計上が窓から落ちるまでの残り秒」を返す（最低1秒）。上限到達で対象 key が無い場合は窓幅を返す
- **並行性**: エンドポイントは `def`（threadpool）で、インメモリ実装は共有可変状態のため単一 `threading.Lock` で判定・計上を直列化する
- **監視**: `rate_limited` イベントを制限種別（`kind`）ごとに記録する。**上限到達（fail-closed）は運用ログ（`rate_limit_cap_reached`）で数値（`count`）のみ観測**する。**検査時 key 破棄（evicted）の集計は行わない**（日常処理でノイズになりやすく、原因究明への寄与が小さい。必要になれば後から追加する）。限界値（回数・窓・key 上限）は1箇所の設定に集約し変更可能にする（key 上限は `RATE_LIMIT_MAX_KEYS`、[§11](design/10-infra-ops.md)）

### ログ・漏えい防止

ログの設計（チャネル・項目・形式・相関・保持・運用・プライバシー）は **[ログ設計](design/15-logging.md) §17 を正本**とする。本節は不変規則のみを定め、詳細は重複させない。

- 秘密値・生IP・`user.id`/`profile.id`・本文・ヘッダー・Cookie・クエリ・例外メッセージをログに出さない（本節「不変規則」）。uvicorn のアクセスログと Caddy のアクセスログは無効化する。
- IP の相関は `ip_daily`（`HMAC-SHA256(key=APP_PEPPER, msg="ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)`、保持30日）のみで行い、日をまたぐ追跡を構造的に不可能にする。
- ログの種類・イベント・項目の追加/変更は設計変更（`design/14-dod-conventions.md` §16 の Security policy）とする。
- 検証は `tests/security` と `tests/api/test_access_log.py` / `tests/security/test_security_event_log.py` が行う。

---

## 9. データベース

### 共通規約

- SQLite（WAL）、全テーブル`STRICT`（3.37以上）。接続ごとに`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`
- ID: UUIDv7を36文字のTEXTで保存（外部に出さない）
- 時刻: UTC固定長ISO 8601 `YYYY-MM-DDTHH:MM:SS.ffffffZ`（27文字）のTEXT。固定長なので文字列比較が時刻順と一致する。生成は`Clock`アダプターのみ
- 秘密値は`*_hash`のみ保存（HMACはHEX64文字、Argon2idはエンコード文字列）
- 本文の空は`''`（NULLにしない）。APIも`""`を返す
- 親`users`削除で子はすべてCASCADE削除（管理者の直接DB削除用）。アプリに削除機能は作らない

### スキーマ

```sql
CREATE TABLE users (
  id          TEXT PRIMARY KEY CHECK (length(id) = 36),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE login_credentials (
  user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  login_id_hash       TEXT NOT NULL UNIQUE CHECK (length(login_id_hash) = 64),
  password_hash       TEXT NOT NULL,
  password_changed_at TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
) STRICT;

CREATE TABLE recovery_credentials (
  user_id            TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  recovery_key_hash  TEXT NOT NULL UNIQUE CHECK (length(recovery_key_hash) = 64),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
) STRICT;

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  CHECK (expires_at > created_at)
) STRICT;
CREATE INDEX ix_sessions_user_active ON sessions(user_id) WHERE revoked_at IS NULL;

CREATE TABLE profiles (
  id             TEXT PRIMARY KEY CHECK (length(id) = 36),
  user_id        TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  public_id      TEXT NOT NULL UNIQUE CHECK (length(public_id) = 18),
  status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
  content        TEXT NOT NULL DEFAULT '' CHECK (length(content) <= 100000),
  published_at   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  CHECK ((status = 'published') = (published_at IS NOT NULL)),
  CHECK (status = 'draft' OR length(content) > 0)
) STRICT;
```

**設計メモ**:
- `content`のドメイン上限は6,000コードポイント。DBのCHECKは**意図的にこれより緩い**（100,000）。将来ドメイン側の上限だけを引き上げる場合に、SQLiteのテーブル再作成を伴うmigrationを避けるため
- `recovery_credentials`は1ユーザー1行。パスワード変更・回復・作成のたびに**同じ行をUPDATE**して`recovery_key_hash`を差し替える（3件保持・スロット管理・有効期限は採用しない）
- `sessions.last_seen_at`は作成時に設定し、更新は前回から5分以上経過した時だけ
- `updated_at`は`users` / `profiles` / `login_credentials` / `recovery_credentials`のみ
- rate limit用テーブルは作らない（MVPはインメモリ）
- IPアドレス・セキュリティイベントログ用のテーブルは作らない

### 主要クエリ（すべてparameterized）

| 用途 | 概要 |
|---|---|
| 公開取得 | `SELECT public_id, content FROM profiles WHERE public_id = ? AND status = 'published'` |
| Session認証 | `token_hash`で`sessions`を検索 → 有効性はドメインで判定 |
| touch | `UPDATE sessions SET last_seen_at = ? WHERE id = ? AND last_seen_at <= ?`（閾値＝現在−5分） |
| 全Session失効 | `UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL` |
| ログイン照合 | `login_id_hash`で`login_credentials`を検索（不在でもダミーArgon2検証を実行） |
| 回復用アカウント検索 | `SELECT user_id FROM recovery_credentials WHERE recovery_key_hash = ?` |

---

## 10. Migration運用

### 基本ルール

- `migrations/NNN_name.sql`。番号順・forward-only（down migrationは持たない）。適用は明示コマンド（`make migrate`）のみ。アプリ起動時に自動実行しない
- runnerが管理する`schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)`。適用済みのchecksum不一致は失敗。各migrationは1 transaction
- **初回の本番適用まで**: `001_initial.sql`を直接編集してよい。開発DBはchecksum不一致になるため空DBから作り直す
- **本番適用後**: 適用済みのmigrationは変更しない。変更は新しい番号のmigrationで行う

### runner仕様

- **FK無効化指示**: migrationファイルの先頭コメントブロックに`-- migrate: foreign_keys=off`を書いた場合のみ、runnerはそのmigrationの実行中だけFK検査を無効にする。指示は他に存在せず、未知の指示はエラーにする。checksumは指示行を含むファイル全体で計算する
- `foreign_keys=off`指定時の実行順序: ①`PRAGMA foreign_keys=OFF`（transaction外）→ ②`BEGIN IMMEDIATE` → ③SQL実行 → ④`PRAGMA foreign_key_check`（1行でも返れば`ROLLBACK`して失敗）→ ⑤`schema_migrations`記録 → ⑥`COMMIT` → ⑦`PRAGMA foreign_keys=ON`（成功・失敗を問わず`finally`で必ず復帰）
- SQLの実行は、`sqlite3.complete_statement`で文単位に分割して1文ずつ`execute`する。**`executescript`は使わない**（transaction中に呼ぶと暗黙に`COMMIT`され、1 migration = 1 transactionが崩れる）。migrationファイル内に`BEGIN`/`COMMIT`を書かない
- migration途中で失敗した場合は`ROLLBACK`し、`schema_migrations`にも記録しない

### 変更の分類と方針

| 分類 | 例 | 方針 |
|---|---|---|
| 追加型 | 新テーブル、索引追加、`ADD COLUMN`（NULL許容、またはNOT NULL＋定数DEFAULT） | **優先する**。再作成は不要 |
| 再作成が必要 | CHECK・NOT NULL・型・制約の変更、DEFAULTなしのNOT NULL列追加、列の削除・順序変更 | テンプレートを使う |

- 将来変わりうる上限値・列挙値は、DBのCHECKをドメインより緩くしておく。再作成を避けるための先手
- 再作成したテーブルの**索引・トリガーはDROPで消える**。新テーブル作成後に作り直す

### テーブル再作成テンプレート

```sql
-- migrate: foreign_keys=off     ← 他テーブルから参照されるテーブル（現状はusersのみ）の場合だけ必須
CREATE TABLE x_new ( ...新しい定義... ) STRICT;
INSERT INTO x_new (col1, col2, ...) SELECT col1, col2, ... FROM x;
DROP TABLE x;
ALTER TABLE x_new RENAME TO x;
-- 索引・トリガーがあればここで再作成
```

- **手順は「新テーブル作成 → コピー → 旧テーブルDROP → 新テーブルをRENAME」。旧テーブルを別名へRENAMEする方式は使わない**（子テーブルのFK参照が別名に書き換わる）
- **`users`の再作成は`foreign_keys=off`を必須とする**。`foreign_keys=ON`のまま`DROP TABLE users`すると、暗黙のDELETEでCASCADEが走り、全子テーブルのデータが消える
- データ内容に依存する変更は、適用前にバックアップの複製で件数・値を確認する（`make migrate-check`）

### 適用前バックアップと復旧

- `make migrate`は、未適用のmigrationがある場合、適用前にバックアップを取得する（SQLiteの`.backup` API／`VACUUM INTO`。WAL/SHMの手動コピーは禁止）。`ENV=prod`では必須（取得失敗なら適用しない）、`dev`/`test`では省略可
- **名前と保持**: 名前は`pre-migrate-v<適用前version>-to-v<適用後version>-<YYYYMMDD-HHMMSS>.db`。保持は**新しい順に最大3世代、かつ取得から30日以内**。整理は`make migrate`の**成功直後**に`pre-migrate-*`だけを対象に行い、失敗時は整理しない。日次・週次バックアップには触れない
- バックアップにはハッシュが含まれるため、アクセスを制限する
- **復旧**: アプリを停止 → 現DBと`-wal`/`-shm`を退避 → バックアップをDBパスへ配置 → `make migrate`の再実行は修正済みmigrationで行う。down migrationは使わない
- `make migrate-check SRC=<バックアップ>`: バックアップを一時ディレクトリへ複製して全未適用migrationを適用し、`integrity_check`・`foreign_key_check`・テーブル別行数を表示する。本番DBには触れない
- **日次バックアップ・オフサイト転送・復元は [§18](design/16-deployment.md) を正本とする**。本節の `pre-migrate-*` は migration 適用前の専用バックアップであり、日次・オフサイトとは別物（相互に削除しない）

### 見直しのトリガー

次のいずれかが現実になったら、PostgreSQLとmigrationツール（Alembic/Atlas等）への移行を再検討する。

- worker>1または複数インスタンスが必要になる
- 単一writerが律速になる
- 時点復旧（PITR）や読み取りレプリカが必要になる
- テーブル再作成が常態化する（目安: 再作成型のmigrationが半数を超える）

---

## 11. インフラ・運用

### 技術と依存

Python 3.14 / FastAPI / SQLite（WAL、標準`sqlite3`）/ SQL直書き（ORMなし）/ 手動migration / pytest / pip / Ruff。テンプレートエンジンは使わない。

| 区分 | 承認済みの依存（これ以外は事前承認） |
|---|---|
| runtime | `fastapi`, `uvicorn`, `argon2-cffi` |
| dev | `pytest`, `httpx2`（TestClient用）, `ruff`, `pyright` |
| e2e | `pytest-playwright`, `playwright`（＋推移依存。`requirements-e2e.txt` に分離） |

- バージョンは`requirements.txt` / `requirements-dev.txt` / `requirements-e2e.txt`で固定する
- **環境確認**: Python 3.14と上記依存のwheelが導入できること、`sqlite3.sqlite_version`が3.37以上であることを確認する。3.14で導入できない場合は、Python 3.13へ下げず承認を求める（UUIDv7が標準に無くなるため）
- **型検査**: `pyright`（`[nodejs]` extra）はtestイメージのビルド時に本体を取得し、実行時のネットワークを不要にする。`pyproject.toml` の `[tool.pyright]`（`standard`）に従う
- **E2E**: `make e2e` で Playwright＋コンテナ内 Chromium を実行する。専用 compose プロジェクト（`compose.yaml` ＋ `compose.e2e.yaml`、自己完結）＋`ENV=test`＋`deploy/e2e/e2e.env` を使い、毎回空DBから起動する。`make check` には含めない（依存は `requirements-e2e.txt` に分離し、runtime / test イメージには入れない）
- **E2E目視確認モード（任意のデバッグ用）**: `make e2e-headed` は `e2e` サービスを `compose.e2e-headed.yaml` で上書きし、Xvfb / x11vnc / noVNC（`e2e-headed` イメージは `FROM e2e` に apt を追加しただけの差分）でコンテナ内 Chromium の画面を `127.0.0.1` 限定公開の `http://127.0.0.1:6080/vnc.html` から目視する。`page.pause()` / `PWDEBUG=1` で停止し、noVNC 越しに手動操作できる。ブラウザはコンテナ内のままで、`make e2e` / `make check` / DoD には影響させない

### 設定（環境変数、起動時に検証して不正なら起動しない）

| 変数 | 内容 |
|---|---|
| `ENV` | `dev` / `test` / `prod` |
| `DATABASE_PATH` | SQLiteファイルのパス（Dockerはnamed volume上） |
| `APP_PEPPER` | 32バイト以上のbase64url |
| `PUBLIC_BASE_URL` | `https://`のオリジン（CSRFのOrigin検証に使用） |
| `TRUSTED_PROXY_COUNT` | 信頼するプロキシ段数（既定0） |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_KIB` / `ARGON2_PARALLELISM` / `ARGON2_MAX_CONCURRENCY` | 既定 3 / 65536 / 4 / 2 |
| `RATE_LIMIT_MAX_KEYS` | インメモリ rate limit の key 上限（既定 10,000）。容量 fail-closed のしきい値（[§8](design/07-security.md)。1以上） |

> **公開版の記載方針**: 以下の Argon2 / rate limit の値は公開版の既定値の例。本番値は環境ごとに tune し、非公開の運用手順で管理する。

- `ENV=test`のときだけ、テスト用の軽量Argon2パラメータとダミーpepperをfixtureで指定してよい。`.env`はcommitせず`.env.example`のみ置く
- テストは`https://testserver`のbase URLを使い、`Secure` cookieを外す設定は作らない

### 実行モデルとDB

- エンドポイントは`def`（threadpool）。標準`sqlite3`とArgon2はブロッキングのため`async def`内で直接呼ばない。uvicornのworkerは1
- **threadpool上限は8**（`anyio.to_thread.current_default_thread_limiter().total_tokens = 8`）。40の既定値に任せず、待機スレッド増殖を防ぐ
- **Argon2の実行枠（`ARGON2_MAX_CONCURRENCY`）は待たせない**。枠が埋まれば認証処理へthreadpoolを投入せず、429または503を返す。**Argon2 呼び出し（`hash` / `verify` / `dummy_verify`）は transaction（`BEGIN IMMEDIATE`）の外で行い、書き込みロックを保持しない**（[§4](design/04-use-cases.md) §5）
- 接続はリクエスト単位。接続ごとに`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`。`isolation_level=None`で自前に制御し、書き込みは`BEGIN IMMEDIATE`。busy timeoutはH-17
- 期限切れレコードは読取り時に書き換えない。期限切れデータの掃除はMVPでは行わない
- 時刻の生成は`Clock`アダプターのみ
- **graceful shutdown**: コンテナは新規受付停止 → 実行中リクエスト完了待ち → プロセス終了。バックアップは稼働中でもSQLiteの`.backup`を使用し、WAL/SHMをコピーしない

### Docker

- SQLiteは**named volume**に置く（bind mountでのWALは不安定）。非rootユーザー、`TZ=UTC`、TLSはリバースプロキシで終端
- リバースプロキシはCaddy。アクセスログは出さない。HSTSはリバースプロキシで付与
- アプリコンテナは`read_only: true`、`cap_drop: [ALL]`、`no-new-privileges:true`

### 運用

**公開停止（不適切投稿対応の第一次対応）**:
```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
UPDATE profiles SET status = 'draft', published_at = NULL, updated_at = :now
 WHERE public_id = :public_id AND status = 'published';
-- 影響行数が1であることを確認してからCOMMIT
COMMIT;
```

**削除依頼の対応（直接DB操作。最終手段）**:
```sql
PRAGMA foreign_keys = ON;   -- 必須
BEGIN IMMEDIATE;
DELETE FROM users
 WHERE id = (SELECT user_id FROM profiles WHERE public_id = :public_id);
-- 影響行数が1であることを確認してからCOMMIT
COMMIT;
```
対応前にバックアップを取り、対応日時・依頼元・対象`public_id`を運用記録に残す（本文・秘密値は記録しない）。

**バックアップ**:
- `sqlite3 <db> ".backup '<dest>'"`または`VACUUM INTO`。WAL/SHMの手動コピーは禁止
- migration適用前の自動バックアップ（`pre-migrate-*`）の保持は最大3世代・30日以内
- バックアップにはハッシュが含まれるため保管先のアクセスを制限する。`APP_PEPPER`はDBバックアップと**別経路**で保管する
- **日次バックアップ・オフサイト転送・監視・Argon2 tune・リリース/デプロイは [§18](design/16-deployment.md) を正本とする**（本節の `pre-migrate-*` とは別物）

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
- `manual`（`make e2e-headed`）: UI主体マイルストーンの目視確認。任意のデバッグではなく**記録必須のゲート**（[完了定義](design/14-dod-conventions.md) §15）。`page.pause` / `PWDEBUG` を使う。

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

---

## 13. UX要件（秘密値の保存確認）

### 作成完了画面

- `login_id`・初期パスワード・`recovery_key`を一度だけ表示する
- 「これらを失うと復旧できない」ことを明示し、「保存した」の確認チェックを必須にする。確認前は次の画面へ進めない
- `recovery_key`の再発行手段は「パスワード変更」または「回復」自体。有効期限は設けない
- 作成をやり直すと別のプロフィールになる（秘密値の再表示はしない）

### パスワード変更・回復後の保存確認画面

- パスワード変更と回復の成功時も、**新しい`recovery_key`を表示し、保存確認を必須にする**
- パスワード変更では`recovery_key`のみ表示。回復では`login_id`と`recovery_key`を表示
- 「保存した」の確認チェック前は次の画面へ進めない
- 旧`recovery_key`は失効済みであることを明示する

### 表示識別文字とコピー

- 表示形式: `login_id`＝`Base58(24)`＋（任意）`_<表示識別文字>`、`recovery_key`＝`Base58(32)_RECOVERY-KEY`＋（任意）`-<表示識別文字>`、初期パスワード＝`Base58(16)`＋横に表示識別文字をラベル表示
- 「管理識別用文字（任意）」を入力すると、`_`以降の文字列が`login_id`と`recovery_key`に即時反映される。パスワードは横のラベルに反映される（コピー時にメモ書きとして合わせてコピーされる）
- 表示識別文字は最大64文字、制御文字・改行不可。`_`は許容。クライアント側のみで保持され、サーバーには送信されない（実名等を含めてもサーバーには残らない）
- 「ログインID/パスワード/リカバリーキーをコピー」で3点をまとめてコピーできる
- 確認画面にもコピーボタンと「全体保存」（メモ帳にそのまま貼る用）を置き、「保存した」のチェックを押させる（確認前は遷移不可）

---

### 分割のための派生メモ（新たな規定は含まない）

- 画面一覧と Cache 属性は [05-api-contract.md](design/05-api-contract.md) の「Web画面」。本章は振る舞いのみ。
- `GET /credentials-saved` は作成・パスワード変更・回復の3経路で共用する。経路を表す値も渡さない（[07-security.md](design/07-security.md) 不変規則3「URLに認証情報を載せない」）。
- 「確認チェック前は遷移不可」は UI 側の制約。サーバー側で同じ制約を強制する API は作らない（[01-overview.md](design/01-overview.md) §2 MVP範囲）。
- AC-6・AC-7 が本章の受け入れ基準。

---

## 14. 設計決定

| ID | 決定 | 根拠・備考 |
|---|---|---|
| D-1 | 回復成功時に`login_id`・パスワード・`recovery_key`を再発行し、Sessionは発行しない | 高エントロピーな単一の回復鍵でアカウントを特定する。詳細は§5・§8 |
| D-2 | CSRFはdouble-submit cookie、Origin検証、JSON限定を併用する | URLに認証情報を載せない方針と整合。詳細は§8 |
| D-3 | Cookieに`__Host-`を使い、`Secure`を無効化する設定を設けない | Cookie属性の制約をブラウザに強制させる。詳細は§8 |
| D-4 | `GET /me/profile`を設け、PATCHは`content`または`status`を1項目以上受け付ける | 編集画面の初期表示と部分更新に必要。詳細は§6 |
| D-5 | エラー表はH-1〜H-18とする | 到達不能な旧エラーは現行契約に含めない。詳細は§7 |
| D-6 | Python 3.14と承認済み依存のみを採用する | 標準のUUIDv7を利用する。依存一覧は§11 |
| D-7 | 新パスワードはNFKC正規化後12〜128文字とし、構成ルールを設けない | 現パスワードとの同一も禁止。詳細は§4・§8 |
| D-8 | プロフィール本文は単一の`content`とし、ドメイン上限を6,000文字、DB上限を100,000文字とする | 入力補助はフロントエンドに委ね、DB制約は将来変更に備えて緩くする。詳細は§2・§9 |
| D-9 | 全画面に`noindex`を適用する | 匿名プロフィールの検索エンジン掲載を避ける。詳細は§6・§8 |
| D-10 | Rate limitは暫定値・プロセス内メモリ・単一workerで運用する | 秘密値そのものをキーにせず、HMAC等で相関する。詳細は§8・§11 |
| D-11 | `APP_PEPPER`は環境変数で管理し、MVPではローテーションしない | 紛失・変更時は既存の資格情報とSessionを照合できなくなる。詳細は§8・§11 |
| D-12 | ログアウトは冪等とし、無効なSessionでも成功応答を返す | クライアント側の再試行を単純化する。詳細は§6・§7 |
| D-13 | 不適切投稿は運用手順で対応し、公開停止を第一次対応、削除を最終手段とする | 通報API・管理画面・管理者ロールはMVPに含めない。詳細は§2・§11 |
| D-14 | MVPのログイン方式は`login_id`＋パスワードとする | パスキーは後続段階で検討する。詳細は§2・§6 |
| D-15 | 認証と認可は論理的に分離し、MVPでは認証のみ実装する | 物理的な分離は将来課題。詳細は§3 |
| D-16 | Agent向け準備は、`Principal(user_id, method, scopes)`の共通抽象（§3）、資源指向のAPI名（§6）、Cookie専用操作の明記（§6・§8）、秘密値（パスワード・`recovery_key`）をAgentに渡さない不変規則（§8）に限定する | OAuth・MCP・ETag・外部IdP等はMVP外（§2）。スコープ名・URL名前空間の予約は `plan/proposals/future-extensions.md`。詳細は各§ |
| D-17 | 外部IdP連携を採用しない | 匿名性を優先する。詳細は§2 |
| D-18 | 本文正規化、入力形式、画面、CSP、Rate limit、Argon2同時実行上限を明示する | 実装者・Agentの推測余地を減らす。詳細は§2・§6・§8・§11 |
| D-19 | 画面は静的HTMLと外部JavaScriptで実装し、テンプレートエンジンを使わない | MVPの実装範囲を抑える。詳細は§2・§6・§11 |
| D-20 | 回復鍵は常に1件だけ有効とし、作成・変更・回復時にローテーションする | 複数コードや有効期限を設けず、通知手段がないことによる復旧不能化を避ける。詳細は§4・§5・§9 |
| D-21 | セキュリティイベントは許可リスト方式で記録し、生IPは保存せず日次HMACを30日保持する | 不正対策に必要な範囲に記録を限定する。詳細は§8・§17 |
| D-22 | `public_id`には`p_`を付け、`login_id`・`recovery_key`には種別接頭辞を付けない | `l`の視認性問題を避ける。詳細は§4・§8。旧方式はD-30で変更 |
| D-23 | 回復資格情報の不正はHTTP 400で返す | 無効な回復入力を示し、HTTP認証チャレンジを意味する401を避ける。詳細は§7 |
| D-24 | worker数は1に制限する | プロセス内Rate limitがworker間で分断されるため。共有ストア導入時に再検討。詳細は§8・§11 |
| D-25 | パスワードはNFKC、本文はNFCで正規化する | 入力用途に応じて正規化方式を分ける。詳細は§4・§8 |
| D-26 | `POST /profiles`の再試行による重複作成を許容する | MVPでは冪等性キーを導入せず、必要時に検討する。詳細は§6 |
| D-27 | SQLite（WAL）、SQL直書き、手書きmigration、自作runnerを維持する | 小規模な単一インスタンスMVPでは移行コストを抑える。詳細は§9・§10 |
| D-28 | Pyrightの`standard`を型検査に使い、`make check`のゲートに含める | 型の取り違えを機械的に検出し、実行時ネットワーク依存を避ける。詳細は§11・§15 |
| D-29 | Rate limitのkey上限10,000到達時は新規keyを拒否し、既存keyの判定を継続する | fail-closedとし、上限到達を運用ログで観測する。詳細は§8・§17 |
| D-30 | `login_id`・`recovery_key`はBase58固定長部分だけを認証対象とし、表示ラベルはクライアント側だけで保持する | HMAC・Rate limit計算前に固定長部分を抽出する。詳細は§1・§4・§6・§8 |
| D-31 | Playwright＋コンテナ内ChromiumのE2Eを`make e2e`で別実行し、`make check`から分離する | 専用Compose・`ENV=test`・専用env-file・空DBで再現性を確保する。詳細は§11・§12・§15 |
| D-32 | `make e2e-headed`で任意の目視確認環境を用意し、noVNCをlocalhostのみに公開する | ブラウザをコンテナ内に保ち、通常の検査やDoDに影響させない。詳細は§11・§12・§15 |
| D-33 | リリースは手動実行を契機にruntime ImageをGHCRへpushし、配備はtagではなくdigestで行う | stagingで検証したdigestをそのまま本番へ出す。詳細は§18 |
| D-34 | 本番ログはDockerのjournaldドライバで回収し、ホストjournaldを30日保持する | 保持期間を生IP相関の30日と一致させる。詳細は§17・§18 |
| D-35 | 日次バックアップは`.backup`→整合性検査→gzip→age暗号化→オフサイト転送とし、復元は手順に従う | 平文をホストに残さず、age秘密鍵をVPSに置かない。詳細は§10・§18 |
| D-36 | stagingとproductionはいずれも`ENV=prod`で動かす | `ENV`は安全性の緩和スイッチであり配備階層ではない。詳細は§11・§18 |
| D-37 | Argon2（`hash`/`verify`/`dummy_verify`）は DB transaction の外で実行し、transaction は保存操作のみを囲む。変更系（パスワード変更）は検証〜更新間の変化を transaction 内で検知して拒否する | 書き込みロックを CPU 処理中に保持せず、実行枠の非ブロッキング 503 を機能させる。詳細は§4・§5・§8・§11 |

---

## 15. 完了定義（Definition of Done）

- `make check`（`ruff check` / `ruff format --check` / `pyright` / `pytest`）が全て成功する
- `make e2e`（Playwright＋コンテナ内Chromium）が成功する（M13では全シナリオ通過を含む）
- 空DBから`make migrate`が成功し、再実行しても壊れない。migrationは番号順・forward-only
- H表全件に対応するテストがある。受け入れ基準AC-1〜14は、**一次検証層**（[テスト](design/11-testing.md) §12）に AC を直接観測するテストがある
- 漏えいテスト（ログ・DBダンプ・エラー応答に秘密値が無い）とアーキテクチャテストが通る
- **UI主体マイルストーン（画面追加を含む M）の完了時は目視確認を必須とする**: `make e2e-headed` で主要動線を目視し、結果を `plan/STATUS.md` §7 に記録する。これは機械テストの代替ではなく、レイアウト・文言・操作感など機械で拾えない退行の確認である
- 挙動が設計書とずれる場合、コードで補正せず承認を求める

---

## 16. 変更規約

**許可**: 命名、private helper、関数分割、仕様を満たすテスト追加、振る舞いを変えないリファクタリング。

**事前承認が必要**: Entity / Context / Aggregate / Portの追加・変更、API追加・変更、DB schema / migration、MVP範囲、Security policy、依存ライブラリとPythonバージョンの変更、秘密値や公開DTOの変更、設計書そのもの。

**不変の安全規則**:
- `user.id`、`profile.id`、所有者情報はURL・HTML・公開API・ログに出さない。公開識別子は`public_id`だけ
- 生password、login_id、session token、recovery code、CSRFトークン、`APP_PEPPER`を、DB・ログ・例外・分析・Referer・URLへ出さない
- URL（path・query・fragment）に認証情報を載せない。ログインIDだけでは認証しない
- ORMを導入しない。SQLはparameterized query。書き込みは`BEGIN IMMEDIATE`のtransaction
- 入力値をエラー応答にechoしない
- MVP外を実装しない
- `.env`・pepper・本番の秘密値を読まない・生成しない・commitしない。テストは明白なダミー値だけを使う

**実装規約**:
- レイヤ依存: `domain`は標準ライブラリのみimport可。`profile`と`identity`は相互にimportしない。Context跨ぎは`src/app/`と`src/main.py`のみ。自作のアーキテクチャテストで検証する
- 時刻・乱数・IDは`Clock` / `SecretGenerator` / `IdGenerator`ポート経由。`datetime.now()`等の直接呼び出しとnaiveな時刻は禁止
- エンドポイントは`def`（threadpool）。`async def`内でDB・Argon2を直接呼ばない
- テストの削除・弱体化・`skip`・`xfail`・期待値の書き換えで通さない。設計とずれるなら承認を求める
- 型検査（Pyright）のエラーを `# type: ignore` / `# pyright: ignore` / 設定緩和で黙らせない。やむを得ない場合は理由を添えて `plan/STATUS.md` に記録し、人間の承認を得る
- コミットは main 直（マイルストーンブランチは切らない）。テストを先に書く。コミットメッセージに対応する仕様章を書く

---

## 17. ログ

**目的（優先順）**: 1) 障害の原因究明（どの経路で何が起き、なぜ失敗したか）、2) セキュリティ事象の検知・追跡（同日の相関まで）、3) 容量・劣化の観測（レートリミッタの上限到達など）。

**前提**: 単一プロセス（`worker = 1`、`design/13-decisions.md` D-24）。レート制限はプロセス内メモリ、DBは SQLite。匿名プロフィールを扱うためプライバシー優先。運用は小規模で、外部へは送らない。明示的に**やらない**: ユーザー単位の長期的行動追跡、分析SaaS連携、ログの外部保管。

### 原則（不変規則）

1. 秘密値（password / `login_id` / session token / `recovery_key` / CSRF / `APP_PEPPER`）を出さない。
2. `user.id` / `profile.id` / 所有者情報を出さない。**生IPを出さない**（`ip_daily` のみ）。
3. request 本文・ヘッダー・Cookie・クエリ・ローカル変数を出さない。
4. 1行1レコードの構造化ログ（JSON）。項目は**許可リスト制**（allowlist）。
5. 相関は **`request_id`**（必須）。IP相関は **`ip_daily`**（日次回転・保持30日）。
6. ログの種類・イベント・項目の追加/変更は**設計変更**（Security policy、`design/14-dod-conventions.md` §16）。
7. 外部送信・行動追跡をしない。保持は日次集計と許容期間に限る。

### チャネル（ロガー）

`logger` を目的別に分ける。各行は「どのチャネルの許可リストに属するか」が一意になる。

| ロガー | 目的 | レベル | 1行の項目（許可リスト） | 出力先 | 保持 |
|---|---|---|---|---|---|
| `app.access` | リクエスト結果（運用） | INFO | `request_id` `route`(template) `method` `status` `duration_ms` `error_code` | stderr | プラットフォーム既定 |
| `app.security` | セキュリティイベント（8種） | INFO | `timestamp` `event` `request_id` `ip_daily`（`rate_limited` のとき `kind`） | stderr | 30日 |
| `app.error` | 想定外例外 | ERROR | `request_id` `exception`(クラス名) | stderr | プラットフォーム既定 |
| `app.limit` | レート制限の集計（容量） | INFO | `event` `count` | stderr | プラットフォーム既定 |
| `app.lifecycle`（任意） | 起動/停止/migration の要約 | INFO | `event` `detail`(安全な値のみ) | stderr | プラットフォーム既定 |
| `uvicorn.access` | 無効 | — | — | — | — |
| Caddy（reverse-proxy） | アクセスログ無効 | — | — | — | — |

- **実装必須**のチャネルは `app.access` / `app.security` / `app.error` / `app.limit` の4つ（[完了定義](design/14-dod-conventions.md) §15 の DoD 判定はこの4つを対象とする）。`app.lifecycle` は**将来利用の設計枠**で、実装してもよいが必須ではない（設計からは外さない）。
- セキュリティイベント8種: `login_failed` / `login_succeeded` / `recovery_failed` / `recovery_succeeded` / `password_changed` / `profile_created` / `rate_limited` / `csrf_failed`。
- `rate_limited` の `kind` は閉じた集合の enum（`login_account` / `login_ip` / `recovery_minute` / `recovery_hour` / `password_change` / `profile_create`）。同時に複数の制限を超過した場合は、超過した各制限につき1件記録する。
- `app.limit` の `event` は `rate_limit_cap_reached`（上限到達の集計。`keys_evicted` は後回し）。
- `app.lifecycle` は**将来利用の設計枠**。実装してもよいが必須ではない（起動・migration の要約に使える枠。DoD 判定には含めない）。

### 形式と相関

- **形式**: 1行 JSON。`ensure_ascii=False`・区切りは詰める。キーは安定（機械可読）。
- **時刻**: セキュリティイベントは `timestamp`（UTC ISO 8601、27文字固定）。`app.access` は設計上「時刻」項目を持たず、行の時刻は**コンテナログのタイムスタンプ**に依存する。
- **相関**:
  - `request_id`: `app.access` / `app.security` / `app.error` を結合する鍵。1リクエスト内で不変。
  - `ip_daily`: `HMAC-SHA256(key=APP_PEPPER, msg="ip_daily:" + YYYY-MM-DD(UTC) + ":" + ip)`。同日の相関のみ可能。日をまたぐ追跡はできない（保持30日）。
- **レベル**: INFO（access/security/limit）、ERROR（error）。DEBUG は本番 off。

### 何を出すか／出さないか

- 出す: 上のチャネル表の許可リストの項目のみ。
- 出さない（deny）: 生IP、秘密値、`user.id`/`profile.id`、本文、ヘッダー、Cookie、クエリ、例外メッセージ（例外クラス名のみ）。
- 担保: `tests/security`（ログ走査・許可リスト検査・AC-1 横断走査・AC-8 DB非保存）＋ `tests/api/test_access_log.py` / `tests/security/test_security_event_log.py`。

### 出力先・保持・ローテーション

- 出力: `logging.StreamHandler()`（既定 `sys.stderr`）。ファイル出力・自前ローテーションはしない。Docker が回収し、ローテーションは Docker のログドライバに委ねる。
- 保持: セキュリティイベントは設計上30日（日次の相関のみ可能）。運用ログはプラットフォーム既定。
- 本番の回収・保持: Docker のログドライバは **journald** とし、ホスト journald の保持期間は運用ポリシーで設定する。tag は `<LOG_TAG_APP>` / `<LOG_TAG_PROXY>`（[§18](design/16-deployment.md)）。
- 方針: 高カーディナリティ（`public_id` 等の生値）・高頻度のログを避ける。集計値で代替できるものは集計にする。

### 運用・監視・保守

**目的別の見方**

| 知りたいこと | 見るチャネル | 手がかり |
|---|---|---|
| どの経路で何が起きた | `app.access` | `route` / `method` / `status` / `error_code` |
| 500 の正体 | `app.error` | `request_id` で結合 → `exception` |
| 認証・回復・作成の成否 | `app.security` | `event` / `ip_daily` |
| 429 の理由（個別制限） | `app.security` | `event=rate_limited` の `kind` |
| 429 の理由（容量/fail-closed） | `app.limit` | `event=rate_limit_cap_reached` |
| 特定リクエストの追跡 | 全チャネル | `request_id` |

**調査手順（playbook）**

1. `app.access` から異常（status 5xx、error_code、duration 異常）を行で特定 → `request_id` を得る。
2. 同じ `request_id` で `app.security` / `app.error` を引く → 事象と例外種別を確認。
3. セキュリティ系なら `ip_daily` で同日の関連イベント（例: `login_failed` の連続 → `login_succeeded`）を追う。
4. 429 が多発していれば `kind` 別に集計。個別制限に当てはまらない大量拒否は `app.limit` の `cap_reached` を確認。
5. 5xx が続く場合は `app.error` の例外クラスの推移を見る（DB/設定/コードの切り分け）。

**監視指標**: 5xx 率、`rate_limited` の `kind` 別件数、未処理例外件数、`cap_reached` 件数。MVP ではログから目視/簡易集計し、メトリクスは別途運用設計で扱う。運用手順の詳細は `docs/logging.md` を参照。

### プライバシー・セキュリティ

- 不変規則の再掲（本節「原則」）。特に **生IPを残さない**（`ip_daily` の日次回転）。
- 漏えい検査を維持する（`tests/security`）。例外ログは `request_id` と例外クラス名のみ。
- 30日の保持・日次HMACが、ユーザー単位の長期的追跡を構造的に不可能にする。

### 変更規約

- イベント種別・項目の追加/変更は D-series（`design/14-dod-conventions.md` §16 の Security policy）。allowlist 定数とテストを同時に更新する。
- ログ形式（JSON 化・キー名）の変更は後方互換に注意（運用クエリに影響）。

### 将来拡張

- `keys_evicted` の集計、メトリクス、外部送信・分析SaaS、分散トレーシング、詳細デバッグレベル、構造化エラーの拡充。
- 拡張は原因究明に必要なものが出た後にまとめて検討する。

### 対象外

- 外部送信・分析ツール連携、保持期間の変更（30日のまま）、`ip_daily` の式変更、DBスキーマ変更、ユーザー単位の長期追跡、ログの完全な機械可読メトリクス化。

---

## 18. デプロイ・リリース・運用基盤

**目的**: アプリを staging / production へ安全にビルド・配備し、公開後に監視・保全（バックアップ/復元）・チューニングする基盤を定める。[§11](design/10-infra-ops.md)（実行モデル・環境変数）の上に「**どうビルドし、どう配り、どう保全するか**」を積む章であり、アプリ本体（`src/`）の仕様は変えない。

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
