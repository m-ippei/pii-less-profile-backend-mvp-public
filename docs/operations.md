# 運用手順書

> 用途: 本サービスの**運用時の手順**（公開停止・削除依頼・バックアップ・復旧）を持つ。
> **正本は `design/10-infra-ops.md` §11（運用）と `design/09-migrations.md` §10（migration 運用）**。本書は手順のみを持ち、SQL・保持期間は設計章と一致させる。
> 削除API・通報API・管理画面は **MVP外**（`design/01-overview.md` §2）。対応は本手順の直接DB操作のみ。
> 公開フローだけでの設計適合確認（レート制限・飽和・ログ相関等）は、公開版には収録していません。

---

## 0. 前提

- DB は SQLite。`make db-shell`（app コンテナで `sqlite3`、`foreign_keys=ON`）または app コンテナ内の `scripts/` から操作する。
- **`PRAGMA foreign_keys = ON` を必ず明示する。** OFF のまま子テーブルを操作すると、削除・更新の連鎖（`ON DELETE CASCADE`）が働かず不整合を作る（`design/09-migrations.md` §10「`users` の再作成は `foreign_keys=off` が必須」の逆で、通常操作は ON が前提）。
- 変更は `BEGIN IMMEDIATE` で行い、**影響行数を確認してから `COMMIT`** する。
- 運用の記録には **本文・秘密値（`login_id` / パスワード / `recovery_key` / session token / CSRF / `APP_PEPPER`）を書かない**（`design/07-security.md` §8 不変規則）。対象は `public_id` のような公開値までにする。

---

## 1. 公開停止（不適切投稿の第一次対応）

`profiles.status` を `draft` に戻し、公開ページを即時に非公開にする（`published_at` を `NULL`）。

```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
UPDATE profiles SET status = 'draft', published_at = NULL, updated_at = :now
 WHERE public_id = :public_id AND status = 'published';
-- 影響行数が1であることを確認してからCOMMIT
COMMIT;
```

- `:now` は UTC の 27 文字 ISO 8601（`format_timestamp` と同形式）。`:public_id` は対象の公開URL識別子。
- 影響行数が 0（既に非公開・不存在）なら `ROLLBACK` し、対象を確認する。
- 公開ページは `no-store` のため、次の閲覧から非公開が反映される（`design/07-security.md` §8 Cache-Control）。

## 2. 削除依頼の対応（直接DB操作・最終手段）

`profiles` から `user_id` を引き、`users` を削除する。子テーブル（`login_credentials` / `recovery_credentials` / `sessions` / `profiles`）は `ON DELETE CASCADE` で連鎖削除され、資格情報も消える。

```sql
PRAGMA foreign_keys = ON;   -- 必須
BEGIN IMMEDIATE;
DELETE FROM users
 WHERE id = (SELECT user_id FROM profiles WHERE public_id = :public_id);
-- 影響行数が1であることを確認してからCOMMIT
COMMIT;
```

- 対応前に**バックアップ**（§3）を取得する。
- 影響行数が 1 であることを確認してから `COMMIT` する。0 なら対象不存在として `ROLLBACK`。
- 運用記録に残すのは **対応日時・依頼元・対象 `public_id`** まで。**本文・秘密値は記録しない**（`design/10-infra-ops.md` §11「削除依頼の対応」）。

## 3. バックアップ

- 稼働中でも SQLite の **`.backup` API** または `VACUUM INTO` を使う。**WAL/SHM を手でコピーしない**（`design/10-infra-ops.md` §11）。
  ```sh
  sqlite3 /data/app.db ".backup '/data/backups/daily-<YYYYMMDD>.db'"
  # または
  sqlite3 /data/app.db "VACUUM INTO '/data/backups/daily-<YYYYMMDD>.db'"
  ```
- **migration 適用前の自動バックアップ**（`pre-migrate-*`）は `make migrate` が取得する（`ENV=prod` では取得失敗時に適用を中止）。保持は **新しい順に最大3世代・30日以内**。整理は `make migrate` の成功直後にだけ走り、`pre-migrate-*` のみを対象とする（`design/09-migrations.md` §10）。
- 自動バックアップは `<DBと同じディレクトリ>/backups/` に置かれる（named volume `/data` 上）。
- バックアップは HMAC 化した資格情報と Argon2id ハッシュを含むため、**保管先のアクセスを制限**する。**`APP_PEPPER` は DB バックアップと別経路**で保管する（`design/07-security.md` §8）。

## 4. バックアップの検証（`make migrate-check`）

バックアップを複製して未適用 migration を適用し、整合性と行数を確認する。**本番DBには触らない**（`design/09-migrations.md` §10）。

```sh
make migrate-check SRC=/data/backups/pre-migrate-v0-to-v1-20261010-120000.db
```

出力: `integrity_check`・`foreign_key_check`・テーブル別行数。

## 5. 復旧（migration 失敗時）

down migration は無い（forward-only）。次の順で戻す（`design/09-migrations.md` §10）。

1. アプリを停止する（新規受付停止 → 実行中完了待ち。`design/10-infra-ops.md` §11 graceful shutdown）。
2. 現 DB と `-wal` / `-shm` を退避する。
3. 対象バックアップを DB パス（`DATABASE_PATH`）へ配置する。
4. 修正した migration で `make migrate` を再実行する。

---

## 6. 記録の規約（要約）

| 残してよい | 残してはいけない |
|---|---|
| 対応日時・依頼元・対象 `public_id`、影響行数、実行した手順 | プロフィール本文、`login_id` / パスワード / `recovery_key` / session token / CSRF / `APP_PEPPER` |

不変規則の正本は `design/07-security.md` §8。運用ログの見方は `docs/logging.md`（正本 `design/15-logging.md` §17）を参照。
