"""`shared/errors` のテスト。

このファイルが守るべき設計上の約束は次の3点。いずれも `design/06-errors.md` §7 と
`design/14-dod-conventions.md` §16 に由来する。

1. H-1〜H-18 の全18 ID が定義に存在し、各 ID が設計書の条件・status・code と一致する。
   H-8 は外部へ露出しないので code を持たない。
2. code 14種の文言は設計書の「エラーコードと固定文言」表と1文字も違わない。
   実装はこの定数表からのみ返し、入力値・内部理由・秘密値を含めない。
3. 表に無い code を返す経路を作らない。未知の code は `KeyError` で落とす。
"""

import pytest

from src.shared.errors.errors import (
    ERROR_MESSAGES,
    ERROR_STATUS,
    HANDLER_ID,
    ErrorCode,
    message_for,
    status_for,
)

# `design/06-errors.md` §7 の H表と1対1で対応する期待値。
# H-18 は「404 NOT_FOUND / 405 METHOD_NOT_ALLOWED」の2 code を持つ。
# H-8 は外部へ露出しない（再生成・再挿入を最大5回。尽きれば500）ので code を持たない。
EXPECTED_HANDLER_CODES: dict[str, str | tuple[str, str] | None] = {
    "H-1": "PROFILE_NOT_FOUND",
    "H-2": "PROFILE_NOT_FOUND",
    "H-3": "INVALID_CREDENTIALS",
    "H-4": "INVALID_CREDENTIALS",
    "H-5": "INVALID_SESSION",
    "H-6": "INVALID_SESSION",
    "H-7": "INVALID_RECOVERY_CREDENTIAL",
    "H-8": None,
    "H-9": "INTERNAL_ERROR",
    "H-10": "VALIDATION_ERROR",
    "H-11": "INVALID_SESSION",
    "H-12": "CSRF_FAILED",
    "H-13": "RATE_LIMITED",
    "H-14": "INVALID_CURRENT_PASSWORD",
    "H-15": "PROFILE_NOT_PUBLISHABLE",
    "H-16": "PAYLOAD_TOO_LARGE",
    "H-17": "SERVICE_UNAVAILABLE",
    "H-18": ("NOT_FOUND", "METHOD_NOT_ALLOWED"),
}

# `design/06-errors.md` §7「エラーコードと固定文言」表の写し。
# 1文字でも違うと `design/14-dod-conventions.md` §16「設計書の文言を変えない」に反する。
EXPECTED_MESSAGES: dict[str, str] = {
    "PROFILE_NOT_FOUND": "プロフィールが見つかりません",
    "INVALID_CREDENTIALS": "ログインIDまたはパスワードが正しくありません",
    "INVALID_SESSION": "セッションが無効です",
    "INVALID_RECOVERY_CREDENTIAL": "回復用キーが正しくありません",
    "INTERNAL_ERROR": "内部エラーが発生しました",
    "VALIDATION_ERROR": "入力内容に誤りがあります",
    "CSRF_FAILED": "セキュリティチェックに失敗しました",
    "RATE_LIMITED": "リクエストが多すぎます。しばらくしてから再試行してください",
    "INVALID_CURRENT_PASSWORD": "現在のパスワードが正しくありません",
    "PROFILE_NOT_PUBLISHABLE": "公開できる内容がありません",
    "PAYLOAD_TOO_LARGE": "リクエスト本文が大きすぎます",
    "SERVICE_UNAVAILABLE": "サービスを一時的に利用できません",
    "NOT_FOUND": "リソースが見つかりません",
    "METHOD_NOT_ALLOWED": "許可されていないメソッドです",
}

# `design/06-errors.md` §7 の H表の HTTP status 列の写し。
EXPECTED_STATUS: dict[str, int] = {
    "PROFILE_NOT_FOUND": 404,
    "INVALID_CREDENTIALS": 401,
    "INVALID_SESSION": 401,
    "INVALID_RECOVERY_CREDENTIAL": 400,
    "INTERNAL_ERROR": 500,
    "VALIDATION_ERROR": 422,
    "CSRF_FAILED": 403,
    "RATE_LIMITED": 429,
    "INVALID_CURRENT_PASSWORD": 403,
    "PROFILE_NOT_PUBLISHABLE": 422,
    "PAYLOAD_TOO_LARGE": 413,
    "SERVICE_UNAVAILABLE": 503,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
}


def _codes_of(handler_id: str) -> tuple[str, ...]:
    """`HANDLER_ID` の値を code のタプルに正規化する（H-8 は空、H-18 は2件）。"""
    value = HANDLER_ID[handler_id]
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def test_all_handler_ids_exist_and_match_design() -> None:
    """H-1〜H-18 の全18 ID が設計書の code と一致すること。

    ID が1つでも欠けると、以後の層が定数表以外から文言を返す経路になる。
    H-18 だけは 404/405 の2 code を持つ（未知ルート・メソッド不一致で使い分ける）。
    """
    assert set(HANDLER_ID) == set(EXPECTED_HANDLER_CODES)
    for handler_id, expected in EXPECTED_HANDLER_CODES.items():
        actual = HANDLER_ID[handler_id]
        if expected is None:
            assert actual is None
        elif isinstance(expected, tuple):
            assert actual is not None and tuple(actual) == expected
        else:
            assert actual == expected


def test_h8_has_no_external_code() -> None:
    """H-8（一意制約衝突）が外部用 code を持たないこと。

    衝突は最大5回の再生成・再挿入で吸収し、尽きれば 500 として扱う。
    外部へ露出する code を持たせると、存在・状態の漏えいにつながる。
    """
    assert HANDLER_ID["H-8"] is None
    assert "H-8" in HANDLER_ID


def test_shared_codes_use_the_same_code() -> None:
    """H-1/H-2、H-3/H-4、H-5/H-6/H-11 が同じ code を共有すること。

    存在・状態（H-1/H-2）や認証の成否（H-3/H-4）を応答で区別しないための約束。
    セッション系（H-5/H-6/H-11）はすべて `INVALID_SESSION` で Cookie 削除になる。
    """
    assert HANDLER_ID["H-1"] == HANDLER_ID["H-2"] == "PROFILE_NOT_FOUND"
    assert HANDLER_ID["H-3"] == HANDLER_ID["H-4"] == "INVALID_CREDENTIALS"
    assert HANDLER_ID["H-5"] == HANDLER_ID["H-6"] == HANDLER_ID["H-11"] == "INVALID_SESSION"


def test_error_code_has_exactly_14_members() -> None:
    """`ErrorCode` が14種の code だけを保持すること。

    H表の code 列を数えると重複・非露出を除いて14種になる。増減は
    `design/14-dod-conventions.md` §16 の事前承認（公開DTO・Security policy）に触れる。
    """
    assert len(ErrorCode) == 14
    assert {member.value for member in ErrorCode} == set(EXPECTED_MESSAGES)
    assert set(ERROR_MESSAGES) == set(EXPECTED_MESSAGES)
    assert set(ERROR_STATUS) == set(EXPECTED_STATUS)


def test_error_messages_match_design_word_for_word() -> None:
    """code 14種の文言が設計書の表と1文字も違わないこと。

    期待値は設計書の写し。言い換え・翻訳・追加はしない
    （`design/14-dod-conventions.md` §16「設計書の文言を変えない」）。
    """
    assert ERROR_MESSAGES == EXPECTED_MESSAGES
    for code, message in EXPECTED_MESSAGES.items():
        assert message_for(code) == message
        assert message_for(ErrorCode(code)) == message


def test_status_for_matches_design() -> None:
    """`status_for` が設計書の HTTP status と一致すること。

    H表の status 列の写しと突き合わせる。H-18 の2 code（404/405）も含めて、
    ハンドラーID経由の code が正しい status に解決できることを確認する。
    """
    assert ERROR_STATUS == EXPECTED_STATUS
    for code, status in EXPECTED_STATUS.items():
        assert status_for(code) == status
        assert status_for(ErrorCode(code)) == status
    for handler_id in HANDLER_ID:
        for code in _codes_of(handler_id):
            assert status_for(code) == EXPECTED_STATUS[code]


def test_unknown_code_raises_key_error() -> None:
    """表に無い code で `message_for` / `status_for` が `KeyError` を投げること。

    未知の code を返す経路を作らないためのガード。`None` や空文字のような
    あり得ない入力も黙って通さず、呼び出し側のバグとして落とす。
    """
    with pytest.raises(KeyError):
        message_for("UNKNOWN_CODE")
    with pytest.raises(KeyError):
        status_for("UNKNOWN_CODE")
    with pytest.raises(KeyError):
        message_for("")
