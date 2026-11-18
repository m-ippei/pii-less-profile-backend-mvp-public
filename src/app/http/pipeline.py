"""リクエスト処理段の順序保証（design/05-api-contract.md §6 判定順 /
design/07-security.md §8 認証フロー）。

判定順は `413` → CSRF(`403`) → 認証(`401`) → 入力検証(`422`) → rate limit(`429`) →
use case。このうち

- `413` は `BodySizeLimitMiddleware` が、
- CSRF(`403`) は `CsrfMiddleware` が

ミドルウェアとして先に確定する（`413` が CSRF より外側。`design/05-api-contract.md` §6
「413 が CSRF より前の理由」）。その内側の「認証 → 入力検証 → rate limit → use case」を、
各エンドポイントの実装に委ねず `run_request_pipeline` で固定する。

各段は callable（省略可）で受け取る。ある段が送出した例外は握りつぶさずそのまま外へ出し、
T-07-04 の例外ハンドラに status へ変換させる。M08/M09 のエンドポイントがこのヘルパーを使う
（本モジュールにエンドポイントは置かない）。
"""

from collections.abc import Callable


def run_request_pipeline[AuthT, InputT, ResultT](
    execute: Callable[[AuthT | None, InputT | None], ResultT],
    *,
    authenticate: Callable[[], AuthT] | None = None,
    validate: Callable[[], InputT] | None = None,
    rate_limit: Callable[[AuthT | None, InputT | None], None] | None = None,
) -> ResultT:
    """認証 → 入力検証 → rate limit → use case をこの順で実行する。

    - `authenticate` は省略可（認証不要のエンドポイント）。結果は rate limit と use case へ渡す
    - `validate` は省略可。結果は rate limit と use case へ渡す
    - `rate_limit` は省略可。拒否は例外で表す（`429` への変換はハンドラ側）
    - `execute` は最後に必ず1回だけ呼び、その戻り値を返す

    前段が例外を送出したら後段は呼ばない（順序保証）。
    """
    auth_result = authenticate() if authenticate is not None else None
    input_result = validate() if validate is not None else None
    if rate_limit is not None:
        rate_limit(auth_result, input_result)
    return execute(auth_result, input_result)
