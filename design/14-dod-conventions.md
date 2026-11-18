# 設計 14: 完了定義・変更規約

> 関連: [概要](01-overview.md) / [テスト](11-testing.md) / [設計決定](13-decisions.md) / [アーキテクチャ](02-architecture.md)

---

## 15. 完了定義（Definition of Done）

- `make check`（`ruff check` / `ruff format --check` / `pyright` / `pytest`）が全て成功する
- `make e2e`（Playwright＋コンテナ内Chromium）が成功する（M13では全シナリオ通過を含む）
- 空DBから`make migrate`が成功し、再実行しても壊れない。migrationは番号順・forward-only
- H表全件に対応するテストがある。受け入れ基準AC-1〜14は、**一次検証層**（[テスト](11-testing.md) §12）に AC を直接観測するテストがある
- 漏えいテスト（ログ・DBダンプ・エラー応答に秘密値が無い）とアーキテクチャテストが通る
- **UI主体マイルストーン（画面追加を含む M）の完了時は目視確認を必須とする**: `make e2e-headed` で主要動線を目視し、結果を `plan/STATUS.md` §7 に記録する。これは機械テストの代替ではなく、レイアウト・文言・操作感など機械で拾えない退行の確認である
- 挙動が設計書とずれる場合、コードで補正せず承認を求める

---

## 16. 変更規約

**許可**: 命名、private helper、関数分割、仕様を満たすテスト追加、振る舞いを変えないリファクタリング。

**事前承認が必要**: Entity / Context / Aggregate / Portの追加・変更、API追加・変更、DB schema / migration、MVP範囲、Security policy、依存ライブラリとPythonバージョンの変更、秘密値や公開DTOの変更、設計書そのもの。

**不変の安全規則**:
- `user.id`、`profile.id`、所有者情報はURL・HTML・公開API・ログに出さない。公開識別子は`public_id`だけ
- 生password、login_id、session token、recovery code、CSRFトークン、`APP_PEPPER`を、DB・ログ・例外・分析・Referer・URLへ出さない
- URL（path・query・fragment）に認証情報を載せない。ログインIDだけでは認証しない
- ORMを導入しない。SQLはparameterized query。書き込みは`BEGIN IMMEDIATE`のtransaction
- 入力値をエラー応答にechoしない
- MVP外を実装しない
- `.env`・pepper・本番の秘密値を読まない・生成しない・commitしない。テストは明白なダミー値だけを使う

**実装規約**:
- レイヤ依存: `domain`は標準ライブラリのみimport可。`profile`と`identity`は相互にimportしない。Context跨ぎは`src/app/`と`src/main.py`のみ。自作のアーキテクチャテストで検証する
- 時刻・乱数・IDは`Clock` / `SecretGenerator` / `IdGenerator`ポート経由。`datetime.now()`等の直接呼び出しとnaiveな時刻は禁止
- エンドポイントは`def`（threadpool）。`async def`内でDB・Argon2を直接呼ばない
- テストの削除・弱体化・`skip`・`xfail`・期待値の書き換えで通さない。設計とずれるなら承認を求める
- 型検査（Pyright）のエラーを `# type: ignore` / `# pyright: ignore` / 設定緩和で黙らせない。やむを得ない場合は理由を添えて `plan/STATUS.md` に記録し、人間の承認を得る
- コミットは main 直（マイルストーンブランチは切らない）。テストを先に書く。コミットメッセージに対応する仕様章を書く