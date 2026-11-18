# ログ運用ガイド

> 用途: 本サービスのログを**どう見て・どう使うか**（目的別の見方・調査手順）の実務ガイド。
> **正本は `design/15-logging.md` §17（ログ設計）**。本書は運用手順のみを持ち、チャネル定義・許可項目・保持期間は設計章を重複させない。ログの種類・項目の追加/変更は設計変更（`design/14-dod-conventions.md` §16 の Security policy）。

---

## 1. チャネル一覧（要点）

| ロガー | 目的 | 主な項目 |
|---|---|---|
| `app.access` | リクエスト結果（運用） | `request_id` `route` `method` `status` `duration_ms` `error_code` |
| `app.security` | セキュリティイベント（8種） | `timestamp` `event` `request_id` `ip_daily`（`rate_limited` は `kind`） |
| `app.error` | 想定外例外 | `request_id` `exception`（クラス名） |
| `app.limit` | レート制限の上限到達（集計） | `event`（=`rate_limit_cap_reached`） `count` |
| `app.lifecycle`（任意） | 起動/停止/migration の要約 | `event` `detail`（安全な値のみ） |

- 出力はすべて stderr（`logging.StreamHandler`）。ファイル出力・自前ローテーションはしない。Docker が回収し、ローテーションは Docker のログドライバに委ねる。
- `uvicorn.access` と Caddy のアクセスログは**無効**。

## 2. 目的別の見方

| 知りたいこと | 見るチャネル | 手がかり |
|---|---|---|
| どの経路で何が起きた | `app.access` | `route` / `method` / `status` / `error_code` |
| 500 の正体 | `app.error` | `request_id` で結合 → `exception` |
| 認証・回復・作成の成否 | `app.security` | `event` / `ip_daily` |
| 429 の理由（個別制限） | `app.security` | `event=rate_limited` の `kind` |
| 429 の理由（容量/fail-closed） | `app.limit` | `event=rate_limit_cap_reached` |
| 特定リクエストの追跡 | 全チャネル | `request_id` |

## 3. 調査手順（playbook）

1. `app.access` から異常（status 5xx、`error_code`、duration 異常）を行で特定し、`request_id` を得る。
2. 同じ `request_id` で `app.security` / `app.error` を引く → 事象と例外種別を確認。
3. セキュリティ系なら `ip_daily` で**同日**の関連イベント（例: `login_failed` の連続 → `login_succeeded`）を追う。`ip_daily` は日次回転のため日をまたいだ追跡はできない。
4. 429 が多発していれば `app.security` の `kind` 別に集計。個別制限に当てはまらない大量拒否は `app.limit` の `cap_reached` を確認。
5. 5xx が続く場合は `app.error` の例外クラスの推移を見る（DB / 設定 / コードの切り分け）。

### 429 の切り分け（`kind`）

`rate_limited` の `kind` は閉じた集合: `login_account` / `login_ip` / `recovery_minute` / `recovery_hour` / `password_change` / `profile_create`。同時に複数の制限を超過した場合は、超過した各制限につき1件記録される。`kind` に当てはまらない大量拒否は、key 数上限（10,000）到達による fail-closed の可能性があり、`app.limit` の `rate_limit_cap_reached`（`count`）で観測する。

### 代表的な調査コマンド例

`request_id` で絞る（`app.access` から得た値を使う）:

```sh
make logs | jq 'select(.request_id == "<request_id>")'
```

`kind` 別に 429 を数える:

```sh
make logs | jq 'select(.event == "rate_limited") | .kind' | sort | uniq -c
```

## 4. 監視指標

5xx 率、`rate_limited` の `kind` 別件数、未処理例外件数、`rate_limit_cap_reached` 件数。MVP ではログからの目視・簡易集計に留め、メトリクスは別途運用設計で扱う。外部送信・分析SaaS・分散トレーシングはプライバシー方針により行わない（`design/15-logging.md` §17）。

## 5. 注意（プライバシー・保持）

- ログには秘密値・生IP・`user.id`/`profile.id`・本文・ヘッダー・Cookie・クエリ・例外メッセージを**出さない**。IP 相関は `ip_daily`（日次回転）のみ。
- セキュリティイベントの保持は**30日**（日次の相関のみ可能）。運用ログの保持はプラットフォーム既定。
- 運用の記録（対応ログ）にも本文・秘密値を書かない（`design/10-infra-ops.md` §11）。
