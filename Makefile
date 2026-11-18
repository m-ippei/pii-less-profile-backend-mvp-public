COMPOSE  := docker compose -f compose.yaml -f compose.local.yaml
RUN_TEST := $(COMPOSE) --profile tools run --rm --no-deps test
RUN_FMT  := $(COMPOSE) --profile tools run --rm --no-deps fmt
RUN_DESIGN := $(COMPOSE) --profile tools run --rm --no-deps design
E2E_COMPOSE := docker compose -p anon-profile-e2e --env-file deploy/e2e/e2e.env -f compose.yaml -f compose.e2e.yaml
E2E_HEADED_COMPOSE := $(E2E_COMPOSE) -f compose.e2e-headed.yaml
E2E_VNC_PORT ?= 6080
ARGS ?=

.PHONY: build up down restart ps logs shell db-shell migrate migrate-check lint typecheck test fmt py check fix sh design design-diff e2e e2e-build e2e-down e2e-headed e2e-headed-build trust-ca reset-data

build:
	$(COMPOSE) build app
	$(COMPOSE) --profile tools build test

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart app

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100 app reverse-proxy

shell:
	$(COMPOSE) exec app sh

# foreign_keys=ONが必須（DATABASE.md §1）
db-shell:
	$(COMPOSE) exec app sqlite3 -cmd "PRAGMA foreign_keys=ON;" /data/app.db

# マイグレーションは明示実行のみ（アプリ起動時に自動実行しない）
migrate:
	$(COMPOSE) run --rm --no-deps app python scripts/migrate.py

# バックアップの整合性とテーブル別行数を確認する（design/09-migrations.md §10）。
# 例: make migrate-check SRC=/data/backups/pre-migrate-v0-to-v1-20261010-120000.db
# バックアップを複製して未適用 migration を適用し、本番DBには触らない。
migrate-check:
	$(COMPOSE) run --rm --no-deps app python scripts/migrate_check.py "$(SRC)"

lint:
	$(RUN_TEST) ruff check .
	$(RUN_TEST) ruff format --check .

# 型検査(Pyright)。設定は pyproject.toml の [tool.pyright]
typecheck:
	$(RUN_TEST) pyright $(ARGS)

test:
	$(RUN_TEST) pytest $(ARGS)

# 整形・自動修正（書き込み可能なマウントで実行）
fmt:
	$(RUN_FMT) ruff check --fix .
	$(RUN_FMT) ruff format .

# 任意のPythonスクリプト実行: make py ARGS="scripts/xxx.py"
py:
	$(RUN_TEST) python $(ARGS)

# tools イメージの対話シェル（B-3）。ホストの python を直接叩かないための入口
sh:
	$(COMPOSE) --profile tools run --rm --no-deps test sh

# 最終ゲート。4ゲートを1回のコンテナ起動で順に実行する（B-1）。
# ホストのコマンドシェル（Windows cmd 等）に依存しないよう、単一コマンド＋コンテナ内 sh で連結する（F-1）。
check:
	$(RUN_TEST) sh -c "ruff check . && ruff format --check . && pyright && pytest"

# 整形してから検証する（B-2）
fix:
	$(MAKE) fmt
	$(MAKE) check

# design/（正本）から DESIGN.md を生成（D-04。既存 test イメージを流用）
design:
	$(RUN_DESIGN) python scripts/design_sync.py assemble

# 生成結果と既存 DESIGN.md の差分を表示（非破壊。標準の diff を使用。差分があれば失敗）
design-diff:
	$(RUN_DESIGN) sh -c "python scripts/design_sync.py assemble --design-md /tmp/DESIGN.generated.md && diff -u DESIGN.md /tmp/DESIGN.generated.md"

# Caddy local CAのルート証明書を取り出す（Windowsに信頼登録する場合）
trust-ca:
	$(COMPOSE) cp reverse-proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-local-root.crt

# localのDBを空に戻す（appを止めてvolumeごと削除）
reset-data:
	$(COMPOSE) down -v

# E2E(Playwright)基盤（D-02）。make check には含めない。
e2e-build:
	$(E2E_COMPOSE) --profile e2e build app e2e

e2e-down:
	$(E2E_COMPOSE) --profile e2e down -v --remove-orphans

# 毎回空DBから（Windowsのcmdでも動くよう、シェル機能は使わず複数行に分ける）。
# 成否にかかわらず破棄したい場合は、失敗後に `make e2e-down` を実行する。
e2e: e2e-build
	-$(E2E_COMPOSE) --profile e2e down -v --remove-orphans
	$(E2E_COMPOSE) run --rm --no-deps app python scripts/migrate.py
	$(E2E_COMPOSE) --profile e2e run --rm e2e $(ARGS)
	$(E2E_COMPOSE) --profile e2e down -v --remove-orphans

# E2E 目視確認モード（任意・D-05）。通常の `make e2e` には影響しない。
# 起動後に Windows のブラウザで http://127.0.0.1:$(E2E_VNC_PORT)/vnc.html を開き「接続」する。
# 手動操作はテストに一時的に page.pause() を入れるか、make e2e-headed PWDEBUG=1 ARGS=... で行う。
#
# e2e-headed は ENTRYPOINT（Xvfb等の起動）を持つため、`run e2e <ARGS>` は CMD ではなく ENTRYPOINT の
# 引数になる（CMD は置換されない）。よって pytest 本体をここで明示し、ARGS をその末尾に足す。
# この pytest 引数は Dockerfile の e2e / e2e-headed の CMD と同一に保つ（変更時は両方）。
E2E_HEADED_PYTEST := pytest -o addopts= tests/e2e --output /app/out/e2e --tracing retain-on-failure --screenshot only-on-failure

e2e-headed-build:
	$(E2E_HEADED_COMPOSE) --profile e2e build app e2e

e2e-headed: e2e-headed-build
	-$(E2E_HEADED_COMPOSE) --profile e2e down -v --remove-orphans
	$(E2E_HEADED_COMPOSE) run --rm --no-deps app python scripts/migrate.py
	@echo noVNC: http://127.0.0.1:$(E2E_VNC_PORT)/vnc.html
	$(E2E_HEADED_COMPOSE) --profile e2e run --rm --service-ports e2e $(E2E_HEADED_PYTEST) $(ARGS)
	$(E2E_HEADED_COMPOSE) --profile e2e down -v --remove-orphans
