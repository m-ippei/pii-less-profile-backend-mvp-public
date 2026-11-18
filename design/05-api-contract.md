# 設計 05: API契約

> 関連: [概要](01-overview.md) / [ユースケース](04-use-cases.md) / [エラー処理](06-errors.md) / [セキュリティ](07-security.md) / [UX要件](12-ux-secrets.md) / [テスト](11-testing.md)

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
- JavaScriptは`/static/`の外部ファイルのみ（インラインスクリプト禁止）。本文の表示は必ず`textContent`を使い、`innerHTML`等の禁止APIは使用しない。**この担保は「静的検査での禁止APIの不在」（`architecture` 層の不在検証）と「E2E での `<script>` 実行不成立」（`e2e` 層・一次証拠）の2段で示す**（[テスト](11-testing.md) §12 の一次検証層）
- GET応答で`__Host-csrf` cookieが無ければミドルウェアが発行する
- `PATCH /me/profile`の同時実行は**last-write-wins**。更新は1 transactionで直列化