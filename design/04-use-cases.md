# 設計 04: ユースケースとトランザクション

> 関連: [概要](01-overview.md) / [アーキテクチャ](02-architecture.md) / [ドメインモデル](03-domain-model.md) / [API契約](05-api-contract.md) / [テスト](11-testing.md)

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