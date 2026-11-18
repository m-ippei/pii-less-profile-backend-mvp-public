# 設計 15: ログ

> 関連: [セキュリティ](07-security.md) / [インフラ・運用](10-infra-ops.md) / [テスト](11-testing.md) / [完了定義](14-dod-conventions.md)

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

- **実装必須**のチャネルは `app.access` / `app.security` / `app.error` / `app.limit` の4つ（[完了定義](14-dod-conventions.md) §15 の DoD 判定はこの4つを対象とする）。`app.lifecycle` は**将来利用の設計枠**で、実装してもよいが必須ではない（設計からは外さない）。
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
- 本番の回収・保持: Docker のログドライバは **journald** とし、ホスト journald の保持期間は運用ポリシーで設定する。tag は `<LOG_TAG_APP>` / `<LOG_TAG_PROXY>`（[§18](16-deployment.md)）。
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
