# syntax=docker/dockerfile:1
#
# 1つのDockerfileから複数のtargetを作る。
#   base    : 共通の実行環境（Python・OS・runtime依存のみ。アプリコードを含まない）
#   runtime : base + コード。local / staging / production に配備するリリース用Image（dev依存を含まない）
#   test    : base + dev依存（lint・test専用。配備しない）
#   e2e / e2e-headed : testの上にPlaywright等を足したImage（make checkには含めない）
# base を runtime と test で共有するため、テスト対象の実行環境はリリースImageと一致する。
# コード（pyproject/src/static/migrations/scripts）は base に置かず runtime だけに COPY する。
# base に置くと、コードやツール設定の変更が test → e2e → e2e-headed の重い層
# （playwright install / apt xvfb 等）を無効化し、編集のたびに数分の再ビルドになるため。
# tests も同様にImageへ焼き込まない（実行時に bind mount する）。

ARG PYTHON_VERSION=3.14

# ---------- base ----------
# 共通の実行環境。アプリコードを含まない。
FROM python:${PYTHON_VERSION}-slim-trixie AS base

ENV PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PIP_NO_CACHE_DIR=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  TZ=UTC \
  PYTHONPATH=/app

# sqlite3 CLIは運用手順(DATABASE.md §6)と.backup用。Debianのlibsqlite3(>=3.37)をPythonも使用する。
RUN apt-get update \
  && apt-get install -y --no-install-recommends sqlite3 \
  && rm -rf /var/lib/apt/lists/*

# 非root。/dataを先にこのユーザー所有で作っておくと、空のnamed volumeが初回マウント時に所有権を引き継ぐ。
RUN groupadd --system --gid 10001 app \
  && useradd  --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
  && mkdir -p /data \
  && chown app:app /data

WORKDIR /app

# 依存層（コード変更でキャッシュが無効化されないよう先にコピー）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---------- runtime ----------
# base + アプリ本体（dev依存を含まない）。コードはrootの所有・実行ユーザーは書き込み不可。
FROM base AS runtime
COPY pyproject.toml ./
COPY LICENSE ./LICENSE
COPY src ./src
COPY static ./static
COPY migrations ./migrations
COPY scripts ./scripts

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA \
  org.opencontainers.image.title="anon-profile-app" \
  org.opencontainers.image.licenses="MIT"

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# worker=1固定(DECISIONS D-24)。アクセスログ無効・Serverヘッダー無効。
# Forwarded系ヘッダーはアプリ側(TRUSTED_PROXY_COUNT)で解釈するためuvicornでは信頼しない。
CMD ["uvicorn", "src.main:app", \
  "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
  "--no-access-log", "--no-server-header", "--no-proxy-headers", \
  "--timeout-graceful-shutdown", "25"]

# ---------- test ----------
# base + dev依存。アプリコードとtestsは焼き込まない（compose.local.yaml が実行時に bind mount する）。
FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
USER 10001:10001
CMD ["pytest"]

# ---------- e2e ----------
# testの上にPlaywrightとChromiumを足す。ブラウザはビルド時に取得し、実行時のネットワークを不要にする。
# runtime / test には影響しない（D-02）。
FROM test AS e2e
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements-e2e.txt ./
RUN pip install -r requirements-e2e.txt \
  && playwright install --with-deps chromium \
  && chmod -R a+rX /ms-playwright \
  && rm -rf /var/lib/apt/lists/*
USER 10001:10001
# addopts を空にして、通常テストの --ignore=tests/e2e を打ち消す
# --output はマウントのサブディレクトリ（pytest-playwright が出力ディレクトリを削除・再作成するため）
CMD ["pytest", "-o", "addopts=", "tests/e2e", "--output", "/app/out/e2e", "--tracing", "retain-on-failure", "--screenshot", "only-on-failure"]

# ---------- e2e-headed ----------
# 目視確認用（D-05）。e2e に Xvfb + fluxbox + x11vnc + noVNC を足し、Chromium の画面だけを
# コンテナ外（127.0.0.1 に限定）へ出す。通常の e2e / test / runtime には影響しない。
# entrypoint は build context の scripts/e2e から直接 COPY する（base / test / e2e はコードを
# 含まないため）。.dockerignore が deploy を除外し再包含できないため scripts/ 配下に置く
# （plan/archive/proposals/e2e-headed-vnc.md §0.1）。重い apt より後に COPY して、スクリプト
# 編集で apt 層が無効化されないようにする。
FROM e2e AS e2e-headed
USER root
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       xvfb x11vnc novnc websockify fluxbox \
  && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 scripts/e2e/headed-entrypoint.sh /usr/local/bin/headed-entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/headed-entrypoint.sh"]
# ENTRYPOINT を定義すると親ステージの CMD は引き継がれないため、e2e と同じ CMD を再掲する。
CMD ["pytest", "-o", "addopts=", "tests/e2e", "--output", "/app/out/e2e", "--tracing", "retain-on-failure", "--screenshot", "only-on-failure"]
