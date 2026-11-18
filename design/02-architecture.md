# 設計 02: アーキテクチャ

> 関連: [概要](01-overview.md) / [ドメインモデル](03-domain-model.md) / [ユースケース](04-use-cases.md) / [テスト](11-testing.md) / [変更規約](14-dod-conventions.md)

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