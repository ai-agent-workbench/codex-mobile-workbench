# Self-owned historical compatibility fixture; MIT.
# Selected file only, not private Git history. Identity literals normalized.
"""Durable App Server conversation projection.

The native App Server thread remains authoritative.  This SQLite store keeps
only the small amount of Web-owned state needed to render a conversation,
deduplicate browser commands, and recover after a Web process restart.

There is deliberately no process, PID, stdout, exit-code, parent-task, or
legacy-backend state in this schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.codex_app_server import AppServerProtocolError


SCHEMA_VERSION = 6
WEB_PUSH_SCHEMA_VERSION = 1
ACTIVE_TURN_STATUSES = {"inProgress", "active", "queued", "stopping"}
TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
CONVERSATION_STATES = {
    "new",
    "starting_thread",
    "idle",
    "starting_turn",
    "active",
    "interrupting",
    "reconciling",
    "needs_user",
}
COMMAND_STATES = {
    "submitted",
    "queued",
    "accepted",
    "delivery_unknown",
    "rejected",
    "completed",
}
_VISIBLE_TEXT_LIMIT = 120_000
_VISIBLE_DETAIL_LIMIT = 80_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _object(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


class ConversationStore:
    """Thread-safe, transactional local projection of native conversations."""

    def __init__(self, database_path: str | os.PathLike[str]):
        self.path = Path(database_path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        try:
            self._prepare()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _prepare(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    active_turn_id TEXT,
                    thread_status TEXT,
                    settings_json TEXT NOT NULL,
                    token_usage_json TEXT,
                    recovery_state TEXT,
                    connection_state TEXT NOT NULL,
                    server_version TEXT,
                    last_error_json TEXT,
                    completion_notification INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    client_origin TEXT,
                    owner_account_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    activity_at TEXT NOT NULL,
                    terminal_generation INTEGER NOT NULL DEFAULT 0,
                    last_terminal_turn_id TEXT,
                    last_terminal_at TEXT,
                    revision INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'model',
                    started_at TEXT,
                    completed_at TEXT,
                    final_text TEXT,
                    error_json TEXT,
                    notification_requested INTEGER NOT NULL DEFAULT 0,
                    notification_state TEXT,
                    notification_detail TEXT,
                    UNIQUE(conversation_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    target_turn_id TEXT,
                    result_turn_id TEXT,
                    text TEXT,
                    payload_json TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    native_item_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT,
                    text TEXT,
                    detail TEXT,
                    source TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(turn_id, native_item_id, kind)
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    call_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    tool TEXT NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    native_item_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_relpath TEXT NOT NULL,
                    preview_kind TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS conversation_reads (
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    seen_generation INTEGER NOT NULL DEFAULT 0,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS context_compaction_events (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    native_item_id TEXT NOT NULL,
                    started_observed_at TEXT,
                    completed_observed_at TEXT,
                    PRIMARY KEY(conversation_id, turn_id, native_item_id)
                );

                CREATE INDEX IF NOT EXISTS turns_conversation_idx
                    ON turns(conversation_id, ordinal);
                CREATE INDEX IF NOT EXISTS commands_conversation_idx
                    ON commands(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS items_turn_idx
                    ON items(turn_id, row_id);
                CREATE INDEX IF NOT EXISTS artifacts_conversation_idx
                    ON artifacts(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS tool_calls_conversation_idx
                    ON tool_calls(conversation_id, state, tool, turn_id);
                CREATE INDEX IF NOT EXISTS conversation_reads_conversation_idx
                    ON conversation_reads(conversation_id, account_id);
                CREATE INDEX IF NOT EXISTS context_compaction_completed_idx
                    ON context_compaction_events(
                        conversation_id, completed_observed_at
                    );
                """
            )
            existing = self._db.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._db.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif existing["value"] in {"1", "2", "3", "4", "5"}:
                # Keep every historical upgrade transactional and tolerate a
                # database left half-upgraded by an older pre-release build.
                try:
                    self._begin()
                    conversation_columns = {
                        row["name"]
                        for row in self._db.execute(
                            "PRAGMA table_info(conversations)"
                        ).fetchall()
                    }
                    turn_columns = {
                        row["name"]
                        for row in self._db.execute(
                            "PRAGMA table_info(turns)"
                        ).fetchall()
                    }
                    item_columns = {
                        row["name"]
                        for row in self._db.execute(
                            "PRAGMA table_info(items)"
                        ).fetchall()
                    }
                    if "pinned" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations "
                            "ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
                        )
                    if "sort_order" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations "
                            "ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                        )
                    if "notification_state" not in turn_columns:
                        self._db.execute(
                            "ALTER TABLE turns ADD COLUMN notification_state TEXT"
                        )
                    if "notification_detail" not in turn_columns:
                        self._db.execute(
                            "ALTER TABLE turns ADD COLUMN notification_detail TEXT"
                        )
                    if "notification_requested" not in turn_columns:
                        self._db.execute(
                            "ALTER TABLE turns ADD COLUMN notification_requested "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                    # Old NULL rows are historical turns for which no delivery
                    # was ever claimed.  Only preserve explicit delivery state;
                    # otherwise enabling a conversation later would replay its
                    # entire history on the next Web-service restart.
                    self._db.execute(
                        """
                        UPDATE turns SET notification_requested = 1
                        WHERE notification_state IN ('sending','sent','failed')
                        """
                    )
                    self._db.execute(
                        """
                        UPDATE turns SET notification_requested = 1
                        WHERE status NOT IN ('completed','failed','interrupted')
                          AND EXISTS (
                              SELECT 1 FROM conversations c
                              WHERE c.id = turns.conversation_id
                                AND c.completion_notification = 1
                          )
                        """
                    )
                    if "token_usage_json" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations ADD COLUMN token_usage_json TEXT"
                        )
                    if "source" not in item_columns:
                        self._db.execute("ALTER TABLE items ADD COLUMN source TEXT")
                    if "activity_at" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations ADD COLUMN activity_at TEXT"
                        )
                    if "terminal_generation" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations ADD COLUMN "
                            "terminal_generation INTEGER NOT NULL DEFAULT 0"
                        )
                    if "last_terminal_turn_id" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations ADD COLUMN "
                            "last_terminal_turn_id TEXT"
                        )
                    if "last_terminal_at" not in conversation_columns:
                        self._db.execute(
                            "ALTER TABLE conversations ADD COLUMN last_terminal_at TEXT"
                        )
                    self._db.execute(
                        """
                        UPDATE conversations
                        SET activity_at = COALESCE(
                            (
                                SELECT MAX(COALESCE(t.completed_at, t.started_at))
                                FROM turns t
                                WHERE t.conversation_id = conversations.id
                            ),
                            (
                                SELECT MAX(cmd.created_at)
                                FROM commands cmd
                                WHERE cmd.conversation_id = conversations.id
                                  AND cmd.kind IN ('start','steer','compact')
                            ),
                            created_at
                        )
                        WHERE activity_at IS NULL
                        """
                    )
                    self._db.execute(
                        """
                        UPDATE conversations
                        SET last_terminal_turn_id = (
                                SELECT t.id FROM turns t
                                WHERE t.conversation_id = conversations.id
                                  AND t.status IN ('completed','failed','interrupted')
                                  AND t.completed_at IS NOT NULL
                                ORDER BY t.ordinal DESC LIMIT 1
                            ),
                            last_terminal_at = (
                                SELECT t.completed_at FROM turns t
                                WHERE t.conversation_id = conversations.id
                                  AND t.status IN ('completed','failed','interrupted')
                                  AND t.completed_at IS NOT NULL
                                ORDER BY t.ordinal DESC LIMIT 1
                            )
                        """
                    )
                    self._db.execute(
                        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
                    self._finish(None)
                except BaseException as error:
                    self._finish(error)
                    raise
            elif existing["value"] != str(SCHEMA_VERSION):
                raise AppServerProtocolError("unsupported conversation database schema")
            # Web Push is a backward-compatible additive extension. Keep the
            # core marker at v6 so the last healthy release can still open the
            # database after an automatic source rollback.
            try:
                self._begin()
                # Read the extension marker only after acquiring the write
                # lock so two simultaneous cold starts cannot both decide to
                # insert it.
                extension = self._db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'web_push_schema_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(
                    WEB_PUSH_SCHEMA_VERSION
                ):
                    raise AppServerProtocolError(
                        "unsupported web push database schema"
                    )
                self._create_web_push_schema()
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) "
                        "VALUES('web_push_schema_version', ?)",
                        (str(WEB_PUSH_SCHEMA_VERSION),),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            os.chmod(self.path, 0o600)

    def _create_web_push_schema(self) -> None:
        """Install the additive Web Push schema; caller owns the transaction."""
        conversation_columns = {
            row["name"]
            for row in self._db.execute(
                "PRAGMA table_info(conversations)"
            ).fetchall()
        }
        if "owner_account_id" not in conversation_columns:
            self._db.execute(
                "ALTER TABLE conversations ADD COLUMN owner_account_id TEXT"
            )
        statements = (
            """CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE, origin TEXT NOT NULL,
                p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                disabled_at TEXT, last_error TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS web_push_deliveries (
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                subscription_id TEXT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
                state TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(turn_id, subscription_id)
            )""",
            """CREATE TABLE IF NOT EXISTS web_push_dispatches (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL, created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS web_push_subscriptions_account_idx ON web_push_subscriptions(account_id, enabled)",
            "CREATE INDEX IF NOT EXISTS web_push_deliveries_turn_idx ON web_push_deliveries(turn_id, state)",
        )
        for statement in statements:
            self._db.execute(statement)

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise AppServerProtocolError(f"invalid {label} ID")

    @staticmethod
    def _validate_state(value: str, choices: set[str], label: str) -> None:
        if value not in choices:
            raise AppServerProtocolError(f"invalid {label} state")

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def _finish(self, error: BaseException | None) -> None:
        self._db.execute("ROLLBACK" if error else "COMMIT")

    def _conversation_row(self, conversation_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise AppServerProtocolError("conversation does not exist")
        return row

    def create_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        cwd: str,
        settings: dict[str, Any],
        project_id: str | None = None,
        completion_notification: bool = False,
        client_origin: str | None = None,
        owner_account_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise AppServerProtocolError("conversation cwd must be absolute")
        if not isinstance(settings, dict):
            raise AppServerProtocolError("conversation settings must be an object")
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                self._db.execute(
                    """
                    INSERT INTO conversations(
                        id, title, cwd, project_id, state, settings_json,
                        connection_state, completion_notification, client_origin,
                        owner_account_id, created_at, updated_at, activity_at
                    ) VALUES(?, ?, ?, ?, 'new', ?, 'disconnected', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        str(title or "新对话")[:200],
                        os.path.normpath(cwd),
                        project_id,
                        _json(settings),
                        int(bool(completion_notification)),
                        client_origin,
                        str(owner_account_id or "").strip()[:200] or None,
                        now,
                        now,
                        now,
                    ),
                )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                if isinstance(error, sqlite3.IntegrityError):
                    raise AppServerProtocolError("conversation already exists") from error
                raise
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            return self._decode_conversation(self._conversation_row(conversation_id))

    def conversation_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        self._validate_id(thread_id, "thread")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return self._decode_conversation(row) if row is not None else None

    def conversation_for_turn(
        self, thread_id: str, turn_id: str
    ) -> dict[str, Any] | None:
        self._validate_id(thread_id, "thread")
        self._validate_id(turn_id, "turn")
        with self._lock:
            row = self._db.execute(
                """
                SELECT c.* FROM conversations c
                JOIN turns t ON t.conversation_id = c.id
                WHERE c.thread_id = ? AND t.id = ?
                """,
                (thread_id, turn_id),
            ).fetchone()
            return self._decode_conversation(row) if row is not None else None

    def list_conversations(
        self,
        limit: int = 100,
        *,
        project_id: str | None | object = ...,
        include_drafts: bool = False,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not ...:
            if project_id is None:
                clauses.append("project_id IS NULL")
            else:
                clauses.append("project_id = ?")
                values.append(project_id)
        if not include_drafts:
            clauses.append("thread_id IS NOT NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        account = str(account_id or "").strip()[:200]
        select = "SELECT conversations.*"
        if account:
            select += """,
                CASE WHEN conversations.terminal_generation > COALESCE((
                    SELECT seen_generation FROM conversation_reads
                    WHERE account_id = ?
                      AND conversation_id = conversations.id
                ), 0) THEN 1 ELSE 0 END AS unread
            """
            values.insert(0, account)
        else:
            select += ", 0 AS unread"
        values.append(limit)
        with self._lock:
            rows = self._db.execute(
                select + " FROM conversations " + where + """
                ORDER BY pinned DESC, activity_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [self._decode_conversation(row) for row in rows]

    def mark_read(self, conversation_id: str, account_id: str) -> None:
        self._validate_id(conversation_id, "conversation")
        account = str(account_id or "").strip()[:200]
        if not account:
            raise AppServerProtocolError("account ID must be non-empty")
        now = utc_now()
        with self._lock:
            conversation = self._conversation_row(conversation_id)
            self._db.execute(
                """
                INSERT INTO conversation_reads(
                    account_id, conversation_id, seen_generation, seen_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(account_id, conversation_id) DO UPDATE SET
                    seen_generation = MAX(
                        conversation_reads.seen_generation,
                        excluded.seen_generation
                    ),
                    seen_at = excluded.seen_at
                """,
                (
                    account,
                    conversation_id,
                    int(conversation["terminal_generation"] or 0),
                    now,
                ),
            )

    def has_active_conversations(self, project_ids: set[str]) -> bool:
        values = sorted(value for value in project_ids if isinstance(value, str))
        if not values:
            return False
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT 1 FROM conversations
                WHERE project_id IN ({placeholders})
                  AND state IN (
                      'starting_thread','starting_turn','active',
                      'interrupting','reconciling','needs_user'
                  )
                LIMIT 1
                """,
                values,
            ).fetchone()
            return row is not None

    def update_conversation_title(
        self, conversation_id: str, title: str
    ) -> dict[str, Any]:
        value = str(title or "").strip()[:200]
        if not value:
            raise AppServerProtocolError("conversation title must be non-empty")
        with self._lock:
            self._conversation_row(conversation_id)
            self._db.execute(
                """
                UPDATE conversations SET title = ?, updated_at = ?,
                    revision = revision + 1 WHERE id = ?
                """,
                (value, utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def set_pinned(self, conversation_ids: list[str], pinned: bool) -> None:
        values = list(dict.fromkeys(conversation_ids))
        for conversation_id in values:
            self._validate_id(conversation_id, "conversation")
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            self._db.execute(
                f"""
                UPDATE conversations SET pinned = ?, updated_at = ?,
                    revision = revision + 1 WHERE id IN ({placeholders})
                """,
                [int(bool(pinned)), utc_now(), *values],
            )

    def set_completion_notification(
        self, conversation_id: str, enabled: bool
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            self._conversation_row(conversation_id)
            self._db.execute(
                """
                UPDATE turns SET notification_requested = ?
                WHERE conversation_id = ?
                  AND id = (
                      SELECT active_turn_id FROM conversations WHERE id = ?
                  )
                  AND status NOT IN ('completed','failed','interrupted')
                  AND notification_state IS NULL
                """,
                (int(bool(enabled)), conversation_id, conversation_id),
            )
            self._db.execute(
                """
                UPDATE conversations SET completion_notification = ?,
                    updated_at = ?, revision = revision + 1 WHERE id = ?
                """,
                (int(bool(enabled)), utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def recoverable_conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM conversations
                WHERE state IN (
                    'starting_thread','starting_turn','active','interrupting',
                    'reconciling','needs_user'
                )
                   OR EXISTS (
                        SELECT 1 FROM commands
                        WHERE commands.conversation_id = conversations.id
                          AND commands.state IN (
                              'submitted','delivery_unknown','queued'
                          )
                   )
                ORDER BY updated_at ASC
                """
            ).fetchall()
            return [self._decode_conversation(row) for row in rows]

    def delete_conversation(self, conversation_id: str) -> None:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def claim_completion_notification(
        self, conversation_id: str, turn_id: str
    ) -> bool:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE turns SET notification_state = 'sending'
                WHERE id = ? AND conversation_id = ?
                  AND status IN ('completed','failed','interrupted')
                  AND notification_requested = 1
                  AND notification_state IS NULL
                """,
                (turn_id, conversation_id),
            )
            return cursor.rowcount == 1

    def release_incomplete_notifications(self) -> int:
        """Make a notification interrupted by process death eligible again."""
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE turns SET notification_state = NULL,
                    notification_detail = 'recovered_after_restart'
                WHERE notification_state = 'sending'
                """
            )
            return cursor.rowcount

    def pending_completion_notifications(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT t.conversation_id, t.id AS turn_id
                FROM turns t
                WHERE t.notification_requested = 1
                  AND t.status IN ('completed','failed','interrupted')
                  AND t.notification_state IS NULL
                ORDER BY t.completed_at ASC, t.ordinal ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def record_completion_notification(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        sent: bool,
        detail: str,
    ) -> None:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        with self._lock:
            self._db.execute(
                """
                UPDATE turns SET notification_state = ?, notification_detail = ?
                WHERE id = ? AND conversation_id = ?
                  AND notification_state = 'sending'
                """,
                (
                    "sent" if sent else "failed",
                    str(detail or "")[:200],
                    turn_id,
                    conversation_id,
                ),
            )

    def upsert_web_push_subscription(
        self,
        *,
        account_id: str,
        endpoint: str,
        origin: str,
        p256dh: str,
        auth: str,
    ) -> dict[str, Any]:
        """Store one browser subscription without allowing endpoint takeover."""
        account = str(account_id or "").strip()[:200]
        if not account:
            raise AppServerProtocolError("web push account is required")
        now = utc_now()
        with self._lock:
            existing = self._db.execute(
                "SELECT * FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
            ).fetchone()
            if existing is not None and existing["account_id"] != account:
                raise AppServerProtocolError("this browser subscription belongs to another account")
            if existing is None:
                subscription_id = "wp" + hashlib.sha256(
                    (endpoint + "\0" + now).encode("utf-8")
                ).hexdigest()[:40]
                self._db.execute(
                    """
                    INSERT INTO web_push_subscriptions(
                        id, account_id, endpoint, origin, p256dh, auth, enabled,
                        created_at, updated_at, disabled_at, last_error
                    ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL)
                    """,
                    (subscription_id, account, endpoint, origin, p256dh, auth, now, now),
                )
            else:
                subscription_id = existing["id"]
                self._db.execute(
                    """
                    UPDATE web_push_subscriptions
                    SET origin = ?, p256dh = ?, auth = ?, enabled = 1,
                        updated_at = ?, disabled_at = NULL, last_error = NULL
                    WHERE id = ?
                    """,
                    (origin, p256dh, auth, now, subscription_id),
                )
            row = self._db.execute(
                "SELECT * FROM web_push_subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
            return self._decode_web_push_subscription(row)

    def disable_web_push_subscription(self, *, account_id: str, endpoint: str) -> bool:
        account = str(account_id or "").strip()[:200]
        if not account:
            raise AppServerProtocolError("web push account is required")
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE web_push_subscriptions
                SET enabled = 0, disabled_at = ?, updated_at = ?
                WHERE endpoint = ? AND account_id = ? AND enabled = 1
                """,
                (utc_now(), utc_now(), endpoint, account),
            )
            return cursor.rowcount == 1

    def web_push_subscriptions_for_account(self, account_id: str) -> list[dict[str, Any]]:
        account = str(account_id or "").strip()[:200]
        if not account:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM web_push_subscriptions
                WHERE account_id = ? AND enabled = 1
                ORDER BY created_at ASC, id ASC
                """,
                (account,),
            ).fetchall()
            return [self._decode_web_push_subscription(row) for row in rows]

    def prepare_web_push_deliveries(
        self, conversation_id: str, turn_id: str, account_id: str
    ) -> list[dict[str, Any]]:
        """Atomically capture the first delivery set for one terminal turn.

        The dispatch marker is deliberately written even if no browser is
        subscribed.  A later subscription must never turn old completion
        history into a surprise notification.
        """
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        account = str(account_id or "").strip()[:200]
        if not account:
            return []
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                eligible = self._db.execute(
                    """
                    SELECT 1 FROM turns
                    WHERE id = ? AND conversation_id = ?
                      AND status IN ('completed','failed','interrupted')
                      AND notification_requested = 1
                    """,
                    (turn_id, conversation_id),
                ).fetchone()
                if eligible is None:
                    self._finish(None)
                    return []
                claimed = self._db.execute(
                    """
                    INSERT OR IGNORE INTO web_push_dispatches(
                        turn_id, conversation_id, account_id, created_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (turn_id, conversation_id, account, now),
                )
                if claimed.rowcount != 1:
                    self._finish(None)
                    return []
                subscriptions = self._db.execute(
                    """
                    SELECT * FROM web_push_subscriptions
                    WHERE account_id = ? AND enabled = 1
                    ORDER BY created_at ASC, id ASC
                    """,
                    (account,),
                ).fetchall()
                for subscription in subscriptions:
                    self._db.execute(
                        """
                        INSERT INTO web_push_deliveries(
                            conversation_id, turn_id, subscription_id, state, detail,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, 'pending', NULL, ?, ?)
                        """,
                        (conversation_id, turn_id, subscription["id"], now, now),
                    )
                rows = self._db.execute(
                    """
                    SELECT d.*, s.endpoint, s.origin, s.p256dh, s.auth
                    FROM web_push_deliveries d
                    JOIN web_push_subscriptions s ON s.id = d.subscription_id
                    WHERE d.turn_id = ? AND d.conversation_id = ? AND d.state = 'pending'
                    ORDER BY s.created_at ASC, s.id ASC
                    """,
                    (turn_id, conversation_id),
                ).fetchall()
                self._finish(None)
                return [dict(row) for row in rows]
            except BaseException as error:
                self._finish(error)
                raise

    def claim_web_push_delivery(self, turn_id: str, subscription_id: str) -> bool:
        self._validate_id(turn_id, "turn")
        self._validate_id(subscription_id, "web push subscription")
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE web_push_deliveries SET state = 'sending', updated_at = ?
                WHERE turn_id = ? AND subscription_id = ? AND state = 'pending'
                """,
                (utc_now(), turn_id, subscription_id),
            )
            return cursor.rowcount == 1

    def record_web_push_delivery(
        self,
        turn_id: str,
        subscription_id: str,
        *,
        state: str,
        detail: str = "",
    ) -> None:
        if state not in {"sent", "invalid", "unknown"}:
            raise AppServerProtocolError("invalid web push delivery state")
        self._validate_id(turn_id, "turn")
        self._validate_id(subscription_id, "web push subscription")
        now = utc_now()
        with self._lock:
            self._db.execute(
                """
                UPDATE web_push_deliveries
                SET state = ?, detail = ?, updated_at = ?
                WHERE turn_id = ? AND subscription_id = ? AND state = 'sending'
                """,
                (state, str(detail or "")[:160], now, turn_id, subscription_id),
            )
            if state == "invalid":
                self._db.execute(
                    """
                    UPDATE web_push_subscriptions
                    SET enabled = 0, disabled_at = ?, updated_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (now, now, str(detail or "invalid")[:160], subscription_id),
                )

    def release_incomplete_web_push_deliveries(self) -> int:
        """Never retry a send whose outcome was lost during process shutdown."""
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE web_push_deliveries SET state = 'unknown',
                    detail = 'recovered_after_restart', updated_at = ?
                WHERE state = 'sending'
                """,
                (utc_now(),),
            )
            return cursor.rowcount

    def set_conversation_state(
        self,
        conversation_id: str,
        state: str,
        *,
        connection_state: str | None = None,
        recovery_state: str | None = None,
        last_error: dict[str, Any] | None = None,
        active_turn_id: str | None | object = ...,
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        self._validate_state(state, CONVERSATION_STATES, "conversation")
        updates = ["state = ?", "updated_at = ?", "revision = revision + 1"]
        values: list[Any] = [state, utc_now()]
        if connection_state is not None:
            updates.append("connection_state = ?")
            values.append(connection_state)
        if recovery_state is not None:
            updates.append("recovery_state = ?")
            values.append(recovery_state)
        if last_error is not None:
            updates.append("last_error_json = ?")
            values.append(_json(last_error))
        if active_turn_id is not ...:
            if active_turn_id is not None:
                self._validate_id(active_turn_id, "turn")
            updates.append("active_turn_id = ?")
            values.append(active_turn_id)
        values.append(conversation_id)
        with self._lock:
            self._conversation_row(conversation_id)
            self._db.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?", values
            )
        return self.get_conversation(conversation_id)

    def bind_thread(
        self, conversation_id: str, thread: dict[str, Any], *, server_version: str
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        self._validate_id(thread_id, "thread")
        token_usage = None
        if isinstance(thread, dict) and "tokenUsage" in thread:
            token_usage = self._normalize_token_usage(thread["tokenUsage"], utc_now())
        with self._lock:
            row = self._conversation_row(conversation_id)
            if row["thread_id"] not in {None, thread_id}:
                raise AppServerProtocolError("conversation is bound to another thread")
            try:
                self._db.execute(
                    """
                    UPDATE conversations
                    SET thread_id = ?, server_version = ?, state = 'idle',
                        connection_state = 'connected', recovery_state = NULL,
                        token_usage_json = COALESCE(?, token_usage_json),
                        updated_at = ?, revision = revision + 1
                    WHERE id = ?
                    """,
                    (
                        thread_id,
                        server_version,
                        _json(token_usage) if token_usage is not None else None,
                        utc_now(),
                        conversation_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise AppServerProtocolError("thread belongs to another conversation") from error
        return self.get_conversation(conversation_id)

    def update_settings(
        self, conversation_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise AppServerProtocolError("settings update must be an object")
        with self._lock:
            row = self._conversation_row(conversation_id)
            settings = _object(row["settings_json"], {})
            if not isinstance(settings, dict):
                settings = {}
            settings.update(updates)
            self._db.execute(
                """
                UPDATE conversations SET settings_json = ?, updated_at = ?,
                    revision = revision + 1 WHERE id = ?
                """,
                (_json(settings), utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def begin_command(
        self,
        conversation_id: str,
        command_id: str,
        *,
        kind: str,
        text: str | None = None,
        payload: dict[str, Any] | None = None,
        target_turn_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(command_id, "command")
        if target_turn_id is not None:
            self._validate_id(target_turn_id, "turn")
        if kind not in {"start", "steer", "interrupt", "settings", "compact"}:
            raise AppServerProtocolError("unsupported conversation command")
        payload_json = _json(payload or {})
        now = utc_now()
        with self._lock:
            self._conversation_row(conversation_id)
            existing = self._db.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["conversation_id"] != conversation_id
                    or existing["kind"] != kind
                    or existing["text"] != text
                    or existing["payload_json"] != payload_json
                ):
                    raise AppServerProtocolError("command ID was reused with new input")
                return self._decode_command(existing), False
            self._db.execute(
                """
                INSERT INTO commands(
                    id, conversation_id, kind, state, target_turn_id, text,
                    payload_json, created_at, updated_at
                ) VALUES(?, ?, ?, 'submitted', ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    conversation_id,
                    kind,
                    target_turn_id,
                    text,
                    payload_json,
                    now,
                    now,
                ),
            )
            if kind in {"start", "steer", "compact"}:
                self._db.execute(
                    "UPDATE conversations SET activity_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return self.command(command_id), True

    def command(self, command_id: str) -> dict[str, Any]:
        self._validate_id(command_id, "command")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("command does not exist")
            return self._decode_command(row)

    def update_command(
        self,
        command_id: str,
        state: str,
        *,
        result_turn_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._validate_id(command_id, "command")
        self._validate_state(state, COMMAND_STATES, "command")
        if result_turn_id is not None:
            self._validate_id(result_turn_id, "turn")
        with self._lock:
            current = self._db.execute(
                "SELECT state, result_turn_id FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if current is None:
                raise AppServerProtocolError("command does not exist")
            if current["state"] == "completed" and state != "completed":
                return self.command(command_id)
            effective_state = state
            effective_turn_id = result_turn_id or current["result_turn_id"]
            if state == "accepted" and effective_turn_id is not None:
                turn = self._db.execute(
                    "SELECT status, completed_at FROM turns WHERE id = ?",
                    (effective_turn_id,),
                ).fetchone()
                if (
                    turn is not None
                    and turn["status"] in TERMINAL_TURN_STATUSES
                    and turn["completed_at"] is not None
                ):
                    effective_state = "completed"
            self._db.execute(
                """
                UPDATE commands SET state = ?, result_turn_id = COALESCE(?, result_turn_id),
                    reason = ?, updated_at = ? WHERE id = ?
                """,
                (effective_state, result_turn_id, reason, utc_now(), command_id),
            )
        return self.command(command_id)

    def queued_commands(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM commands
                WHERE conversation_id = ? AND state = 'queued'
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
            return [self._decode_command(row) for row in rows]

    def commands_with_state(
        self, conversation_id: str, state: str
    ) -> list[dict[str, Any]]:
        self._validate_id(conversation_id, "conversation")
        self._validate_state(state, COMMAND_STATES, "command")
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM commands
                WHERE conversation_id = ? AND state = ?
                ORDER BY created_at ASC
                """,
                (conversation_id, state),
            ).fetchall()
            return [self._decode_command(row) for row in rows]

    def bind_turn(
        self,
        conversation_id: str,
        turn: dict[str, Any],
        *,
        command_id: str,
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(command_id, "command")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        self._validate_id(turn_id, "turn")
        status = turn.get("status") or "inProgress"
        if status not in ACTIVE_TURN_STATUSES | TERMINAL_TURN_STATUSES:
            raise AppServerProtocolError("turn has an invalid status")
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                conversation = self._conversation_row(conversation_id)
                command = self._db.execute(
                    "SELECT * FROM commands WHERE id = ?", (command_id,)
                ).fetchone()
                if command is None or command["conversation_id"] != conversation_id:
                    raise AppServerProtocolError("turn command does not belong to conversation")
                existing = self._db.execute(
                    """
                    SELECT conversation_id, status, completed_at
                    FROM turns WHERE id = ?
                    """,
                    (turn_id,),
                ).fetchone()
                if existing is not None and existing["conversation_id"] != conversation_id:
                    raise AppServerProtocolError("turn belongs to another conversation")
                if existing is None:
                    ordinal = self._db.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM turns WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()[0]
                    turn_kind = "compact" if command["kind"] == "compact" else "model"
                    self._db.execute(
                        """
                        INSERT INTO turns(
                            id, conversation_id, ordinal, status, kind, started_at,
                            notification_requested
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            turn_id,
                            conversation_id,
                            ordinal,
                            status,
                            turn_kind,
                            now,
                            int(bool(conversation["completion_notification"])),
                        ),
                    )
                already_terminal = bool(
                    existing is not None
                    and existing["status"] in TERMINAL_TURN_STATUSES
                    and existing["completed_at"] is not None
                )
                self._db.execute(
                    """
                    UPDATE commands SET state = ?, result_turn_id = ?,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        "completed" if already_terminal else "accepted",
                        turn_id,
                        now,
                        command_id,
                    ),
                )
                if not already_terminal:
                    next_state = "active" if status in ACTIVE_TURN_STATUSES else "idle"
                    self._db.execute(
                        """
                        UPDATE conversations SET state = ?, active_turn_id = ?,
                            connection_state = 'connected', recovery_state = NULL,
                            updated_at = ?, activity_at = ?,
                            revision = revision + 1 WHERE id = ?
                        """,
                        (
                            next_state,
                            turn_id if next_state == "active" else None,
                            now,
                            now,
                            conversation_id,
                        ),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
        return self.snapshot(conversation_id)

    def _record_terminal_locked(
        self, conversation_id: str, turn_id: str
    ) -> bool:
        terminal = self._db.execute(
            """
            SELECT ordinal, status, completed_at FROM turns
            WHERE id = ? AND conversation_id = ?
            """,
            (turn_id, conversation_id),
        ).fetchone()
        if (
            terminal is None
            or terminal["status"] not in TERMINAL_TURN_STATUSES
            or terminal["completed_at"] is None
        ):
            return False
        current = self._db.execute(
            """
            SELECT t.ordinal FROM conversations c
            LEFT JOIN turns t ON t.id = c.last_terminal_turn_id
            WHERE c.id = ?
            """,
            (conversation_id,),
        ).fetchone()
        current_ordinal = current["ordinal"] if current is not None else None
        if current_ordinal is not None and int(current_ordinal) >= int(terminal["ordinal"]):
            return False
        self._db.execute(
            """
            UPDATE conversations
            SET last_terminal_turn_id = ?, last_terminal_at = ?,
                terminal_generation = terminal_generation + 1,
                activity_at = ?
            WHERE id = ?
            """,
            (
                turn_id,
                terminal["completed_at"],
                terminal["completed_at"],
                conversation_id,
            ),
        )
        return True

    def apply_notification(self, message: dict[str, Any]) -> dict[str, Any]:
        """Apply one ordered public notification to an exact Thread/Turn."""
        if not isinstance(message, dict):
            raise AppServerProtocolError("notification must be an object")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return {"changed": False}
        thread_id, turn_id = self._notification_ids(method, params)
        if thread_id is None:
            return {"changed": False}
        with self._lock:
            conversation = self._db.execute(
                "SELECT * FROM conversations WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if conversation is None:
                return {"changed": False}
            conversation_id = conversation["id"]
            if method == "turn/started" and turn_id is not None:
                exists = self._db.execute(
                    "SELECT 1 FROM turns WHERE id = ? AND conversation_id = ?",
                    (turn_id, conversation_id),
                ).fetchone()
                if exists is None:
                    return {
                        "changed": False,
                        "conversation_id": conversation_id,
                        "unbound_turn": params.get("turn"),
                    }
            if turn_id is not None:
                owner = self._db.execute(
                    """
                    SELECT conversation_id, status, completed_at
                    FROM turns WHERE id = ?
                    """,
                    (turn_id,),
                ).fetchone()
                if owner is None or owner["conversation_id"] != conversation_id:
                    return {"changed": False, "conversation_id": conversation_id}
                # A terminal notification is idempotent.  Once its transaction
                # recorded completed_at, a replay must not settle a newer turn.
                if owner["status"] in TERMINAL_TURN_STATUSES and (
                    method not in {"turn/completed", "thread/tokenUsage/updated"}
                    or (
                        method == "turn/completed"
                        and owner["completed_at"] is not None
                    )
                ):
                    return {"changed": False, "conversation_id": conversation_id}
            try:
                self._begin()
                result = self._apply_notification_locked(
                    conversation_id, method, params, turn_id
                )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
        result.setdefault("conversation_id", conversation_id)
        return result

    def _apply_notification_locked(
        self,
        conversation_id: str,
        method: str,
        params: dict[str, Any],
        turn_id: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        changed = False
        artifact_candidates: list[dict[str, str]] = []
        terminal = False
        if method == "thread/settings/updated":
            settings_value = params.get("threadSettings")
            if isinstance(settings_value, dict) and "serviceTier" in settings_value:
                row = self._conversation_row(conversation_id)
                settings = _object(row["settings_json"], {})
                settings["service_tier"] = settings_value.get("serviceTier")
                self._db.execute(
                    "UPDATE conversations SET settings_json = ? WHERE id = ?",
                    (_json(settings), conversation_id),
                )
                changed = True
        elif method == "thread/tokenUsage/updated":
            token_usage = self._normalize_token_usage(
                params.get("tokenUsage"), now
            )
            # Usage is optional display telemetry.  A partial/newer wire shape
            # must never make message delivery, recovery, or reconciliation
            # fail; retain the last valid sample instead.
            if token_usage is None:
                return {
                    "changed": False,
                    "terminal": False,
                    "turn_id": turn_id,
                    "artifact_candidates": artifact_candidates,
                }
            row = self._conversation_row(conversation_id)
            encoded = _json(token_usage)
            if row["token_usage_json"] != encoded:
                self._db.execute(
                    "UPDATE conversations SET token_usage_json = ? WHERE id = ?",
                    (encoded, conversation_id),
                )
                changed = True
        elif method == "thread/status/changed":
            status = params.get("status")
            self._db.execute(
                "UPDATE conversations SET thread_status = ? WHERE id = ?",
                (_json(status), conversation_id),
            )
            changed = True
        elif method == "turn/started" and turn_id is not None:
            self._db.execute(
                "UPDATE turns SET status = 'inProgress', started_at = COALESCE(started_at, ?) WHERE id = ?",
                (now, turn_id),
            )
            self._db.execute(
                """
                UPDATE conversations
                SET state = 'active', active_turn_id = ?, activity_at = ?
                WHERE id = ?
                """,
                (turn_id, now, conversation_id),
            )
            changed = True
        elif method in {"item/started", "item/completed"} and turn_id is not None:
            item = params.get("item")
            if isinstance(item, dict):
                changed = self._upsert_item_locked(
                    conversation_id,
                    turn_id,
                    item,
                    completed=method == "item/completed",
                    source="notification_item",
                    now=now,
                )
                if item.get("type") == "contextCompaction":
                    changed = self._record_context_compaction_locked(
                        conversation_id,
                        turn_id,
                        item,
                        completed=method == "item/completed",
                        now=now,
                    ) or changed
                if method == "item/completed" and item.get("type") == "imageGeneration":
                    saved_path = item.get("savedPath")
                    item_id = item.get("id")
                    if isinstance(saved_path, str) and isinstance(item_id, str):
                        artifact_candidates.append(
                            {"path": saved_path, "native_item_id": item_id}
                        )
        elif method in {
            "item/agentMessage/delta",
            "item/plan/delta",
            "item/reasoning/summaryTextDelta",
            "item/commandExecution/outputDelta",
        } and turn_id is not None:
            item_id = params.get("itemId")
            delta = params.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                kind, field = {
                    "item/agentMessage/delta": ("agent_message", "text"),
                    "item/plan/delta": ("plan", "text"),
                    "item/reasoning/summaryTextDelta": ("reasoning_summary", "text"),
                    "item/commandExecution/outputDelta": ("command_execution", "detail"),
                }[method]
                changed = self._append_delta_locked(
                    conversation_id,
                    turn_id,
                    item_id,
                    kind,
                    field,
                    delta,
                    source="notification_delta",
                    now=now,
                )
        elif method == "turn/plan/updated" and turn_id is not None:
            plan = params.get("plan")
            if isinstance(plan, list):
                text = "\n".join(
                    f"{str(step.get('status') or 'pending')}: {str(step.get('step') or step.get('text') or '')}"
                    for step in plan
                    if isinstance(step, dict)
                )[:_VISIBLE_TEXT_LIMIT]
                changed = self._upsert_projection_locked(
                    conversation_id,
                    turn_id,
                    "turn-plan",
                    "plan",
                    status="inProgress",
                    text=text,
                    detail=None,
                    phase=None,
                    source="notification_item",
                    now=now,
                )
        elif method == "turn/completed" and turn_id is not None:
            turn = params.get("turn")
            if isinstance(turn, dict):
                native_status = turn.get("status")
                if native_status in TERMINAL_TURN_STATUSES:
                    for item in turn.get("items") or []:
                        if isinstance(item, dict):
                            self._upsert_item_locked(
                                conversation_id,
                                turn_id,
                                item,
                                completed=True,
                                source="notification_item",
                                now=now,
                            )
                            if item.get("type") == "imageGeneration":
                                saved_path = item.get("savedPath")
                                item_id = item.get("id")
                                if isinstance(saved_path, str) and isinstance(item_id, str):
                                    artifact_candidates.append(
                                        {"path": saved_path, "native_item_id": item_id}
                                    )
                    final_text = self._final_text_locked(turn_id)
                    error = turn.get("error") if isinstance(turn.get("error"), dict) else None
                    self._db.execute(
                        """
                        UPDATE turns SET status = ?, completed_at = ?, final_text = ?,
                            error_json = ? WHERE id = ?
                        """,
                        (
                            native_status,
                            now,
                            final_text,
                            _json(error) if error else None,
                            turn_id,
                        ),
                    )
                    self._db.execute(
                        """
                        UPDATE commands SET state = 'completed', updated_at = ?
                        WHERE conversation_id = ? AND result_turn_id = ?
                          AND state = 'accepted'
                        """,
                        (now, conversation_id, turn_id),
                    )
                    current = self._conversation_row(conversation_id)
                    if current["active_turn_id"] == turn_id or (
                        current["active_turn_id"] is None
                        and current["state"] != "active"
                    ):
                        self._db.execute(
                            """
                            UPDATE conversations SET state = 'idle',
                                active_turn_id = NULL, recovery_state = NULL,
                                connection_state = 'connected'
                            WHERE id = ?
                            """,
                            (conversation_id,),
                        )
                    changed = True
                    terminal = self._record_terminal_locked(
                        conversation_id, turn_id
                    )
        if changed:
            self._db.execute(
                """
                UPDATE conversations SET updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (now, conversation_id),
            )
        return {
            "changed": changed,
            "terminal": terminal,
            "turn_id": turn_id,
            "artifact_candidates": artifact_candidates,
        }

    def _record_context_compaction_locked(
        self,
        conversation_id: str,
        turn_id: str,
        item: dict[str, Any],
        *,
        completed: bool,
        now: str,
    ) -> bool:
        """Persist only live observed context-compaction lifecycle events."""
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
            return False
        previous = self._db.execute(
            """
            SELECT started_observed_at, completed_observed_at
            FROM context_compaction_events
            WHERE conversation_id = ? AND turn_id = ? AND native_item_id = ?
            """,
            (conversation_id, turn_id, item_id),
        ).fetchone()
        column = "completed_observed_at" if completed else "started_observed_at"
        if previous is not None and previous[column] is not None:
            return False
        self._db.execute(
            f"""
            INSERT INTO context_compaction_events(
                conversation_id, turn_id, native_item_id, {column}
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(conversation_id, turn_id, native_item_id) DO UPDATE SET
                {column} = COALESCE(
                    context_compaction_events.{column}, excluded.{column}
                )
            """,
            (conversation_id, turn_id, item_id, now),
        )
        return True

    @staticmethod
    def _notification_ids(
        method: str, params: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(thread_id, str):
            return None, None
        return thread_id, turn_id if isinstance(turn_id, str) else None

    @staticmethod
    def _normalize_token_usage(
        value: Any, observed_at: str
    ) -> dict[str, Any] | None:
        """Normalize optional App Server usage without gating core recovery."""
        if not isinstance(value, dict):
            return None

        counters = (
            "inputTokens",
            "cachedInputTokens",
            "outputTokens",
            "reasoningOutputTokens",
            "totalTokens",
        )
        result: dict[str, Any] = {}
        for section in ("last", "total"):
            usage = value.get(section)
            if not isinstance(usage, dict):
                return None
            normalized: dict[str, int] = {}
            for name in counters:
                token_count = usage.get(name)
                if (
                    isinstance(token_count, bool)
                    or not isinstance(token_count, int)
                    or token_count < 0
                ):
                    return None
                normalized[name] = token_count
            cache_write = usage.get("cacheWriteInputTokens")
            if cache_write is not None:
                if (
                    isinstance(cache_write, bool)
                    or not isinstance(cache_write, int)
                    or cache_write < 0
                ):
                    return None
                normalized["cacheWriteInputTokens"] = cache_write
            result[section] = normalized

        window = value.get("modelContextWindow")
        if window is not None and (
            isinstance(window, bool) or not isinstance(window, int) or window <= 0
        ):
            return None
        result["modelContextWindow"] = window
        updated_at = value.get("updatedAt", observed_at)
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = observed_at
        try:
            datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated_at = observed_at
        result["updatedAt"] = updated_at
        return result

    def _upsert_item_locked(
        self,
        conversation_id: str,
        turn_id: str,
        item: dict[str, Any],
        *,
        completed: bool,
        source: str,
        now: str,
    ) -> bool:
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return False
        kind = {
            "agentMessage": "agent_message",
            "plan": "plan",
            "reasoning": "reasoning_summary",
            "commandExecution": "command_execution",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
            "dynamicToolCall": "dynamic_tool_call",
            "webSearch": "web_search",
            "imageView": "image_view",
            "imageGeneration": "image_generation",
            "contextCompaction": "context_compaction",
        }.get(str(item.get("type")), "activity")
        status = item.get("status") if isinstance(item.get("status"), str) else (
            "completed" if completed else "inProgress"
        )
        phase = item.get("phase") if isinstance(item.get("phase"), str) else None
        text = None
        detail = None
        if item.get("type") == "agentMessage":
            text = str(item.get("text") or "")[:_VISIBLE_TEXT_LIMIT]
        elif item.get("type") == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(
                    entry if isinstance(entry, str) else str(entry.get("text") or "")
                    for entry in summary
                    if isinstance(entry, (str, dict))
                )[:_VISIBLE_TEXT_LIMIT]
            elif isinstance(summary, str):
                text = summary[:_VISIBLE_TEXT_LIMIT]
        elif item.get("type") == "plan":
            text = str(item.get("text") or "")[:_VISIBLE_TEXT_LIMIT]
        elif item.get("type") == "commandExecution":
            detail = str(
                item.get("aggregatedOutput") or item.get("output") or ""
            )[:_VISIBLE_DETAIL_LIMIT]
        elif item.get("type") == "imageGeneration":
            detail = str(item.get("status") or "")[:_VISIBLE_DETAIL_LIMIT]
        return self._upsert_projection_locked(
            conversation_id,
            turn_id,
            item_id,
            kind,
            status=status,
            phase=phase,
            text=text,
            detail=detail,
            source=source,
            now=now,
        )

    def _upsert_projection_locked(
        self,
        conversation_id: str,
        turn_id: str,
        item_id: str,
        kind: str,
        *,
        status: str,
        phase: str | None,
        text: str | None,
        detail: str | None,
        source: str,
        now: str,
    ) -> bool:
        row = self._db.execute(
            """
            SELECT * FROM items
            WHERE turn_id = ? AND native_item_id = ? AND kind = ?
            """,
            (turn_id, item_id, kind),
        ).fetchone()
        if row is None:
            self._db.execute(
                """
                INSERT INTO items(
                    conversation_id, turn_id, native_item_id, kind, status,
                    phase, text, detail, source, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    turn_id,
                    item_id,
                    kind,
                    status,
                    phase,
                    text,
                    detail,
                    source,
                    now,
                    now,
                ),
            )
            return True
        values = (status, phase, text, detail, source)
        previous = (
            row["status"], row["phase"], row["text"], row["detail"],
            row["source"],
        )
        if values == previous:
            return False
        self._db.execute(
            """
            UPDATE items SET status = ?, phase = ?, text = ?, detail = ?, source = ?,
                revision = revision + 1, updated_at = ? WHERE row_id = ?
            """,
            (*values, now, row["row_id"]),
        )
        return True

    def _append_delta_locked(
        self,
        conversation_id: str,
        turn_id: str,
        item_id: str,
        kind: str,
        field: str,
        delta: str,
        *,
        source: str,
        now: str,
    ) -> bool:
        row = self._db.execute(
            """
            SELECT * FROM items
            WHERE turn_id = ? AND native_item_id = ? AND kind = ?
            """,
            (turn_id, item_id, kind),
        ).fetchone()
        if row is None:
            return self._upsert_projection_locked(
                conversation_id,
                turn_id,
                item_id,
                kind,
                status="inProgress",
                phase=None,
                text=delta[:_VISIBLE_TEXT_LIMIT] if field == "text" else None,
                detail=delta[:_VISIBLE_DETAIL_LIMIT] if field == "detail" else None,
                source=source,
                now=now,
            )
        maximum = _VISIBLE_TEXT_LIMIT if field == "text" else _VISIBLE_DETAIL_LIMIT
        value = (str(row[field] or "") + delta)[-maximum:]
        self._db.execute(
            f"""
            UPDATE items SET {field} = ?, source = ?, revision = revision + 1,
                updated_at = ? WHERE row_id = ?
            """,
            (value, source, now, row["row_id"]),
        )
        return True

    def _final_text_locked(self, turn_id: str) -> str | None:
        rows = self._db.execute(
            """
            SELECT phase, text FROM items
            WHERE turn_id = ? AND kind = 'agent_message'
            ORDER BY row_id ASC
            """,
            (turn_id,),
        ).fetchall()
        finals = [str(row["text"] or "").strip() for row in rows if row["phase"] == "final_answer"]
        if finals:
            return finals[-1]
        compatible = [str(row["text"] or "").strip() for row in rows if row["phase"] is None]
        return compatible[-1] if compatible else None

    def reconcile_thread(
        self, conversation_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Project one authoritative thread/read response without replaying work."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("thread"), dict):
            raise AppServerProtocolError("thread/read response is malformed")
        thread = snapshot["thread"]
        thread_id = thread.get("id")
        self._validate_id(thread_id, "thread")
        now = utc_now()
        token_usage = (
            self._normalize_token_usage(thread["tokenUsage"], now)
            if "tokenUsage" in thread
            else None
        )
        with self._lock:
            try:
                self._begin()
                conversation = self._conversation_row(conversation_id)
                if conversation["thread_id"] != thread_id:
                    raise AppServerProtocolError("thread/read returned another conversation")
                turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
                active: list[str] = []
                unknown_native_turn_status = False
                for ordinal, turn in enumerate(turns, 1):
                    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                        continue
                    turn_id = turn["id"]
                    self._validate_id(turn_id, "turn")
                    status = turn.get("status") if isinstance(turn.get("status"), str) else "unknown"
                    existing = self._db.execute(
                        """
                        SELECT conversation_id, status, completed_at
                        FROM turns WHERE id = ?
                        """,
                        (turn_id,),
                    ).fetchone()
                    if existing is not None and existing["conversation_id"] != conversation_id:
                        raise AppServerProtocolError("native turn belongs to another conversation")
                    self._db.execute(
                        """
                        INSERT INTO turns(
                            id, conversation_id, ordinal, status, started_at,
                            notification_requested
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET status = excluded.status
                        """,
                        (
                            turn_id,
                            conversation_id,
                            ordinal,
                            status,
                            now,
                            int(bool(conversation["completion_notification"])),
                        ),
                    )
                    for item in turn.get("items") or []:
                        if isinstance(item, dict):
                            self._upsert_item_locked(
                                conversation_id,
                                turn_id,
                                item,
                                completed=status in TERMINAL_TURN_STATUSES,
                                source="thread_read",
                                now=now,
                            )
                    if status in TERMINAL_TURN_STATUSES:
                        error = turn.get("error") if isinstance(turn.get("error"), dict) else None
                        self._db.execute(
                            """
                            UPDATE turns SET completed_at = COALESCE(completed_at, ?),
                                final_text = ?, error_json = ? WHERE id = ?
                            """,
                            (
                                now,
                                self._final_text_locked(turn_id),
                                _json(error) if error else None,
                                turn_id,
                            ),
                        )
                        self._db.execute(
                            """
                            UPDATE commands SET state = 'completed', updated_at = ?
                            WHERE conversation_id = ? AND result_turn_id = ?
                              AND state = 'accepted'
                            """,
                            (now, conversation_id, turn_id),
                        )
                        if not (
                            existing is not None
                            and existing["status"] in TERMINAL_TURN_STATUSES
                            and existing["completed_at"] is not None
                        ):
                            self._record_terminal_locked(
                                conversation_id, turn_id
                            )
                    elif status in ACTIVE_TURN_STATUSES:
                        active.append(turn_id)
                    else:
                        # A newly introduced native status cannot safely be
                        # interpreted as idle.  Keep the conversation gated
                        # until a user-triggered reconcile can be reviewed.
                        unknown_native_turn_status = True
                unknown = self._db.execute(
                    """
                    SELECT 1 FROM commands
                    WHERE conversation_id = ? AND state = 'delivery_unknown' LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if unknown_native_turn_status:
                    state = "needs_user"
                    active_turn_id = None
                    recovery_state = "unknown_native_turn_status"
                elif len(active) == 1:
                    state = "active"
                    active_turn_id = active[0]
                    recovery_state = "rejoined"
                elif not active and unknown is None:
                    state = "idle"
                    active_turn_id = None
                    recovery_state = "reconciled"
                else:
                    state = "needs_user"
                    active_turn_id = active[0] if len(active) == 1 else None
                    recovery_state = "ambiguous_native_state"
                self._db.execute(
                    """
                    UPDATE conversations SET state = ?, active_turn_id = ?,
                        thread_status = ?, recovery_state = ?,
                        connection_state = 'connected',
                        token_usage_json = COALESCE(?, token_usage_json),
                        updated_at = ?,
                        revision = revision + 1 WHERE id = ?
                    """,
                    (
                        state,
                        active_turn_id,
                        _json(thread.get("status")),
                        recovery_state,
                        _json(token_usage) if token_usage is not None else None,
                        now,
                        conversation_id,
                    ),
                )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
        return self.snapshot(conversation_id)

    def claim_tool_call(
        self,
        conversation_id: str,
        turn_id: str,
        call_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        for value, label in (
            (conversation_id, "conversation"),
            (turn_id, "turn"),
            (call_id, "tool call"),
        ):
            self._validate_id(value, label)
        digest = hashlib.sha256(_json(arguments).encode("utf-8")).hexdigest()
        now = utc_now()
        with self._lock:
            owner = self._db.execute(
                "SELECT conversation_id, status FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if owner is None or owner["conversation_id"] != conversation_id:
                raise AppServerProtocolError("tool call turn is not owned by conversation")
            if owner["status"] in TERMINAL_TURN_STATUSES:
                raise AppServerProtocolError("tool call turn is terminal")
            existing = self._db.execute(
                "SELECT * FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["conversation_id"] != conversation_id
                    or existing["turn_id"] != turn_id
                    or existing["tool"] != tool
                    or existing["arguments_sha256"] != digest
                ):
                    raise AppServerProtocolError("tool call ID was reused")
                if existing["state"] == "completed":
                    return _object(existing["response_json"], {})
                return None
            self._db.execute(
                """
                INSERT INTO tool_calls(
                    call_id, conversation_id, turn_id, tool, arguments_sha256,
                    state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'inProgress', ?, ?)
                """,
                (call_id, conversation_id, turn_id, tool, digest, now, now),
            )
        return None

    def complete_tool_call(self, call_id: str, response: dict[str, Any]) -> dict[str, Any]:
        self._validate_id(call_id, "tool call")
        with self._lock:
            row = self._db.execute(
                "SELECT response_json, state FROM tool_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("tool call is not claimed")
            if row["state"] == "completed":
                return _object(row["response_json"], {})
            self._db.execute(
                """
                UPDATE tool_calls SET state = 'completed', response_json = ?,
                    updated_at = ? WHERE call_id = ?
                """,
                (_json(response), utc_now(), call_id),
            )
        return response

    def insert_artifact(
        self,
        record: dict[str, Any],
        *,
        max_count: int,
        max_total_bytes: int,
    ) -> dict[str, Any]:
        required = {
            "id",
            "conversation_id",
            "idempotency_key",
            "name",
            "mime_type",
            "size",
            "sha256",
            "storage_relpath",
            "preview_kind",
            "origin",
        }
        if not isinstance(record, dict) or not required.issubset(record):
            raise AppServerProtocolError("artifact record is incomplete")
        self._validate_id(record["id"], "artifact")
        self._validate_id(record["conversation_id"], "conversation")
        if record.get("turn_id") is not None:
            self._validate_id(record["turn_id"], "turn")
        size = int(record["size"])
        if size < 0 or max_count < 1 or max_total_bytes < 1:
            raise AppServerProtocolError("artifact quota inputs are invalid")
        with self._lock:
            try:
                self._begin()
                self._conversation_row(record["conversation_id"])
                existing = self._db.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE conversation_id = ? AND idempotency_key = ?
                    """,
                    (record["conversation_id"], record["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    result = self._decode_artifact(existing)
                    self._finish(None)
                    return result
                usage = self._db.execute(
                    """
                    SELECT COUNT(*) AS artifact_count,
                           COALESCE(SUM(size), 0) AS total_bytes
                    FROM artifacts WHERE conversation_id = ?
                    """,
                    (record["conversation_id"],),
                ).fetchone()
                if (
                    int(usage["artifact_count"]) >= int(max_count)
                    or int(usage["total_bytes"]) + size > int(max_total_bytes)
                ):
                    raise AppServerProtocolError(
                        "conversation artifact quota exceeded"
                    )
                now = record.get("created_at") or utc_now()
                self._db.execute(
                    """
                    INSERT INTO artifacts(
                        id, conversation_id, turn_id, native_item_id,
                        idempotency_key, name, mime_type, size, sha256,
                        storage_relpath, preview_kind, origin, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["conversation_id"],
                        record.get("turn_id"),
                        record.get("native_item_id"),
                        record["idempotency_key"],
                        record["name"],
                        record["mime_type"],
                        size,
                        record["sha256"],
                        record["storage_relpath"],
                        record["preview_kind"],
                        record["origin"],
                        now,
                    ),
                )
                self._db.execute(
                    """
                    UPDATE conversations SET updated_at = ?, revision = revision + 1
                    WHERE id = ?
                    """,
                    (now, record["conversation_id"]),
                )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
        return self.artifact(record["id"])

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        self._validate_id(artifact_id, "artifact")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("artifact does not exist")
            return self._decode_artifact(row)

    def artifact_for_key(
        self, conversation_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        self._validate_id(conversation_id, "conversation")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise AppServerProtocolError("artifact idempotency key is invalid")
        with self._lock:
            row = self._db.execute(
                """
                SELECT * FROM artifacts
                WHERE conversation_id = ? AND idempotency_key = ?
                """,
                (conversation_id, idempotency_key),
            ).fetchone()
            return self._decode_artifact(row) if row is not None else None

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            conversation = self._decode_conversation(
                self._conversation_row(conversation_id)
            )
            turns = self._db.execute(
                "SELECT * FROM turns WHERE conversation_id = ? ORDER BY ordinal ASC",
                (conversation_id,),
            ).fetchall()
            commands = self._db.execute(
                "SELECT * FROM commands WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
            items = self._db.execute(
                "SELECT * FROM items WHERE conversation_id = ? ORDER BY row_id ASC",
                (conversation_id,),
            ).fetchall()
            artifacts = self._db.execute(
                "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
            tool_calls = self._db.execute(
                """
                SELECT turn_id, tool, response_json FROM tool_calls
                WHERE conversation_id = ? AND state = 'completed'
                """,
                (conversation_id,),
            ).fetchall()
            compaction_count = int(self._db.execute(
                """
                SELECT COUNT(*) FROM context_compaction_events
                WHERE conversation_id = ? AND completed_observed_at IS NOT NULL
                """,
                (conversation_id,),
            ).fetchone()[0])
            latest_compaction = self._db.execute(
                """
                SELECT state FROM commands
                WHERE conversation_id = ? AND kind = 'compact'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            compaction_state = "idle"
            if latest_compaction is not None:
                if latest_compaction["state"] in {"submitted", "queued", "accepted"}:
                    compaction_state = "busy"
                elif latest_compaction["state"] in {"rejected", "delivery_unknown"}:
                    compaction_state = "failed"
            conversation["context_compaction_completed_count"] = compaction_count
            conversation["context_compaction_state"] = compaction_state
        turn_values = [self._decode_turn(row) for row in turns]
        by_turn_items: dict[str, list[dict[str, Any]]] = {}
        for row in items:
            by_turn_items.setdefault(row["turn_id"], []).append(self._decode_item(row))
        by_turn_commands: dict[str, list[dict[str, Any]]] = {}
        queue: list[dict[str, Any]] = []
        for row in commands:
            command = self._decode_command(row)
            turn_id = command.get("result_turn_id") or command.get("target_turn_id")
            if turn_id:
                by_turn_commands.setdefault(turn_id, []).append(command)
            elif command["state"] == "queued":
                queue.append(command)
        by_turn_artifacts: dict[str, list[dict[str, Any]]] = {}
        detached_artifacts: list[dict[str, Any]] = []
        for row in artifacts:
            artifact = self._decode_artifact(row)
            if artifact.get("turn_id"):
                by_turn_artifacts.setdefault(artifact["turn_id"], []).append(artifact)
            else:
                detached_artifacts.append(artifact)
        artifact_publish_failed_turns = set()
        for row in tool_calls:
            response = _object(row["response_json"], {})
            if (
                row["tool"] == "publish_artifact"
                and isinstance(response, dict)
                and response.get("success") is False
            ):
                artifact_publish_failed_turns.add(row["turn_id"])
        for turn in turn_values:
            turn["commands"] = by_turn_commands.get(turn["id"], [])
            turn["items"] = by_turn_items.get(turn["id"], [])
            turn["artifacts"] = by_turn_artifacts.get(turn["id"], [])
            turn["artifact_publish_failed"] = (
                turn["id"] in artifact_publish_failed_turns
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "conversation": conversation,
            "turns": turn_values,
            "commands": [self._decode_command(row) for row in commands],
            "queue": queue,
            "artifacts": detached_artifacts,
        }

    @staticmethod
    def _decode_web_push_subscription(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        return value

    @staticmethod
    def _decode_conversation(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["settings"] = _object(value.pop("settings_json"), {})
        value["token_usage"] = _object(value.pop("token_usage_json"), None)
        value["last_error"] = _object(value.pop("last_error_json"), None)
        value["thread_status"] = _object(value.get("thread_status"), value.get("thread_status"))
        value["completion_notification"] = bool(value["completion_notification"])
        value["pinned"] = bool(value["pinned"])
        value["unread"] = bool(value.get("unread", False))
        value["terminal_generation"] = int(value.get("terminal_generation") or 0)
        return value

    @staticmethod
    def _decode_turn(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["error"] = _object(value.pop("error_json"), None)
        return value

    @staticmethod
    def _decode_command(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = _object(value.pop("payload_json"), {})
        return value

    @staticmethod
    def _decode_item(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _decode_artifact(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)
