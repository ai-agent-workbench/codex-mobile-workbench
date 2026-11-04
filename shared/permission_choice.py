"""First-use permission enrollment for managed v2 instances only."""
from __future__ import annotations

import sqlite3

from shared.config import PERMISSION_CHOICE_REQUIRED
from shared.workbench_settings import settings_store


class PermissionChoiceRequired(ValueError):
    """A new conversation was requested before the account chose a default."""

    code = "permission_choice_required"

    def __init__(self):
        super().__init__("请先选择新对话的执行权限，再继续创建对话。")


def pending(account: str | None) -> bool:
    """Whether this account must finish the one-time enrollment.

    The profile/environment declares whether this behavior exists; the
    settings database can only record completion, never classify deployment
    age.  Missing/invalid account state remains pending.
    """
    if not PERMISSION_CHOICE_REQUIRED:
        return False
    if not account:
        return True
    try:
        return not settings_store().permission_choice_completed(account)
    except (OSError, sqlite3.Error) as exc:
        # Do not expose storage details or permit a draft when completion
        # cannot be proven.  Route callers already render ValueError safely.
        raise ValueError("首次权限选择状态暂不可读，未新建对话。") from exc


def require_completed(account: str | None) -> None:
    if pending(account):
        raise PermissionChoiceRequired()
