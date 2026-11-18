"""Profile domain の例外（design/03-domain-model.md §4 Domain rule）。

domain 層は標準ライブラリのみを import するため（design/02-architecture.md §3）、
外部へ露出する `code` への対応付けは application 層で行う。
"""


class ProfileDomainError(Exception):
    """Profile domain の不変条件違反の基底。"""


class ProfileNotPublishableError(ProfileDomainError):
    """H-15: 更新後の `content` が空のまま publish しようとした。"""


class InvalidProfileTransitionError(ProfileDomainError):
    """不正な状態遷移（draft 以外からの publish、published 以外からの unpublish）。"""
