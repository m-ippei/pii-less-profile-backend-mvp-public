"""クライアントIPの抽出（`design/07-security.md` §8 Rate limit /
`design/10-infra-ops.md` §11 設定）。

アプリの直接の接続相手はリバースプロキシ（Caddy）なので、`X-Forwarded-For`（XFF）から
実クライアントを得る。XFF は各プロキシが接続元IPを右へ追記するため、右端が直近プロキシの
観測した実クライアントになる。クライアントが偽装した左側の値を信頼しないよう、信頼する
プロキシ段数 `TRUSTED_PROXY_COUNT` ぶんだけ右から辿る（Werkzeug `ProxyFix` の
`values[-trusted]` と同じ）。段数0のときは XFF を信頼せず接続元を使う。

XFF の解釈は各エンドポイントへ散らさず、この1箇所に閉じる。
"""

import os
from collections.abc import Mapping

from starlette.requests import Request

DEFAULT_TRUSTED_PROXY_COUNT = 0


def client_ip(
    *,
    x_forwarded_for: str | None,
    peer: str | None,
    trusted_proxy_count: int,
) -> str:
    """XFF と接続元から実クライアントIPを求める。

    - `trusted_proxy_count == 0` は XFF を信頼せず `peer` を返す
    - `trusted_proxy_count > 0` は右端から数えて `trusted_proxy_count` 番目を返す
    - XFF が欠落・空・段数に足りない場合は `peer` へフォールバックする

    `peer` が `None`（ASGI の `client` 未設定）のときは空文字を返す。
    """
    if trusted_proxy_count < 0:
        raise ValueError("trusted_proxy_count must be non-negative")
    if trusted_proxy_count == 0:
        return peer or ""
    hops = _parse_forwarded_for(x_forwarded_for)
    if len(hops) >= trusted_proxy_count:
        return hops[-trusted_proxy_count]
    return peer or ""


def load_trusted_proxy_count(environ: Mapping[str, str] | None = None) -> int:
    """`TRUSTED_PROXY_COUNT` を読む。既定0、不正は `ValueError`。

    `design/10-infra-ops.md` §11「起動時に検証して不正なら起動しない」に従い、整数に
    解釈できない値・負値は拒否する。
    """
    raw = (os.environ if environ is None else environ).get(
        "TRUSTED_PROXY_COUNT", str(DEFAULT_TRUSTED_PROXY_COUNT)
    )
    try:
        count = int(raw)
    except ValueError as error:
        raise ValueError("TRUSTED_PROXY_COUNT must be an integer") from error
    if count < 0:
        raise ValueError("TRUSTED_PROXY_COUNT must be non-negative")
    return count


def _parse_forwarded_for(value: str | None) -> list[str]:
    """XFF をカンマで分割し、前後空白を除去、空要素を落とす。"""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def request_client_ip(request: Request) -> str:
    """`Request` から実クライアントIPを求める。

    `X-Forwarded-For` と接続元（`request.client`）を `client_ip` へ渡す。信頼プロキシ
    段数は環境変数 `TRUSTED_PROXY_COUNT` から読む（`design/10-infra-ops.md` §11）。
    """
    peer = request.client.host if request.client is not None else None
    return client_ip(
        x_forwarded_for=request.headers.get("x-forwarded-for"),
        peer=peer,
        trusted_proxy_count=load_trusted_proxy_count(),
    )
