"""`static/` の禁止 API とインライン記述の不在検証（design/05-api-contract.md §6 /
design/11-testing.md §12 static 層）。

静的 HTML / JS は「インラインスクリプト禁止・`innerHTML` 等の禁止 API 不使用・
`style=` 属性なし・ローカルの外部ファイルのみ参照」を満たす。`static` 層は
セキュリティ不変規則の**不在検証のみ**を行い、成果物テキストの存在確認はしない
（`design/11-testing.md` §12）。`<script>` 実行不成立の一次証拠は e2e（AC-12）。
"""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

# 本文を DOM へ挿入して XSS を招く API（`design/05-api-contract.md` §6）。
FORBIDDEN_JS_TOKENS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
)

_STYLE_ATTRIBUTE = re.compile(r"\sstyle\s*=")
_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=")
_SCRIPT_SRC = re.compile(r"<script[^>]*\ssrc=\"([^\"]+)\"")
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)")


def _files(suffix: str) -> list[Path]:
    return sorted(STATIC_DIR.rglob(f"*{suffix}"))


def test_static_js_exists() -> None:
    """`static/` に少なくとも1つの JS が存在する（走査対象の空振り検出）。"""
    assert _files(".js")


def test_required_screen_assets_exist() -> None:
    """M09 の作成・編集・公開画面のアセットが存在する（走査対象の空振り検出）。"""
    present = {
        path.relative_to(STATIC_DIR).as_posix() for path in STATIC_DIR.rglob("*") if path.is_file()
    }
    for name in (
        "new.html",
        "profile-editor.html",
        "profiles.html",
        "js/new.js",
        "js/profile-editor.js",
        "js/public-profile.js",
    ):
        assert name in present, name


def test_js_has_no_forbidden_apis() -> None:
    """`static/` の JS に禁止 API が含まれないこと。"""
    for path in _files(".js"):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_JS_TOKENS:
            assert token not in text, f"{path.name}: {token}"


def test_js_files_contain_no_script_tags() -> None:
    """JS ファイル自体に `<script>` が混入していないこと。"""
    for path in _files(".js"):
        assert "<script" not in path.read_text(encoding="utf-8"), path.name


def test_html_has_no_inline_script_or_style_attribute() -> None:
    """HTML にインライン `<script>`・`style=` 属性・インラインイベントハンドラが無いこと。"""
    for path in _files(".html"):
        text = path.read_text(encoding="utf-8")
        assert not _INLINE_SCRIPT.search(text), path.name
        assert not _STYLE_ATTRIBUTE.search(text), path.name
        assert not _INLINE_HANDLER.search(text), path.name


def test_html_references_only_local_static_scripts() -> None:
    """HTML の `<script src>` が `/static/js/` のローカル参照だけであること。"""
    for path in _files(".html"):
        for match in _SCRIPT_SRC.finditer(path.read_text(encoding="utf-8")):
            assert match.group(1).startswith("/static/js/"), f"{path.name}: {match.group(1)}"
