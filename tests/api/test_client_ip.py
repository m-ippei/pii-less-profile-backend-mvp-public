"""クライアントIP抽出のテスト（design/07-security.md §8 Rate limit /
design/10-infra-ops.md §11 設定）。

T-10-02 の完了条件「`X-Forwarded-For` の右から `TRUSTED_PROXY_COUNT` 個を除いた IP を
返す（0/1/複数段の境界）」を、`client_ip` の単体で観測する。

- 段数0は XFF を信頼せず、接続元（`peer`）を返す
- 段数N（>0）は右端から数えて N 番目の値（`parts[-N]`）を返す
- 値が足りない（XFF 欠落・段数不足）ときは接続元へフォールバックする

`X-Forwarded-For` は各プロキシが接続元IPを右へ追記するため、右端が直近プロキシの
観測した実クライアントになる（Caddy の `reverse_proxy` の既定、Werkzeug `ProxyFix` の
`values[-trusted]` と同じ）。
"""

import pytest

from src.app.http.client_ip import client_ip, load_trusted_proxy_count

PEER = "10.0.0.1"


def test_zero_trusted_proxies_uses_the_direct_peer() -> None:
    """段数0では XFF を信頼せず、接続元を返すこと。クライアントが XFF を偽装しても無効。"""
    assert client_ip(x_forwarded_for="9.9.9.9", peer=PEER, trusted_proxy_count=0) == PEER


def test_single_trusted_proxy_returns_the_rightmost_value() -> None:
    """段数1では右端（直近プロキシが追記した実クライアント）を返すこと。"""
    assert client_ip(x_forwarded_for="1.1.1.1", peer=PEER, trusted_proxy_count=1) == "1.1.1.1"


def test_single_trusted_proxy_ignores_forged_left_values() -> None:
    """段数1では、クライアントが左に足した偽装値を無視し右端を返すこと。"""
    assert (
        client_ip(x_forwarded_for="9.9.9.9, 1.1.1.1", peer=PEER, trusted_proxy_count=1) == "1.1.1.1"
    )


def test_multiple_trusted_proxies_skip_from_the_right() -> None:
    """段数2では右端から2番目（2段目のプロキシが観測した実クライアント）を返すこと。"""
    assert (
        client_ip(
            x_forwarded_for="9.9.9.9, 1.1.1.1, 10.0.0.5",
            peer=PEER,
            trusted_proxy_count=2,
        )
        == "1.1.1.1"
    )


def test_missing_header_falls_back_to_the_peer() -> None:
    """XFF が欠落しているときは接続元を返すこと。"""
    assert client_ip(x_forwarded_for=None, peer=PEER, trusted_proxy_count=1) == PEER


def test_too_few_values_fall_back_to_the_peer() -> None:
    """段数より値が少ないときは接続元を返すこと（ProxyFix と同じ安全側）。"""
    assert client_ip(x_forwarded_for="1.1.1.1", peer=PEER, trusted_proxy_count=2) == PEER


def test_values_are_trimmed_and_empty_entries_dropped() -> None:
    """カンマ区切りの前後空白を除去し、空要素は無視すること。"""
    assert (
        client_ip(x_forwarded_for=" 1.1.1.1 , 2.2.2.2 ", peer=PEER, trusted_proxy_count=1)
        == "2.2.2.2"
    )


def test_negative_trusted_proxy_count_is_rejected() -> None:
    """負の段数は設定ミスとして拒否すること。"""
    with pytest.raises(ValueError):
        client_ip(x_forwarded_for="1.1.1.1", peer=PEER, trusted_proxy_count=-1)


def test_load_trusted_proxy_count_defaults_to_zero() -> None:
    """`TRUSTED_PROXY_COUNT` が未設定なら 0 とすること（design/10-infra-ops.md §11 既定0）。"""
    assert load_trusted_proxy_count({}) == 0


def test_load_trusted_proxy_count_reads_the_value() -> None:
    """`TRUSTED_PROXY_COUNT` の整数値を読むこと。"""
    assert load_trusted_proxy_count({"TRUSTED_PROXY_COUNT": "2"}) == 2


def test_load_trusted_proxy_count_rejects_invalid_values() -> None:
    """整数に解釈できない値・負値は起動時に拒否すること（不正なら起動しない）。"""
    with pytest.raises(ValueError):
        load_trusted_proxy_count({"TRUSTED_PROXY_COUNT": "abc"})
    with pytest.raises(ValueError):
        load_trusted_proxy_count({"TRUSTED_PROXY_COUNT": "-1"})
