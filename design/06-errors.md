# 設計 06: エラー処理

> 関連: [API契約](05-api-contract.md) / [セキュリティ](07-security.md) / [テスト](11-testing.md) / [インフラ・運用](10-infra-ops.md)

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