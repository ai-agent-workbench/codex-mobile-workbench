"""Short-lived, account-bound cursors for stable conversation index pages."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


PAGE_SIZE = 20
SNAPSHOT_TTL_SECONDS = 30 * 60
MAX_SNAPSHOTS = 128


class ConversationPageCursorError(ValueError):
    """A cursor cannot be used for the requested account or list scope."""

    code = "cursor_invalid"


class ConversationPageCursorExpired(ConversationPageCursorError):
    code = "cursor_expired"


@dataclass(frozen=True)
class ConversationPageSnapshot:
    token: str
    account_id: str
    scope: str
    conversation_ids: tuple[str, ...]
    expires_at: float
    order_version: str = ""


class ConversationPageSnapshots:
    """In-memory snapshot registry; intentionally lost on service restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = SNAPSHOT_TTL_SECONDS,
        maximum: int = MAX_SNAPSHOTS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._maximum = maximum
        self._snapshots: dict[str, ConversationPageSnapshot] = {}
        self._lock = threading.RLock()

    def start(
        self,
        conversation_ids: list[str],
        *,
        account_id: str | None,
        scope: str,
        order_version: str = "",
    ) -> ConversationPageSnapshot:
        now = time.monotonic()
        account = str(account_id or "")
        with self._lock:
            self._purge(now)
            while len(self._snapshots) >= self._maximum:
                oldest = min(self._snapshots.values(), key=lambda item: item.expires_at)
                self._snapshots.pop(oldest.token, None)
            token = secrets.token_urlsafe(24)
            snapshot = ConversationPageSnapshot(
                token=token,
                account_id=account,
                scope=scope,
                conversation_ids=tuple(conversation_ids),
                expires_at=now + self._ttl_seconds,
                order_version=order_version,
            )
            self._snapshots[token] = snapshot
            return snapshot

    def resume(
        self,
        cursor: str,
        *,
        account_id: str | None,
        scope: str,
    ) -> tuple[ConversationPageSnapshot, int]:
        token, position = self._parse_cursor(cursor)
        now = time.monotonic()
        with self._lock:
            snapshot = self._snapshots.get(token)
            if snapshot is None:
                raise ConversationPageCursorError("分页已失效，请刷新页面后重试。")
            if snapshot.expires_at <= now:
                self._snapshots.pop(token, None)
                raise ConversationPageCursorExpired("分页已过期，请刷新页面后重试。")
            if (
                snapshot.account_id != str(account_id or "")
                or snapshot.scope != scope
            ):
                raise ConversationPageCursorError("分页不属于当前列表，请刷新页面后重试。")
            if position < 0 or position >= len(snapshot.conversation_ids):
                raise ConversationPageCursorError("分页位置无效，请刷新页面后重试。")
            return snapshot, position

    @staticmethod
    def cursor(snapshot: ConversationPageSnapshot, position: int) -> str:
        return snapshot.token + "." + str(position)

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[str, int]:
        if not isinstance(cursor, str) or len(cursor) > 128:
            raise ConversationPageCursorError("分页游标无效，请刷新页面后重试。")
        token, separator, raw_position = cursor.rpartition(".")
        if not separator or not token or not raw_position.isascii() or not raw_position.isdecimal():
            raise ConversationPageCursorError("分页游标无效，请刷新页面后重试。")
        if len(token) < 20:
            raise ConversationPageCursorError("分页游标无效，请刷新页面后重试。")
        try:
            return token, int(raw_position)
        except ValueError as error:
            raise ConversationPageCursorError("分页游标无效，请刷新页面后重试。") from error

    def _purge(self, now: float) -> None:
        for token, snapshot in tuple(self._snapshots.items()):
            if snapshot.expires_at <= now:
                self._snapshots.pop(token, None)
