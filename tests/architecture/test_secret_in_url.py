"""AC-13: URL（path・query・fragment）に認証情報を載せない（design/07-security.md §8 /
design/11-testing.md §12 architecture 層）。

静的な**不在検証**として、`static/` の HTML / JS が秘密値（`login_id` / `recovery_key` /
`password` / session token 等）を URL へ載せていないことを走査する。秘密値のクライアント保持は
`sessionStorage`（`secrets.js`）に限る。遷移後に実際に URL へ出ないことの実行時検証は
e2e（M09 T-09-05）が担う。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "static"

SECRET_TOKENS = ("login_id", "recovery_key", "password", "initial_password", "ap_secrets")

# URL を組み立て・遷移しうる記述（引数または代入式を捕捉する）。
_URL_SITES = (
    re.compile(r"location\.assign\s*\(([^)]*)\)"),
    re.compile(r"location\.replace\s*\(([^)]*)\)"),
    re.compile(r"location\.href\s*=\s*([^;\n]+)"),
    re.compile(r"window\.open\s*\(([^)]*)\)"),
    re.compile(r"fetch\s*\(([^,)]*)"),
)

# query / fragment を書ける記述は、値を問わず使わない（URL に値が載る余地を消す）。
_FORBIDDEN_SITES = (
    re.compile(r"location\.search\s*="),
    re.compile(r"location\.hash\s*="),
    re.compile(r"history\.(?:pushState|replaceState)\s*\("),
)

# `stashSecrets` に渡す `next`（遷移先）は定型パスに限る。
_NEXT_ASSIGN = re.compile(r"next\s*:\s*([^,\n}]+)")
_PATH_LITERAL = re.compile(r'^"/[^"?\n]*"$')
_HTML_URL_ATTR = re.compile(r'(?:href|action)\s*=\s*"([^"]*)"')


def _js_files() -> list[Path]:
    return sorted((STATIC / "js").glob("*.js"))


def _url_violations(text: str) -> list[str]:
    """URL へ秘密値を載せる／動的に URL を組み立てる記述を返す（走査ロジックの本体）。"""
    violations: list[str] = []
    for pattern in _URL_SITES:
        for match in pattern.finditer(text):
            arg = match.group(1).strip()
            for token in SECRET_TOKENS:
                if token in arg:
                    violations.append(f"secret {token!r} in URL expression: {match.group(0)}")
            if "+" in arg:
                violations.append(f"dynamic URL expression: {match.group(0)}")
    for pattern in _FORBIDDEN_SITES:
        for match in pattern.finditer(text):
            violations.append(f"forbidden URL site: {match.group(0)}")
    return violations


def _next_violations(text: str) -> list[str]:
    """`next: <値>` の値が定型パス（`"/..."`）でないものを返す。"""
    return [
        match.group(1).strip()
        for match in _NEXT_ASSIGN.finditer(text)
        if not _PATH_LITERAL.match(match.group(1).strip())
    ]


def _html_url_violations(text: str) -> list[str]:
    """HTML の `href` / `action` 属性に秘密値または動的合成が含まれる記述を返す。"""
    return [
        value
        for value in (match.group(1) for match in _HTML_URL_ATTR.finditer(text))
        if any(token in value for token in SECRET_TOKENS) or "+" in value
    ]


def test_static_js_keeps_secrets_out_of_urls() -> None:
    """`static/js/*.js` が秘密値を URL に載せない。"""
    files = _js_files()
    assert files, "static/js の JS が見つからない"
    violations: list[str] = []
    for path in files:
        for message in _url_violations(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {message}")
    assert violations == []


def test_navigation_targets_are_static_paths() -> None:
    """`stashSecrets` の遷移先 `next` が定型パス（`"/..."`）である。"""
    violations: list[str] = []
    for path in _js_files():
        for value in _next_violations(path.read_text(encoding="utf-8")):
            violations.append(f"{path.name}: next={value!r}")
    assert violations == []


def test_static_html_keeps_secrets_out_of_urls() -> None:
    """`static/*.html` の `href` / `action` に秘密値・動的合成が無い。"""
    files = sorted(STATIC.glob("*.html"))
    assert files, "static の HTML が見つからない"
    violations: list[str] = []
    for path in files:
        for value in _html_url_violations(path.read_text(encoding="utf-8")):
            violations.append(f"{path.name}: {value!r}")
    assert violations == []


def test_secrets_module_uses_session_storage_not_url() -> None:
    """`secrets.js` は `sessionStorage` を使い、URL へ秘密値を載せない。"""
    text = (STATIC / "js" / "secrets.js").read_text(encoding="utf-8")
    assert "sessionStorage" in text
    assert _url_violations(text) == []


def test_scanner_detects_secret_in_url() -> None:
    """陽性対照: 秘密値を URL に載せる記述を検出する。"""
    assert _url_violations('window.location.assign("/x?login_id=" + login_id);')
    assert _url_violations('location.search = "login_id=" + login_id;')
    assert _url_violations('history.pushState({}, "", "/x?recovery_key=" + recovery_key);')


def test_scanner_detects_dynamic_url() -> None:
    """陽性対照: 秘密値でなくても動的合成の URL を検出する。"""
    assert _url_violations('window.location.assign("/profiles/" + public_id);')


def test_scanner_detects_bad_next() -> None:
    """陽性対照: `next` が定型パスでなければ検出する。"""
    assert _next_violations('stashSecrets({ next: "/x?recovery_key=" + key });')


def test_scanner_detects_secret_in_html_url() -> None:
    """陽性対照: HTML の URL 属性に秘密値があれば検出する。"""
    assert _html_url_violations('<a href="/login?recovery_key=x">x</a>')


def test_scanner_passes_clean_text() -> None:
    """陰性対照: 定型パスと no-secret の fetch は検出しない。"""
    clean = 'window.location.assign("/credentials-saved");\nfetch("/api/v1/me/profile", {});\n'
    assert _url_violations(clean) == []
