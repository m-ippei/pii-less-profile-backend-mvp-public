# 設計 10: インフラ・運用

> 関連: [migration運用](09-migrations.md) / [データベース](08-database.md) / [セキュリティ](07-security.md) / [エラー処理](06-errors.md) / [デプロイ・リリース・運用基盤](16-deployment.md)

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
| `RATE_LIMIT_MAX_KEYS` | インメモリ rate limit の key 上限（既定 10,000）。容量 fail-closed のしきい値（[§8](07-security.md)。1以上） |

> **公開版の記載方針**: 以下の Argon2 / rate limit の値は公開版の既定値の例。本番値は環境ごとに tune し、非公開の運用手順で管理する。

- `ENV=test`のときだけ、テスト用の軽量Argon2パラメータとダミーpepperをfixtureで指定してよい。`.env`はcommitせず`.env.example`のみ置く
- テストは`https://testserver`のbase URLを使い、`Secure` cookieを外す設定は作らない

### 実行モデルとDB

- エンドポイントは`def`（threadpool）。標準`sqlite3`とArgon2はブロッキングのため`async def`内で直接呼ばない。uvicornのworkerは1
- **threadpool上限は8**（`anyio.to_thread.current_default_thread_limiter().total_tokens = 8`）。40の既定値に任せず、待機スレッド増殖を防ぐ
- **Argon2の実行枠（`ARGON2_MAX_CONCURRENCY`）は待たせない**。枠が埋まれば認証処理へthreadpoolを投入せず、429または503を返す。**Argon2 呼び出し（`hash` / `verify` / `dummy_verify`）は transaction（`BEGIN IMMEDIATE`）の外で行い、書き込みロックを保持しない**（[§4](04-use-cases.md) §5）
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
- **日次バックアップ・オフサイト転送・監視・Argon2 tune・リリース/デプロイは [§18](16-deployment.md) を正本とする**（本節の `pre-migrate-*` とは別物）