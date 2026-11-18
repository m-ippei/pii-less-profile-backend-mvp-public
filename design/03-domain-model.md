# 設計 03: ドメインモデル

> 関連: [概要](01-overview.md) / [アーキテクチャ](02-architecture.md) / [ユースケース](04-use-cases.md) / [データベース](08-database.md) / [セキュリティ](07-security.md)

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