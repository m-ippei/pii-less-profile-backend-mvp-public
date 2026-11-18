# 設計 09: migration運用

> 関連: [データベース](08-database.md) / [インフラ・運用](10-infra-ops.md) / [テスト](11-testing.md)

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
- **日次バックアップ・オフサイト転送・復元は [§18](16-deployment.md) を正本とする**。本節の `pre-migrate-*` は migration 適用前の専用バックアップであり、日次・オフサイトとは別物（相互に削除しない）

### 見直しのトリガー

次のいずれかが現実になったら、PostgreSQLとmigrationツール（Alembic/Atlas等）への移行を再検討する。

- worker>1または複数インスタンスが必要になる
- 単一writerが律速になる
- 時点復旧（PITR）や読み取りレプリカが必要になる
- テーブル再作成が常態化する（目安: 再作成型のmigrationが半数を超える）