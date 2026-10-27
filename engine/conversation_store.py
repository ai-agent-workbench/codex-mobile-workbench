"""Durable App Server conversation projection.

The native App Server thread remains authoritative.  This SQLite store keeps
only the small amount of Web-owned state needed to render a conversation,
deduplicate browser commands, and recover after a Web process restart.

There is deliberately no process, PID, stdout, exit-code, parent-task, or
legacy-backend state in this schema.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from engine.codex_app_server import AppServerProtocolError
from shared.diagnostics import emit
from shared.maintenance_admission import MaintenanceAdmission, STEER_ONLY
from engine.conversation_search import (
    SearchQuery,
    agent_message_text_key,
    normalize_search_query,
    realtime_agent_message,
    safe_search_snippet,
    thread_read_agent_copy,
)
from engine.conversation_questions import answer_digest, recommended_answers


SCHEMA_VERSION = 6
WEB_PUSH_SCHEMA_VERSION = 1
QUESTION_SCHEMA_VERSION = 1
CONTEXT_SWITCH_SCHEMA_VERSION = 1
ASYNC_USER_INPUT_SCHEMA_VERSION = 1
ASYNC_AUTO_QUESTION_SCHEMA_VERSION = 1
CONVERSATION_SEARCH_SCHEMA_VERSION = 1
ASYNC_AUTO_WAIT_MS = 60_000
_CONTEXT_SWITCH_DETAIL_LIMIT = 20
ACTIVE_TURN_STATUSES = {"inProgress", "active", "queued", "stopping"}
TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
# Future delivery uses the current journal, not callback snapshots.
# Legacy labels remain fail-closed until the additive intent is promoted.
_NATIVE_DELETE_SETTLED_SQL = """
    c.native_delete_pending = 0
    AND COALESCE(c.recovery_state, '') NOT IN
        ('deleting_native_thread','delete_delivery_unknown',
         'native_thread_deleted','native_thread_absent')
"""
_UNCLAIMED_TERMINAL_DELIVERY_SQL = """
    (t.notification_state IS NULL AND julianday(t.completed_at) BETWEEN
        julianday('now') - (600.0 / 86400.0) AND julianday('now'))
    OR NOT EXISTS (SELECT 1 FROM web_push_dispatches p WHERE p.turn_id=t.id)
    OR EXISTS (SELECT 1 FROM web_push_deliveries d WHERE d.turn_id=t.id AND d.state='pending')
"""
# Shared by discovery and atomic claim: recheck at send time, not just startup.
# NULL/malformed/future timestamps fail closed.  A newer turn supersedes a
# recovery notice even when that newer turn is still running.  Live callbacks
# keep their per-turn semantics so queue advancement cannot race away a notice.
_COMPLETION_NOTIFICATION_ELIGIBILITY = f"""
    t.notification_requested = 1
    AND t.status IN ('completed','failed','interrupted')
    AND t.notification_state IS NULL
    AND NOT EXISTS (SELECT 1 FROM schema_meta enabled
                    WHERE enabled.key='completion_notifications_enabled' AND enabled.value!='1')
    AND (NOT EXISTS (SELECT 1 FROM schema_meta epoch WHERE epoch.key='completion_notifications_epoch')
         OR EXISTS (SELECT 1 FROM commands proof JOIN schema_meta epoch
                    ON epoch.key='completion_notifications_epoch' AND epoch.value=proof.completion_notification_epoch
                    WHERE proof.conversation_id=t.conversation_id AND proof.result_turn_id=t.id))
    AND julianday(t.completed_at) BETWEEN
        julianday(:now) - (600.0 / 86400.0) AND julianday(:now)
    AND EXISTS (
        SELECT 1 FROM conversations c
        WHERE c.id = t.conversation_id AND c.completion_notification = 1
          AND {_NATIVE_DELETE_SETTLED_SQL}
    )
    AND (:recovery = 0 OR NOT EXISTS (
        SELECT 1 FROM turns newer
        WHERE newer.conversation_id = t.conversation_id
          AND newer.ordinal > t.ordinal
    ))
"""
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
# Python str.strip() whitespace, expressed with SQLite built-ins so a source
# rollback never needs to register a Python UDF for the installed triggers.
_SEARCH_STRIP_CHARS = "char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"


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


def _item_projection_kind(item: dict[str, Any]) -> str:
    """Map one native item to the stable browser-safe projection kind."""
    return {
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


def _async_questions(value: Any) -> list[dict[str, Any]] | None:
    """Keep only the documented, browser-safe async-input shape.

    ``agentMessage.questions`` is a native App Server field, not a convention
    inferred from the fallback text.  An unknown future shape is
    deliberately rendered through that fallback instead of being guessed at.
    """
    if not isinstance(value, list) or not value:
        return None
    normalized: list[dict[str, Any]] = []
    for question in value:
        if not isinstance(question, dict):
            return None
        title = question.get("title")
        if (
            not isinstance(title, str)
            or not title.strip()
        ):
            return None
        raw_options = question.get("options")
        if raw_options is None:
            options = None
        elif (
            isinstance(raw_options, list)
            and raw_options
            and all(
                isinstance(option, str)
                and option.strip()
                for option in raw_options
            )
        ):
            options = list(raw_options)
        else:
            return None
        normalized.append({"title": title, "options": options})
    return normalized


class ConversationStore:
    """Thread-safe, transactional local projection of native conversations."""

    def __init__(self, database_path: str | os.PathLike[str]):
        self.path = Path(database_path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._project_deletes: set[str] = set()
        # Browser-only cache invalidation; never native execution state.
        self._browser_history_nonce = uuid.uuid4().hex
        self._browser_history_epochs: dict[str, int] = {}
        self.maintenance_admission = MaintenanceAdmission(self.path)
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
                    delivery TEXT,
                    questions_json TEXT,
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

                -- Only the safe projection of an internal raw Responses
                -- event is retained. In particular, no raw item, call ID,
                -- tool arguments, output, text, or reasoning is stored.
                CREATE TABLE IF NOT EXISTS context_switch_events (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    call_key_sha256 TEXT NOT NULL,
                    call_observed_at TEXT NOT NULL,
                    output_observed_at TEXT,
                    server_version TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, turn_id, call_key_sha256)
                );

                -- Web-owned, opaque request records for native
                -- item/tool/requestUserInput. Native Thread/Turn state stays
                -- authoritative; this table only journals delivery and the
                -- browser-safe projection.
                CREATE TABLE IF NOT EXISTS question_requests (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    native_item_id TEXT NOT NULL,
                    request_id_json TEXT NOT NULL,
                    request_id_type TEXT NOT NULL,
                    connection_epoch TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    fingerprint TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    deadline_at TEXT,
                    deadline_source TEXT,
                    is_blocking INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    answer_source TEXT,
                    answers_json TEXT,
                    answers_digest TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS question_commands (
                    command_id TEXT PRIMARY KEY,
                    question_id TEXT NOT NULL REFERENCES question_requests(id)
                        ON DELETE CASCADE,
                    answers_digest TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS turns_conversation_idx
                    ON turns(conversation_id, ordinal);
                CREATE INDEX IF NOT EXISTS commands_conversation_idx
                    ON commands(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS items_turn_idx
                    ON items(turn_id, row_id);
                CREATE INDEX IF NOT EXISTS items_conversation_idx
                    ON items(conversation_id, row_id);
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
                CREATE INDEX IF NOT EXISTS context_switch_observed_idx
                    ON context_switch_events(
                        conversation_id, output_observed_at, call_observed_at
                    );
                CREATE INDEX IF NOT EXISTS question_requests_conversation_idx
                    ON question_requests(conversation_id, turn_id, state);
                CREATE INDEX IF NOT EXISTS question_requests_reissue_idx
                    ON question_requests(thread_id, turn_id, fingerprint, state);
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
            # Native delete delivery is an operation intent, not a connection
            # recovery label. Reconcile/notifications must not erase it. This
            # additive column remains readable by the previous v6 source.
            try:
                self._begin()
                columns = {row["name"] for row in self._db.execute(
                    "PRAGMA table_info(conversations)"
                ).fetchall()}
                if "native_delete_pending" not in columns:
                    self._db.execute(
                        "ALTER TABLE conversations ADD COLUMN "
                        "native_delete_pending INTEGER NOT NULL DEFAULT 0"
                    )
                self._db.execute(
                    "UPDATE conversations SET native_delete_pending = 1 "
                    "WHERE recovery_state IN "
                    "('deleting_native_thread','delete_delivery_unknown',"
                    "'native_thread_deleted','native_thread_absent')"
                )
                turn_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(turns)").fetchall()}
                if "delete_deferred_terminal" not in turn_columns:
                    self._db.execute("ALTER TABLE turns ADD COLUMN delete_deferred_terminal INTEGER NOT NULL DEFAULT 0")
                if "delete_deferred_recovery" not in turn_columns:
                    self._db.execute("ALTER TABLE turns ADD COLUMN delete_deferred_recovery INTEGER NOT NULL DEFAULT 0")
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            # Questions are an additive Web projection. Keeping this marker
            # separate lets the previous v6 release ignore the new tables on
            # automated source rollback.
            try:
                self._begin()
                extension = self._db.execute(
                    "SELECT value FROM schema_meta WHERE key = 'question_schema_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(QUESTION_SCHEMA_VERSION):
                    raise AppServerProtocolError("unsupported question database schema")
                question_columns = {
                    row["name"] for row in self._db.execute(
                        "PRAGMA table_info(question_requests)"
                    ).fetchall()
                }
                if question_columns and "deadline_source" not in question_columns:
                    self._db.execute(
                        "ALTER TABLE question_requests ADD COLUMN deadline_source TEXT"
                    )
                for table in ("question_requests", "question_commands"):
                    columns = {
                        row["name"] for row in self._db.execute(
                            "PRAGMA table_info(" + table + ")"
                        ).fetchall()
                    }
                    if "attempt" not in columns:
                        self._db.execute(
                            "ALTER TABLE " + table
                            + " ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
                        )
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('question_schema_version', ?)",
                        (str(QUESTION_SCHEMA_VERSION),),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            # Context-switch records are an additive safe projection. The
            # core marker remains v6 so the prior healthy release can safely
            # open this database after a source rollback.
            try:
                self._begin()
                extension = self._db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'context_switch_schema_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(
                    CONTEXT_SWITCH_SCHEMA_VERSION
                ):
                    raise AppServerProtocolError(
                        "unsupported context switch database schema"
                    )
                self._create_context_switch_schema()
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        ("context_switch_schema_version", str(CONTEXT_SWITCH_SCHEMA_VERSION)),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            # Async user-input is a browser-safe projection of documented
            # AgentMessage fields. Keep it additive so an automatic source
            # rollback can still read the prior v6 database.
            try:
                self._begin()
                extension = self._db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'async_user_input_schema_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(
                    ASYNC_USER_INPUT_SCHEMA_VERSION
                ):
                    raise AppServerProtocolError(
                        "unsupported async user input database schema"
                    )
                self._create_async_user_input_schema()
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        ("async_user_input_schema_version", str(
                            ASYNC_USER_INPUT_SCHEMA_VERSION
                        )),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            # Async questions gain a durable host deadline without changing the
            # pre-existing item projection.  The separate marker lets the prior
            # release ignore this extension if a startup smoke rolls back.
            try:
                self._begin()
                extension = self._db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'async_auto_question_schema_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(
                    ASYNC_AUTO_QUESTION_SCHEMA_VERSION
                ):
                    raise AppServerProtocolError(
                        "unsupported async auto question database schema"
                    )
                self._create_async_auto_question_schema()
                self._backfill_async_question_requests_locked()
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        ("async_auto_question_schema_version", str(
                            ASYNC_AUTO_QUESTION_SCHEMA_VERSION
                        )),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
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
            # Search is an additive derived projection.  It deliberately has
            # its own marker: a source rollback can keep using the core v6
            # tables and simply ignore these extra tables/triggers.
            try:
                self._begin()
                extension = self._db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'conversation_search_version'"
                ).fetchone()
                if extension is not None and extension["value"] != str(
                    CONVERSATION_SEARCH_SCHEMA_VERSION
                ):
                    raise AppServerProtocolError(
                        "unsupported conversation search schema"
                    )
                objects = {
                    (row["type"], row["name"])
                    for row in self._db.execute(
                        "SELECT type,name FROM sqlite_master "
                        "WHERE name IN ('conversation_search_documents',"
                        "'conversation_search_fts',"
                        "'conversation_search_fallback_provenance',"
                        "'conversation_search_item_sources')"
                    )
                }
                projection_missing = {
                    ("table", "conversation_search_documents"),
                    ("table", "conversation_search_fts"),
                } - objects
                if extension is not None and (
                    "table", "conversation_search_fallback_provenance"
                ) not in objects:
                    # Unlike the rebuildable index, this remembers rejected
                    # origins even after their source items have disappeared.
                    raise AppServerProtocolError(
                        "conversation search provenance is missing; backup recovery required"
                    )
                if extension is not None and (
                    "table", "conversation_search_item_sources"
                ) not in objects:
                    raise AppServerProtocolError(
                        "conversation search source identities are missing; backup recovery required"
                    )
                self._create_conversation_search_schema()
                if extension is None:
                    self._backfill_conversation_search_sources_locked()
                # A marker can survive a damaged/manual cleanup while the
                # projection tables do not.  Never recreate an empty index
                # and silently treat that as a completed backfill.
                if extension is None or projection_missing:
                    self._backfill_conversation_search_locked()
                if extension is None:
                    self._db.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        ("conversation_search_version", str(
                            CONVERSATION_SEARCH_SCHEMA_VERSION
                        )),
                    )
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            # Nullable admission epochs are additive: a prior release can
            # still write commands after rollback (without proving a new notice).
            try:
                self._begin()
                columns = {row['name'] for row in self._db.execute('PRAGMA table_info(commands)')}
                if 'completion_notification_epoch' not in columns:
                    self._db.execute('ALTER TABLE commands ADD COLUMN completion_notification_epoch TEXT')
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise
            os.chmod(self.path, 0o600)

    def _create_conversation_search_schema(self) -> None:
        """Install the local, safe-text search projection and SQL-only upkeep.

        The triggers use no Python UDF so a prior release, or another normal
        SQLite connection, can continue writing the core projection safely.
        """
        self._execute_conversation_search_script("""
            CREATE TABLE IF NOT EXISTS conversation_search_item_sources (
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                native_item_id TEXT NOT NULL,
                row_id INTEGER,
                blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
                PRIMARY KEY(turn_id,native_item_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS conversation_search_fallback_provenance (
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                trusted INTEGER NOT NULL CHECK(trusted IN (0,1)),
                source_item_id TEXT NOT NULL,
                CHECK(trusted=0 OR source_item_id != ''),
                PRIMARY KEY(turn_id,text,source_item_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS conversation_search_documents (
                doc_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('title','user','assistant')),
                source_id TEXT NOT NULL,
                turn_id TEXT,
                command_id TEXT,
                native_item_id TEXT,
                text TEXT NOT NULL,
                sort_at TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(kind, source_id)
            );
            CREATE INDEX IF NOT EXISTS conversation_search_documents_conversation_idx
                ON conversation_search_documents(conversation_id, kind, sort_at DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search_fts USING fts5(
                text,
                content='conversation_search_documents',
                content_rowid='doc_id',
                tokenize='trigram'
            );

            CREATE TRIGGER IF NOT EXISTS conversation_search_document_ai
            AFTER INSERT ON conversation_search_documents BEGIN
                INSERT INTO conversation_search_fts(rowid, text)
                VALUES (NEW.doc_id, NEW.text);
            END;
            CREATE TRIGGER IF NOT EXISTS conversation_search_document_bd
            BEFORE DELETE ON conversation_search_documents BEGIN
                INSERT INTO conversation_search_fts(conversation_search_fts, rowid, text)
                VALUES ('delete', OLD.doc_id, OLD.text);
            END;
            CREATE TRIGGER IF NOT EXISTS conversation_search_document_au_text
            AFTER UPDATE OF text ON conversation_search_documents
            WHEN OLD.text IS NOT NEW.text BEGIN
                INSERT INTO conversation_search_fts(conversation_search_fts, rowid, text)
                VALUES ('delete', OLD.doc_id, OLD.text);
                INSERT INTO conversation_search_fts(rowid, text)
                VALUES (NEW.doc_id, NEW.text);
            END;

            CREATE TRIGGER IF NOT EXISTS conversation_search_conversation_ai
            AFTER INSERT ON conversations WHEN NEW.thread_id IS NOT NULL
                AND trim(NEW.title) != '' BEGIN
                INSERT INTO conversation_search_documents(
                    conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                    text,sort_at,sort_order
                ) VALUES(NEW.id,'title',NEW.id,NULL,NULL,NULL,NEW.title,NEW.activity_at,0)
                ON CONFLICT(kind,source_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id, text=excluded.text,
                    sort_at=excluded.sort_at, sort_order=excluded.sort_order
                WHERE conversation_search_documents.conversation_id IS NOT excluded.conversation_id
                   OR conversation_search_documents.text IS NOT excluded.text
                   OR conversation_search_documents.sort_at IS NOT excluded.sort_at
                   OR conversation_search_documents.sort_order IS NOT excluded.sort_order;
            END;
            CREATE TRIGGER IF NOT EXISTS conversation_search_conversation_au
            AFTER UPDATE OF title,thread_id,activity_at ON conversations BEGIN
                INSERT INTO conversation_search_documents(
                    conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                    text,sort_at,sort_order
                ) SELECT NEW.id,'title',NEW.id,NULL,NULL,NULL,NEW.title,NEW.activity_at,0
                  WHERE NEW.thread_id IS NOT NULL AND trim(NEW.title) != ''
                ON CONFLICT(kind,source_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id, text=excluded.text,
                    sort_at=excluded.sort_at, sort_order=excluded.sort_order
                WHERE conversation_search_documents.conversation_id IS NOT excluded.conversation_id
                   OR conversation_search_documents.text IS NOT excluded.text
                   OR conversation_search_documents.sort_at IS NOT excluded.sort_at
                   OR conversation_search_documents.sort_order IS NOT excluded.sort_order;
                DELETE FROM conversation_search_documents
                WHERE kind='title' AND source_id=NEW.id
                  AND (NEW.thread_id IS NULL OR trim(NEW.title) = '');
            END;

            CREATE TRIGGER IF NOT EXISTS conversation_search_command_ai
            AFTER INSERT ON commands WHEN NEW.kind IN ('start','steer')
                AND trim(COALESCE(NEW.text,'')) != '' BEGIN
                INSERT INTO conversation_search_documents(
                    conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                    text,sort_at,sort_order
                ) VALUES(NEW.conversation_id,'user',NEW.id,
                    COALESCE(NEW.result_turn_id,NEW.target_turn_id),NEW.id,NULL,
                    NEW.text,NEW.created_at,0)
                ON CONFLICT(kind,source_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,turn_id=excluded.turn_id,
                    command_id=excluded.command_id,native_item_id=excluded.native_item_id,
                    text=excluded.text,sort_at=excluded.sort_at,sort_order=excluded.sort_order
                WHERE conversation_search_documents.conversation_id IS NOT excluded.conversation_id
                   OR conversation_search_documents.turn_id IS NOT excluded.turn_id
                   OR conversation_search_documents.command_id IS NOT excluded.command_id
                   OR conversation_search_documents.native_item_id IS NOT excluded.native_item_id
                   OR conversation_search_documents.text IS NOT excluded.text
                   OR conversation_search_documents.sort_at IS NOT excluded.sort_at
                   OR conversation_search_documents.sort_order IS NOT excluded.sort_order;
            END;
            CREATE TRIGGER IF NOT EXISTS conversation_search_command_au
            AFTER UPDATE OF kind,text,result_turn_id,target_turn_id,created_at ON commands BEGIN
                INSERT INTO conversation_search_documents(
                    conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                    text,sort_at,sort_order
                ) SELECT NEW.conversation_id,'user',NEW.id,
                    COALESCE(NEW.result_turn_id,NEW.target_turn_id),NEW.id,NULL,
                    NEW.text,NEW.created_at,0
                  WHERE NEW.kind IN ('start','steer')
                    AND trim(COALESCE(NEW.text,'')) != ''
                ON CONFLICT(kind,source_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,turn_id=excluded.turn_id,
                    command_id=excluded.command_id,native_item_id=excluded.native_item_id,
                    text=excluded.text,sort_at=excluded.sort_at,sort_order=excluded.sort_order
                WHERE conversation_search_documents.conversation_id IS NOT excluded.conversation_id
                   OR conversation_search_documents.turn_id IS NOT excluded.turn_id
                   OR conversation_search_documents.command_id IS NOT excluded.command_id
                   OR conversation_search_documents.native_item_id IS NOT excluded.native_item_id
                   OR conversation_search_documents.text IS NOT excluded.text
                   OR conversation_search_documents.sort_at IS NOT excluded.sort_at
                   OR conversation_search_documents.sort_order IS NOT excluded.sort_order;
                DELETE FROM conversation_search_documents
                WHERE kind='user' AND source_id=NEW.id
                  AND (NEW.kind NOT IN ('start','steer')
                       OR trim(COALESCE(NEW.text,'')) = '');
            END;
            CREATE TRIGGER IF NOT EXISTS conversation_search_command_ad
            AFTER DELETE ON commands BEGIN
                DELETE FROM conversation_search_documents
                WHERE kind='user' AND source_id=OLD.id;
            END;
        """)
        fallback = self._conversation_search_fallback_sql()
        # Recompute one assistant document with a literal SQL template.  The
        # latest non-async final item wins; only when no such text exists does
        # turns.final_text become the compatibility fallback.
        def assistant_trigger(
            name: str, event: str, table: str, ref: str, when: str = "", *,
            item_ref: str | None = None, deleted_item: bool = False,
        ) -> None:
            effective_i = self._conversation_search_effective_source_sql("i")
            candidate = f"""
                {self._conversation_search_record_source_sql(item_ref) if item_ref else ''}
                {'''UPDATE conversation_search_item_sources SET row_id=NULL
                    WHERE turn_id=OLD.turn_id AND native_item_id=OLD.native_item_id
                      AND row_id=OLD.row_id;''' if deleted_item else ''}
                {self._conversation_search_source_correction_sql(item_ref) if item_ref else ''}
                {self._conversation_search_observed_proofs_sql(ref)}
                {self._conversation_search_provenance_sql(ref)}
                INSERT INTO conversation_search_documents(
                    conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                    text,sort_at,sort_order
                )
                SELECT t.conversation_id,'assistant',t.id,t.id,NULL,
                    (SELECT i.native_item_id FROM items i
                     WHERE i.turn_id=t.id AND i.kind='agent_message'
                       AND {effective_i}
                       AND i.phase='final_answer'
                       AND COALESCE(i.delivery,'') != 'async'
                       AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                       AND trim(COALESCE(i.text,'')) != ''
                     ORDER BY i.row_id DESC LIMIT 1),
                    COALESCE(
                        (SELECT trim(i.text) FROM items i
                         WHERE i.turn_id=t.id AND i.kind='agent_message'
                           AND {effective_i}
                           AND i.phase='final_answer'
                           AND COALESCE(i.delivery,'') != 'async'
                           AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                           AND trim(COALESCE(i.text,'')) != ''
                         ORDER BY i.row_id DESC LIMIT 1),
                        {fallback}
                    ),
                    COALESCE(t.completed_at,t.started_at,''),t.ordinal
                FROM turns t WHERE t.id={ref}
                  AND COALESCE(
                    (SELECT trim(i.text) FROM items i
                     WHERE i.turn_id=t.id AND i.kind='agent_message'
                       AND {effective_i}
                       AND i.phase='final_answer'
                       AND COALESCE(i.delivery,'') != 'async'
                       AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                       AND trim(COALESCE(i.text,'')) != ''
                     ORDER BY i.row_id DESC LIMIT 1),
                    {fallback}
                  ) IS NOT NULL
                ON CONFLICT(kind,source_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,turn_id=excluded.turn_id,
                    command_id=excluded.command_id,native_item_id=excluded.native_item_id,
                    text=excluded.text,sort_at=excluded.sort_at,sort_order=excluded.sort_order
                WHERE conversation_search_documents.conversation_id IS NOT excluded.conversation_id
                   OR conversation_search_documents.turn_id IS NOT excluded.turn_id
                   OR conversation_search_documents.command_id IS NOT excluded.command_id
                   OR conversation_search_documents.native_item_id IS NOT excluded.native_item_id
                   OR conversation_search_documents.text IS NOT excluded.text
                   OR conversation_search_documents.sort_at IS NOT excluded.sort_at
                   OR conversation_search_documents.sort_order IS NOT excluded.sort_order;
                DELETE FROM conversation_search_documents
                WHERE kind='assistant' AND source_id={ref}
                  AND NOT EXISTS (
                    SELECT 1 FROM turns t WHERE t.id={ref}
                    AND COALESCE(
                        (SELECT trim(i.text) FROM items i
                         WHERE i.turn_id=t.id AND i.kind='agent_message'
                           AND {effective_i}
                           AND i.phase='final_answer'
                           AND COALESCE(i.delivery,'') != 'async'
                           AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                           AND trim(COALESCE(i.text,'')) != ''
                         ORDER BY i.row_id DESC LIMIT 1),
                        {fallback}
                    ) IS NOT NULL
                  );
            """
            self._execute_conversation_search_script(
                f"CREATE TRIGGER IF NOT EXISTS {name} {event} ON "
                + table + " "
                + ("WHEN " + when + " " if when else "")
                + "BEGIN " + candidate + " END;"
            )

        assistant_trigger(
            "conversation_search_turn_au", "AFTER UPDATE OF final_text,completed_at,started_at,ordinal,status",
            "turns", "NEW.id",
        )
        assistant_trigger(
            "conversation_search_turn_ai", "AFTER INSERT", "turns", "NEW.id",
        )
        assistant_trigger(
            "conversation_search_item_ai", "AFTER INSERT", "items", "NEW.turn_id",
            "NEW.kind='agent_message' OR EXISTS ("
            "SELECT 1 FROM conversation_search_item_sources origin "
            "WHERE origin.turn_id=NEW.turn_id AND origin.native_item_id=NEW.native_item_id)",
            item_ref="NEW",
        )
        assistant_trigger(
            "conversation_search_item_au", "AFTER UPDATE OF kind,phase,delivery,text,status", "items", "NEW.turn_id",
            "NEW.kind='agent_message' OR OLD.kind='agent_message' OR EXISTS ("
            "SELECT 1 FROM conversation_search_item_sources origin "
            "WHERE origin.turn_id=NEW.turn_id AND origin.native_item_id=NEW.native_item_id)",
            item_ref="NEW",
        )
        assistant_trigger(
            "conversation_search_item_ad", "AFTER DELETE", "items", "OLD.turn_id",
            "OLD.kind='agent_message' OR EXISTS ("
            "SELECT 1 FROM conversation_search_item_sources origin "
            "WHERE origin.turn_id=OLD.turn_id AND origin.native_item_id=OLD.native_item_id)",
            deleted_item=True,
        )
        self._execute_conversation_search_script("""
            CREATE TRIGGER IF NOT EXISTS conversation_search_turn_ad
            AFTER DELETE ON turns BEGIN
                DELETE FROM conversation_search_documents
                WHERE kind='assistant' AND source_id=OLD.id;
            END;
        """)

    @staticmethod
    def _conversation_search_effective_source_sql(alias: str) -> str:
        # Native IDs identify a source, whereas the legacy projection keeps
        # one row per (turn, ID, kind). Never revive a superseded kind row.
        return f"""EXISTS (SELECT 1 FROM conversation_search_item_sources effective
            WHERE effective.turn_id={alias}.turn_id
              AND effective.native_item_id={alias}.native_item_id
              AND effective.row_id={alias}.row_id)"""

    @staticmethod
    def _conversation_search_unsafe_source_sql(alias: str) -> str:
        # Only an actual final/legacy synchronous body can prove a fallback.
        # Other known agent bodies receive per-body rejection evidence.
        return f"""({alias}.kind != 'agent_message'
            OR COALESCE({alias}.delivery,'')='async'
            OR ({alias}.phase IS NOT NULL AND {alias}.phase != 'final_answer'))"""

    @staticmethod
    def _conversation_search_record_source_sql(item_ref: str) -> str:
        return f"""
            INSERT INTO conversation_search_item_sources(turn_id,native_item_id,row_id,blocked)
            VALUES({item_ref}.turn_id,{item_ref}.native_item_id,{item_ref}.row_id,
                CASE WHEN {item_ref}.kind='agent_message'
                  AND {item_ref}.delivery='async' THEN 1 ELSE 0 END)
            ON CONFLICT(turn_id,native_item_id) DO UPDATE SET
                row_id=excluded.row_id,
                blocked=MAX(conversation_search_item_sources.blocked,excluded.blocked);
        """

    def _backfill_conversation_search_sources_locked(self) -> None:
        # Bootstrap only on the first install. Thereafter this journal also
        # retains tombstones and rejected classifications after item deletion.
        self._db.execute("""
            INSERT INTO conversation_search_item_sources(turn_id,native_item_id,row_id,blocked)
            SELECT turn_id,native_item_id,row_id,blocked FROM (
                SELECT i.*,
                    ROW_NUMBER() OVER (PARTITION BY turn_id,native_item_id
                        ORDER BY updated_at DESC,row_id DESC) AS choice,
                    MAX(CASE WHEN kind='agent_message' AND delivery='async'
                        THEN 1 ELSE 0 END)
                        OVER (PARTITION BY turn_id,native_item_id) AS blocked
                FROM items i WHERE EXISTS (
                    SELECT 1 FROM items origin WHERE origin.turn_id=i.turn_id
                      AND origin.native_item_id=i.native_item_id AND origin.kind='agent_message'
                )
            ) WHERE choice=1
        """)

    @staticmethod
    def _conversation_search_observed_proofs_sql(ref: str | None = None) -> str:
        """Remember durable answer bodies even before final_text is written.

        This also rejects non-final and superseded rows on first backfill. Proofs
        survive deletion, but an actual source correction revokes all bodies
        previously proved by that native identity.
        """
        selected = f"i.turn_id={ref} AND " if ref is not None else ""
        normalized = f"trim(COALESCE(i.text,''),{_SEARCH_STRIP_CHARS})"
        trusted = f"""CASE WHEN {ConversationStore._conversation_search_effective_source_sql('i')}
            AND NOT {ConversationStore._conversation_search_unsafe_source_sql('i')}
            THEN 1 ELSE 0 END"""
        return f"""
            INSERT INTO conversation_search_fallback_provenance(turn_id,text,trusted,source_item_id)
            SELECT i.turn_id,{normalized},{trusted},i.native_item_id
            FROM items i JOIN turns t ON t.id=i.turn_id
            WHERE {selected}i.kind='agent_message'
              AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
              AND {normalized} != ''
            ORDER BY {trusted} ASC,i.row_id ASC
            ON CONFLICT(turn_id,text,source_item_id) DO UPDATE SET
                trusted=excluded.trusted
            WHERE excluded.trusted=1 AND (
                conversation_search_fallback_provenance.trusted != 1
            );
        """

    @staticmethod
    def _conversation_search_source_correction_sql(item_ref: str) -> str:
        """Revoke historical proofs by item identity, even during a NULL final.

        A source's text can change before its delivery/phase is corrected.
        Therefore revocation must cover every text previously proved by that
        item, not only its current body or the turn's current saved answer.
        """
        return f"""
            UPDATE conversation_search_fallback_provenance
            SET trusted=0
            WHERE conversation_search_fallback_provenance.turn_id={item_ref}.turn_id
              AND source_item_id={item_ref}.native_item_id AND trusted=1
              AND NOT (
                  {item_ref}.kind='agent_message'
                  AND ({item_ref}.phase IS NULL OR {item_ref}.phase='final_answer')
                  AND COALESCE({item_ref}.delivery,'') != 'async'
                  AND (COALESCE({item_ref}.status,'')='completed' OR EXISTS (
                      SELECT 1 FROM turns t WHERE t.id={item_ref}.turn_id
                        AND t.status IN ('completed','failed','interrupted')
                  ))
              );
        """

    @staticmethod
    def _conversation_search_provenance_sql(ref: str | None = None) -> str:
        """Remember proven/rejected saved answers independently of source items.

        Only synchronous durable text proves a fallback. Rejection survives
        source deletion, temporary NULLs, index rebuilds and old v6 writers.
        A later real answer with the same text can upgrade that proof safely.
        A corrected non-answer source can revoke it; merely deleting the source
        cannot erase a previously recorded rejection.
        Neither original items nor saved turn text is changed by this journal.
        """
        normalized = f"trim(COALESCE(t.final_text,''),{_SEARCH_STRIP_CHARS})"
        selected = f"t.id={ref} AND " if ref is not None else ""
        return f"""
            INSERT INTO conversation_search_fallback_provenance(turn_id,text,trusted,source_item_id)
            SELECT t.id,{normalized},0,''
            FROM turns t WHERE {selected}{normalized} != '' AND (
                EXISTS (
                    SELECT 1 FROM items origin WHERE origin.turn_id=t.id
                      AND origin.kind='agent_message' AND origin.delivery='async'
                ) OR EXISTS (
                    SELECT 1 FROM conversation_search_item_sources origin
                    WHERE origin.turn_id=t.id AND origin.blocked=1
                )
            )
            ON CONFLICT(turn_id,text,source_item_id) DO NOTHING;
        """

    @staticmethod
    def _conversation_search_fallback_sql() -> str:
        normalized = f"trim(COALESCE(t.final_text,''),{_SEARCH_STRIP_CHARS})"
        return f"""CASE WHEN
            EXISTS (
                SELECT 1 FROM conversation_search_fallback_provenance origin
                WHERE origin.turn_id=t.id AND origin.text={normalized} AND origin.trusted=1
            ) OR (
                NOT EXISTS (
                    SELECT 1 FROM conversation_search_item_sources origin
                    WHERE origin.turn_id=t.id AND origin.blocked=1
                ) AND
                NOT EXISTS (
                    SELECT 1 FROM items origin WHERE origin.turn_id=t.id
                      AND origin.kind='agent_message' AND origin.delivery='async'
                ) AND NOT EXISTS (
                    SELECT 1 FROM conversation_search_fallback_provenance origin
                    WHERE origin.turn_id=t.id AND origin.text={normalized} AND origin.trusted=0
                )
            ) THEN NULLIF({normalized}, '') ELSE NULL END"""

    def _execute_conversation_search_script(self, script: str) -> None:
        """Run a multi-statement trigger script without ``executescript``'s COMMIT.

        sqlite3.Connection.executescript commits an outer transaction first,
        which would make a failed first-time derived-index install partial.
        ``complete_statement`` understands trigger ``BEGIN … END`` blocks.
        """
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                self._db.execute(statement)
                statement = ""
        if statement.strip():
            raise AppServerProtocolError("conversation search schema statement is incomplete")

    def _backfill_conversation_search_locked(self) -> None:
        """Build search documents from the safe local projection, once."""
        fallback = self._conversation_search_fallback_sql()
        effective_i = self._conversation_search_effective_source_sql("i")
        self._execute_conversation_search_script(self._conversation_search_observed_proofs_sql())
        self._execute_conversation_search_script(self._conversation_search_provenance_sql())
        # If only the FTS table was lost, the surviving documents' DELETE
        # triggers must first see a populated index, not an empty new one.
        self._db.execute("INSERT INTO conversation_search_fts(conversation_search_fts) VALUES('rebuild')")
        self._db.execute("DELETE FROM conversation_search_documents")
        self._db.execute("""
            INSERT INTO conversation_search_documents(
                conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                text,sort_at,sort_order
            ) SELECT id,'title',id,NULL,NULL,NULL,title,activity_at,0
              FROM conversations WHERE thread_id IS NOT NULL AND trim(title) != ''
        """)
        self._db.execute("""
            INSERT INTO conversation_search_documents(
                conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                text,sort_at,sort_order
            ) SELECT conversation_id,'user',id,COALESCE(result_turn_id,target_turn_id),
                id,NULL,text,created_at,0 FROM commands
              WHERE kind IN ('start','steer') AND trim(COALESCE(text,'')) != ''
        """)
        self._db.execute(f"""
            INSERT INTO conversation_search_documents(
                conversation_id,kind,source_id,turn_id,command_id,native_item_id,
                text,sort_at,sort_order
            ) SELECT t.conversation_id,'assistant',t.id,t.id,NULL,
                (SELECT i.native_item_id FROM items i WHERE i.turn_id=t.id
                   AND {effective_i}
                   AND i.kind='agent_message' AND i.phase='final_answer'
                   AND COALESCE(i.delivery,'') != 'async'
                   AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                   AND trim(COALESCE(i.text,'')) != ''
                 ORDER BY i.row_id DESC LIMIT 1),
                COALESCE((SELECT trim(i.text) FROM items i WHERE i.turn_id=t.id
                   AND {effective_i}
                   AND i.kind='agent_message' AND i.phase='final_answer'
                   AND COALESCE(i.delivery,'') != 'async'
                   AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                   AND trim(COALESCE(i.text,'')) != ''
                 ORDER BY i.row_id DESC LIMIT 1),
                 {fallback}),
                COALESCE(t.completed_at,t.started_at,''),t.ordinal
              FROM turns t
             WHERE COALESCE((SELECT trim(i.text) FROM items i WHERE i.turn_id=t.id
                   AND {effective_i}
                   AND i.kind='agent_message' AND i.phase='final_answer'
                   AND COALESCE(i.delivery,'') != 'async'
                   AND (i.status='completed' OR t.status IN ('completed','failed','interrupted'))
                   AND trim(COALESCE(i.text,'')) != ''
                 ORDER BY i.row_id DESC LIMIT 1),
                 {fallback}) IS NOT NULL
        """)

    def _create_context_switch_schema(self) -> None:
        """Install the additive, raw-free context-switch projection."""
        for statement in (
            """CREATE TABLE IF NOT EXISTS context_switch_events (
                conversation_id TEXT NOT NULL REFERENCES conversations(id)
                    ON DELETE CASCADE,
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                call_key_sha256 TEXT NOT NULL,
                call_observed_at TEXT NOT NULL,
                output_observed_at TEXT,
                server_version TEXT NOT NULL,
                PRIMARY KEY(conversation_id, turn_id, call_key_sha256)
            )""",
            """CREATE INDEX IF NOT EXISTS context_switch_observed_idx
                ON context_switch_events(
                    conversation_id, output_observed_at, call_observed_at
                )""",
        ):
            self._db.execute(statement)

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

    def _create_async_user_input_schema(self) -> None:
        """Install the additive AgentMessage async-input projection."""
        columns = {
            row["name"] for row in self._db.execute(
                "PRAGMA table_info(items)"
            ).fetchall()
        }
        if "delivery" not in columns:
            self._db.execute("ALTER TABLE items ADD COLUMN delivery TEXT")
        if "questions_json" not in columns:
            self._db.execute("ALTER TABLE items ADD COLUMN questions_json TEXT")

    def _create_async_auto_question_schema(self) -> None:
        """Install durable host-owned resolution records for async questions."""
        command_columns = {
            row["name"] for row in self._db.execute(
                "PRAGMA table_info(commands)"
            ).fetchall()
        }
        if "origin" not in command_columns:
            self._db.execute(
                "ALTER TABLE commands ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'"
            )
        for statement in (
            """CREATE TABLE IF NOT EXISTS async_question_requests (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id)
                    ON DELETE CASCADE,
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                native_item_id TEXT NOT NULL,
                question_index INTEGER NOT NULL,
                question_json TEXT NOT NULL,
                recommended_answer TEXT,
                deadline_at TEXT,
                state TEXT NOT NULL,
                answer_source TEXT,
                answer_text TEXT,
                command_id TEXT,
                resolved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, turn_id, native_item_id, question_index)
            )""",
            """CREATE INDEX IF NOT EXISTS async_question_requests_pending_idx
                ON async_question_requests(state, deadline_at, conversation_id)""",
            """CREATE INDEX IF NOT EXISTS async_question_requests_item_idx
                ON async_question_requests(turn_id, native_item_id, question_index)""",
        ):
            self._db.execute(statement)

    def _backfill_async_question_requests_locked(self) -> None:
        """Register active cards that arrived before this additive extension."""
        rows = self._db.execute(
            """SELECT i.conversation_id, i.turn_id, i.native_item_id,
                      i.questions_json
               FROM items i
               JOIN turns t ON t.id = i.turn_id
               WHERE i.delivery = 'async' AND i.questions_json IS NOT NULL
                 AND t.status NOT IN ('completed', 'failed', 'interrupted')"""
        ).fetchall()
        now = utc_now()
        for row in rows:
            questions = _async_questions(_object(row["questions_json"], None))
            if questions:
                self._register_async_questions_locked(
                    row["conversation_id"], row["turn_id"],
                    row["native_item_id"], questions, now=now,
                )

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

    def question_request_ids_for_deadline_cancel(
        self, conversation_id: str
    ) -> tuple[list[str], list[str]]:
        """Return async then interactive request ids without hydrating questions."""
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            async_ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM async_question_requests WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()]
            question_ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM question_requests WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()]
        return async_ids, question_ids

    def initial_title_source_live(self, conversation_id: str, turn_id: str) -> bool:
        """Test the title source predicate without reading projected item bodies."""
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        with self._lock:
            row = self._db.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM conversations c
                    JOIN turns t ON t.conversation_id = c.id AND t.id = ?
                    WHERE c.id = ?
                      AND c.active_turn_id = ?
                      AND c.state <> 'interrupting'
                      AND c.native_delete_pending = 0
                      AND COALESCE(c.recovery_state, '') NOT IN
                          ('deleting_native_thread','delete_delivery_unknown',
                           'native_thread_deleted','native_thread_absent')
                      AND t.status IN ('inProgress','active','queued')
                      AND NOT EXISTS (
                          SELECT 1 FROM commands command
                          WHERE command.conversation_id = c.id
                            AND command.kind = 'interrupt'
                            AND command.target_turn_id = ?
                            AND command.state IN
                                ('submitted','queued','accepted','delivery_unknown')
                      )
                ) AS live
                """,
                (turn_id, conversation_id, turn_id, turn_id),
            ).fetchone()
        return bool(row["live"])

    def turn_ids_started_at_or_before(
        self, conversation_id: str, cutoff: str
    ) -> list[str]:
        """Read historical turn ids for a stop proof without item projection."""
        self._validate_id(conversation_id, "conversation")
        if not isinstance(cutoff, str):
            raise AppServerProtocolError("turn cutoff is invalid")
        with self._lock:
            return [row["id"] for row in self._db.execute(
                """
                SELECT id FROM turns
                WHERE conversation_id = ? AND started_at <= ?
                ORDER BY ordinal ASC
                """,
                (conversation_id, cutoff),
            ).fetchall()]

    def turn_terminal_checkpoints(
        self, conversation_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return every pre-reconcile terminal marker without loading items."""
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, status, completed_at FROM turns
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchall()
        return {
            row["id"]: {"id": row["id"], "status": row["status"],
                        "completed_at": row["completed_at"]}
            for row in rows
        }

    def capture_reconcile_checkpoint(self, conversation_id: str) -> dict[str, Any]:
        """Capture projection revisions before an awaited native history read.

        The checkpoint is intentionally internal and scoped to one
        conversation.  Reconciliation uses it to avoid replacing a live
        notification update with an older page observed during that read.
        """
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            conversation = self._conversation_row(conversation_id)
            item_rows = self._db.execute(
                """
                SELECT turn_id, native_item_id, kind, revision, source FROM items
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchall()
            turn_rows = self._db.execute(
                "SELECT id, status FROM turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        return {
            "conversation_id": conversation_id,
            "items": {
                (row["turn_id"], row["native_item_id"], row["kind"]): row["revision"]
                for row in item_rows
            },
            "item_sources": {
                (row["turn_id"], row["native_item_id"], row["kind"]): row["source"]
                for row in item_rows
            },
            "turn_statuses": {row["id"]: row["status"] for row in turn_rows},
            "thread_status": conversation["thread_status"],
        }

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
        select = """SELECT conversations.*,
                (SELECT status FROM turns
                 WHERE id = conversations.last_terminal_turn_id) AS last_terminal_status"""
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

    def list_conversation_ids(
        self,
        *,
        project_id: str | None | object = ...,
        account_id: str | None = None,
    ) -> list[str]:
        return self.list_conversation_order(
            project_id=project_id, account_id=account_id,
        )[0]

    def list_conversation_order(
        self,
        *,
        project_id: str | None | object = ...,
        account_id: str | None = None,
    ) -> tuple[list[str], str]:
        """Snapshot IDs: pinned, successful unread results, then recent activity.

        Only this paginated history order changes; the homepage's recent list
        remains activity-based. Reading does not change conversation activity.
        The fingerprint also covers date/status groups, without reading text or
        allocating a cursor. Token deltas and projection revisions are excluded.
        """
        account = str(account_id or "").strip()[:200]
        clauses = ["c.thread_id IS NOT NULL"]
        values: list[Any] = [account, account]
        if project_id is not ...:
            if project_id is None:
                clauses.append("c.project_id IS NULL")
            else:
                clauses.append("c.project_id = ?")
                values.append(project_id)
        with self._lock:
            rows = self._db.execute(
                """
                SELECT c.id, c.pinned, c.activity_at, c.created_at,
                       c.last_terminal_at, c.state, t.status AS terminal_status,
                       CASE WHEN ? != '' AND c.state = 'idle'
                            AND t.status = 'completed'
                            AND c.terminal_generation > COALESCE(r.seen_generation, 0)
                       THEN 1 ELSE 0 END AS completed_unread
                FROM conversations c
                LEFT JOIN turns t ON t.id = c.last_terminal_turn_id
                LEFT JOIN conversation_reads r
                  ON r.conversation_id = c.id AND r.account_id = ?
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY c.pinned DESC,
                    CASE WHEN c.pinned = 0 THEN completed_unread ELSE 0 END DESC,
                    CASE WHEN c.pinned = 0 AND completed_unread = 1
                         THEN c.last_terminal_at ELSE c.activity_at END DESC,
                    c.created_at DESC, c.id DESC
                """,
                values,
            ).fetchall()
        signature = [
            [row["id"], row["pinned"], row["completed_unread"], row["state"],
             row["terminal_status"],
             row["last_terminal_at"] if row["state"] == "idle" and row["last_terminal_at"]
             else row["activity_at"]]
            for row in rows
        ]
        # Date headings must be rebuilt after midnight, even with no new turn.
        version = hashlib.sha256(_json([
            datetime.now().astimezone().date().isoformat(), signature,
        ]).encode()).hexdigest()
        return [str(row["id"]) for row in rows], version

    @staticmethod
    def _search_query(value: str | SearchQuery) -> SearchQuery:
        return value if isinstance(value, SearchQuery) else normalize_search_query(value)

    @staticmethod
    def _search_fts_expression(query: SearchQuery) -> str | None:
        terms = [term for term in query.tokens if len(term) >= 3]
        if not terms:
            return None
        # Quoting makes every caller character data, never an FTS operator.
        return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

    @staticmethod
    def _search_clauses(
        query: SearchQuery, *, alias: str = "d", use_fts: bool = True
    ) -> tuple[list[str], list[Any]]:
        clauses = [f"instr(lower({alias}.text), lower(?)) > 0" for _ in query.tokens]
        values: list[Any] = list(query.tokens)
        expression = ConversationStore._search_fts_expression(query) if use_fts else None
        if expression is not None:
            clauses.insert(
                0,
                f"({alias}.doc_id IN (SELECT rowid FROM conversation_search_fts "
                f"WHERE conversation_search_fts MATCH ?) OR instr({alias}.text,char(0)) > 0)",
            )
            values.insert(0, expression)
        return clauses, values

    def _search_select(
        self,
        sql: str,
        query: SearchQuery,
        values: list[Any],
    ) -> list[sqlite3.Row]:
        """Use trigram candidates where supported, safely scan on odd literals.

        FTS's query grammar is never user-controlled.  A few valid literal
        strings (notably unusual quote combinations) are not accepted by some
        SQLite FTS builds, so retain correct literal semantics by retrying the
        same bounded derived-document query without its candidate clause.
        """
        clauses, query_values = self._search_clauses(query)
        try:
            return self._db.execute(sql.format(search=" AND ".join(clauses)), query_values + values).fetchall()
        except sqlite3.OperationalError:
            clauses, query_values = self._search_clauses(query, use_fts=False)
            return self._db.execute(sql.format(search=" AND ".join(clauses)), query_values + values).fetchall()

    def search_conversation_ids(
        self,
        query: str | SearchQuery,
        *,
        project_id: str | None | object = ...,
    ) -> list[str]:
        """Return current searchable conversation IDs, title hits before body hits.

        Only document IDs cross this first query; snippets are loaded later for
        the at-most-one-page batch requested by ``search_matches_by_ids``.
        """
        parsed = self._search_query(query)
        scope = ["c.thread_id IS NOT NULL"]
        values: list[Any] = []
        if project_id is not ...:
            if project_id is None:
                scope.append("c.project_id IS NULL")
            else:
                scope.append("c.project_id = ?")
                values.append(project_id)
        sql = """
            SELECT c.id,
                   MAX(CASE WHEN d.kind = 'title' THEN 1 ELSE 0 END) AS title_hit
            FROM conversation_search_documents d
            JOIN conversations c ON c.id = d.conversation_id
            WHERE {search} AND """ + " AND ".join(scope) + """
            GROUP BY c.id
            ORDER BY title_hit DESC, c.activity_at DESC, c.id DESC
        """
        with self._lock:
            return [str(row["id"]) for row in self._search_select(sql, parsed, values)]

    def search_matches_by_ids(
        self,
        query: str | SearchQuery,
        conversation_ids: list[str],
        *,
        project_id: str | None | object = ...,
    ) -> list[dict[str, Any]]:
        """Return one current safe hit per requested ID, with a bounded excerpt."""
        parsed = self._search_query(query)
        ids = list(dict.fromkeys(
            value for value in conversation_ids
            if isinstance(value, str) and _ID_RE.fullmatch(value)
        ))
        if not ids:
            return []
        scope = ["c.thread_id IS NOT NULL"]
        values: list[Any] = []
        if project_id is not ...:
            if project_id is None:
                scope.append("c.project_id IS NULL")
            else:
                scope.append("c.project_id = ?")
                values.append(project_id)
        scope.append("d.conversation_id IN (" + ",".join("?" for _ in ids) + ")")
        values.extend(ids)
        sql = """
            WITH matches AS (
                SELECT d.conversation_id,d.kind,d.turn_id,d.command_id,
                       d.native_item_id,d.text,
                       COUNT(*) OVER (PARTITION BY d.conversation_id) AS match_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.conversation_id
                           ORDER BY CASE WHEN d.kind='title' THEN 0 ELSE 1 END,
                                    d.sort_at DESC,d.sort_order DESC,d.doc_id DESC
                       ) AS choice
                FROM conversation_search_documents d
                JOIN conversations c ON c.id=d.conversation_id
                WHERE {search} AND """ + " AND ".join(scope) + """
            )
            SELECT conversation_id,kind,turn_id,command_id,native_item_id,text,match_count
            FROM matches WHERE choice=1
        """
        with self._lock:
            rows = self._search_select(sql, parsed, values)
            target_ids = [self._canonical_search_item_id_locked(row) for row in rows]
        result: list[dict[str, Any]] = []
        for row, target_id in zip(rows, target_ids):
            snippet, highlights = safe_search_snippet(row["text"], parsed)
            result.append({
                "conversation_id": str(row["conversation_id"]),
                "kind": str(row["kind"]),
                "turn_id": row["turn_id"],
                "command_id": row["command_id"],
                "item_id": target_id,
                "snippet": snippet,
                "highlights": highlights,
                "match_count": int(row["match_count"]),
            })
        return result

    def _canonical_search_item_id_locked(self, hit: sqlite3.Row) -> str | None:
        """Map a hidden thread-read copy to its browser-visible native message.

        Only the chosen turn's final agent text is read; never load the whole
        conversation or call Native. Both paths use the same equivalence key.
        """
        item_id = hit["native_item_id"]
        if hit["kind"] != "assistant" or not isinstance(item_id, str) or not re.fullmatch(r"item-\d+", item_id):
            return item_id
        original = self._db.execute(
            "SELECT native_item_id,phase,text,delivery,source FROM items "
            "WHERE turn_id=? AND native_item_id=? AND kind='agent_message'",
            (hit["turn_id"],item_id),
        ).fetchone()
        if original is None or not thread_read_agent_copy(dict(original)):
            return item_id
        key = agent_message_text_key(dict(original))
        candidates = self._db.execute(
            f"""SELECT i.native_item_id,i.phase,i.text,i.delivery,i.source FROM items i
                WHERE i.turn_id=? AND i.kind='agent_message' AND i.phase='final_answer'
                  AND COALESCE(i.delivery,'') != 'async'
                  AND substr(i.native_item_id,1,4)='msg_'
                  AND {self._conversation_search_effective_source_sql('i')}
                ORDER BY i.row_id ASC""", (hit["turn_id"],),
        ).fetchall()
        for candidate in candidates:
            if realtime_agent_message(dict(candidate)) and agent_message_text_key(dict(candidate)) == key:
                return str(candidate["native_item_id"])
        return item_id

    def list_conversations_by_ids(
        self,
        conversation_ids: list[str],
        *,
        project_id: str | None | object = ...,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load current safe projection rows for IDs after re-checking their scope."""
        ids = list(dict.fromkeys(
            value for value in conversation_ids
            if isinstance(value, str) and _ID_RE.fullmatch(value)
        ))
        if not ids:
            return []
        clauses = ["conversations.thread_id IS NOT NULL"]
        where_values: list[Any] = []
        if project_id is not ...:
            if project_id is None:
                clauses.append("conversations.project_id IS NULL")
            else:
                clauses.append("conversations.project_id = ?")
                where_values.append(project_id)
        clauses.append("conversations.id IN (" + ",".join("?" for _ in ids) + ")")
        account = str(account_id or "").strip()[:200]
        select = """SELECT conversations.*,
                (SELECT status FROM turns
                 WHERE id = conversations.last_terminal_turn_id) AS last_terminal_status"""
        values: list[Any] = []
        if account:
            select += """,
                CASE WHEN conversations.terminal_generation > COALESCE((
                    SELECT seen_generation FROM conversation_reads
                    WHERE account_id = ?
                      AND conversation_id = conversations.id
                ), 0) THEN 1 ELSE 0 END AS unread
            """
            values.append(account)
        else:
            select += ", 0 AS unread"
        values.extend(where_values)
        values.extend(ids)
        with self._lock:
            rows = self._db.execute(
                select + " FROM conversations WHERE " + " AND ".join(clauses), values
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
            if self._project_deletes.intersection(values):
                return True
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
            if row is not None:
                return True
            # Admission receipts precede asynchronous native preflight. A
            # filesystem deletion cannot race past that already-saved intent.
            if self._db.execute(f'''SELECT 1 FROM conversations c JOIN commands x ON x.conversation_id=c.id
                WHERE c.project_id IN ({placeholders}) AND x.kind IN ('start','steer','compact')
                AND x.state IN ('submitted','queued','delivery_unknown') LIMIT 1''', values).fetchone():
                return True
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_goal_commands'").fetchone():
                if self._db.execute(f'''SELECT 1 FROM conversations c JOIN native_goal_commands x ON x.conversation_id=c.id
                    WHERE c.project_id IN ({placeholders}) AND x.state='submitted'
                    AND (json_extract(x.payload_json,'$.action')='resume' OR
                         (json_extract(x.payload_json,'$.action')='set' AND COALESCE(json_extract(x.payload_json,'$.status'),'active')='active'))
                    LIMIT 1''', values).fetchone():
                    return True
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_goal_projection'").fetchone():
                return self._db.execute(f'''SELECT 1 FROM conversations c
                    JOIN native_goal_projection g ON g.conversation_id=c.id
                    WHERE c.project_id IN ({placeholders}) AND g.continuing=1 LIMIT 1''', values).fetchone() is not None
            return False

    def update_conversation_title(
        self, conversation_id: str, title: str, *, manual: bool = True
    ) -> dict[str, Any]:
        value = str(title or "").strip()
        if not value:
            raise AppServerProtocolError("conversation title must be non-empty")
        with self._lock:
            row = self._conversation_row(conversation_id)
            settings = _object(row["settings_json"], {})
            # Legacy manual inputs keep their previous bound. New AI-title
            # conversations must preserve the full generated text on save.
            if not isinstance(settings.get("_ai_initial_title"), dict):
                value = value[:200]
            if manual and isinstance(settings.get("_ai_initial_title"), dict):
                settings["_ai_initial_title"] = {"state": "manual"}
            self._db.execute(
                """
                UPDATE conversations SET title = ?, settings_json = ?, updated_at = ?,
                    revision = revision + 1 WHERE id = ?
                """,
                (value, _json(settings), utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def prepare_initial_ai_title(self, conversation_id: str, command_id: str) -> bool:
        """Durably bind the opt-in to the first user start, never a later turn."""
        with self._lock:
            row = self._conversation_row(conversation_id)
            settings = _object(row["settings_json"], {})
            marker = settings.get("_ai_initial_title")
            command = self._db.execute("SELECT kind,origin FROM commands WHERE id=? AND conversation_id=?",
                                       (command_id, conversation_id)).fetchone()
            if (row["client_origin"] != "mobile_web" or not isinstance(marker, dict)
                    or marker.get("state") != "pending" or command is None
                    or command["kind"] != "start" or command["origin"] != "user"
                    or marker.get("command_id", command_id) != command_id):
                return False
            settings["_ai_initial_title"] = {**marker, "command_id": command_id,
                                            "expected_title": row["title"]}
            self.update_settings(conversation_id, {"_ai_initial_title": settings["_ai_initial_title"]})
            return True

    def reserve_initial_ai_title(self, conversation_id: str, turn_id: str, call_id: str) -> dict[str, Any] | None:
        """Reserve one attempt, protecting same-text manual renames and crashes."""
        with self._lock:
            row = self._conversation_row(conversation_id)
            settings = _object(row["settings_json"], {})
            marker = settings.get("_ai_initial_title")
            if (row["client_origin"] != "mobile_web" or not isinstance(marker, dict)
                    or marker.get("state") != "pending"
                    or row["title"] != marker.get("expected_title")):
                return None
            command = self._db.execute("SELECT result_turn_id FROM commands WHERE id=? AND conversation_id=?",
                                       (marker.get("command_id"), conversation_id)).fetchone()
            turn = self._db.execute("SELECT status FROM turns WHERE id=? AND conversation_id=?",
                                    (turn_id, conversation_id)).fetchone()
            if (command is None or command["result_turn_id"] != turn_id or turn is None
                    or turn["status"] not in ACTIVE_TURN_STATUSES):
                return None
            settings["_ai_initial_title"] = {**marker, "state": "applying", "call_id": call_id}
            self.update_settings(conversation_id, {"_ai_initial_title": settings["_ai_initial_title"]})
            return self.get_conversation(conversation_id)

    def finish_initial_ai_title(self, conversation_id: str, call_id: str, *, title: str | None = None,
                                state: str = "failed") -> bool:
        with self._lock:
            row = self._conversation_row(conversation_id)
            settings = _object(row["settings_json"], {})
            marker = settings.get("_ai_initial_title")
            if (not isinstance(marker, dict) or marker.get("state") != "applying"
                    or marker.get("call_id") != call_id):
                return False
            if title is not None and row["title"] != marker.get("expected_title"):
                return False
            settings["_ai_initial_title"] = {**marker, "state": state}
            self._db.execute("""UPDATE conversations SET title=?,settings_json=?,updated_at=?,
                                 revision=revision+1 WHERE id=?""",
                             (title if title is not None else row["title"], _json(settings), utc_now(), conversation_id))
            return True

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
                   OR native_delete_pending = 1
                   OR json_extract(settings_json, '$._plan_approval_transition.command_id') IS NOT NULL
                   OR EXISTS (
                        SELECT 1 FROM commands
                        WHERE commands.conversation_id = conversations.id
                          AND (commands.state IN ('submitted','delivery_unknown','queued')
                               OR (commands.state = 'accepted' AND commands.kind = 'interrupt'
                                   AND commands.payload_json = '{"scope":"conversation_tree"}'))
                   )
                   OR EXISTS (
                        SELECT 1 FROM async_question_requests
                        WHERE async_question_requests.conversation_id = conversations.id
                          AND async_question_requests.state = 'awaiting_answer'
                          AND async_question_requests.deadline_at IS NOT NULL
                   )
                ORDER BY updated_at ASC
                """
            ).fetchall()
            return [self._decode_conversation(row) for row in rows]

    def delete_conversation(self, conversation_id: str) -> None:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def source_delivery_is_eligible(self, conversation_id: str) -> bool:
        """Reject delayed callbacks for deleted or unresolved-delete sources."""
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            return self._db.execute(
                f"""SELECT 1 FROM conversations c
                    WHERE c.id = ? AND {_NATIVE_DELETE_SETTLED_SQL}""",
                (conversation_id,),
            ).fetchone() is not None

    def conversation_exists(self, conversation_id: str) -> bool:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone() is not None

    def defer_terminal_delivery(
        self, conversation_id: str, turn_id: str, *, live_observed: bool = False
    ) -> bool:
        """Journal real observations, or late callbacks blocked by deletion.

        Wire observations are recorded in their projection transaction before
        any ingestion await. Deletion may begin during that await. Snapshot
        reconciliation can only preserve an existing wire-authorized marker;
        it never creates one, so it cannot backfill Push.
        """
        with self._lock:
            cursor = self._db.execute(
                f"""UPDATE turns AS t SET delete_deferred_terminal=
                        CASE WHEN delete_deferred_terminal<=0 THEN abs(delete_deferred_terminal)+1 ELSE delete_deferred_terminal END,
                        delete_deferred_recovery=CASE WHEN delete_deferred_terminal=0 THEN 0 ELSE delete_deferred_recovery END
                    WHERE t.id=? AND t.conversation_id=?
                      AND t.status IN ('completed','failed','interrupted')
                      AND t.notification_requested=1
                      AND (?=1 OR t.delete_deferred_terminal<>0)
                      AND EXISTS (SELECT 1 FROM conversations c
                          WHERE c.id=t.conversation_id AND c.completion_notification=1
                            AND (?=1 OR NOT ({_NATIVE_DELETE_SETTLED_SQL})))
                      AND ({_UNCLAIMED_TERMINAL_DELIVERY_SQL})""",
                (turn_id, conversation_id, int(live_observed), int(live_observed)),
            )
            return cursor.rowcount == 1

    def release_deferred_terminal_leases(self) -> None:
        # A local callback reservation is not a remote delivery claim.
        # Channel sending/unknown states still prohibit resending after restart.
        with self._lock:
            self._db.execute("""UPDATE turns SET delete_deferred_terminal=abs(delete_deferred_terminal),
                delete_deferred_recovery=1 WHERE delete_deferred_terminal<>0""")

    def finish_deferred_terminal_delivery(self, conversation_id: str, turn_id: str, lease: int) -> None:
        with self._lock:
            self._db.execute(
                f"""UPDATE turns AS t SET delete_deferred_terminal=CASE
                    WHEN t.notification_requested=0 OR NOT EXISTS (
                        SELECT 1 FROM conversations c WHERE c.id=t.conversation_id AND c.completion_notification=1) THEN 0
                    WHEN ({_UNCLAIMED_TERMINAL_DELIVERY_SQL}) OR EXISTS (
                        SELECT 1 FROM conversations c WHERE c.id=t.conversation_id AND NOT ({_NATIVE_DELETE_SETTLED_SQL})) THEN ?
                    ELSE 0 END
                    WHERE t.id=? AND t.conversation_id=? AND t.delete_deferred_terminal=?""",
                (lease + 1, turn_id, conversation_id, -lease),
            )

    def take_deferred_terminal_deliveries(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Reserve known live callbacks once; channel claims still gate sends."""
        with self._lock:
            try:
                self._begin()
                clause = " AND t.conversation_id=?" if conversation_id is not None else ""
                rows = self._db.execute(
                    f"""SELECT t.conversation_id, t.id AS turn_id, t.delete_deferred_terminal AS lease,
                            t.delete_deferred_recovery AS recovery,
                            ({_UNCLAIMED_TERMINAL_DELIVERY_SQL}) AS unclaimed FROM turns t
                        JOIN conversations c ON c.id=t.conversation_id
                        WHERE t.delete_deferred_terminal>0 AND c.completion_notification=1
                          AND t.notification_requested=1 AND {_NATIVE_DELETE_SETTLED_SQL}
                        {clause} ORDER BY t.completed_at, t.ordinal""",
                    (conversation_id,) if conversation_id is not None else (),
                ).fetchall()
                self._db.executemany("UPDATE turns SET delete_deferred_terminal=? WHERE id=?",
                                     [(-r["lease"] if r["unclaimed"] else 0, r["turn_id"]) for r in rows])
                self._finish(None)
                return [{"conversation_id": row["conversation_id"], "turn_id": row["turn_id"],
                         "lease": row["lease"], "recovery": bool(row["recovery"])}
                        for row in rows if row["unclaimed"]]
            except BaseException as error:
                self._finish(error)
                raise

    def configure_completion_notifications(self, enabled: bool) -> None:
        """Remember deployment transitions, not account/bell preferences.

        Re-enabling records a durable command-admission epoch. Native turns
        discovered by *any later recovery* cannot acquire a fresh intent unless
        their local command (or live native-start receipt) belongs to the new
        epoch. Recovery-only receipts never provide this proof. Recovery
        returning normally is deliberately not treated as a success proof.
        Extra schema_meta keys are ignored by older versions on rollback.
        """
        with self._lock:
            try:
                self._begin()
                previous = self._db.execute("SELECT value FROM schema_meta "
                    "WHERE key='completion_notifications_enabled'").fetchone()
                suppress = not enabled or (previous is not None and previous["value"] != "1")
                if suppress:
                    self._db.execute("""UPDATE turns SET notification_requested=0,
                        notification_state=CASE WHEN status IN ('completed','failed','interrupted')
                            THEN 'skipped' ELSE notification_state END,
                        notification_detail='module_disabled'
                        WHERE notification_state IS NULL AND notification_requested=1""")
                if enabled and previous is not None and previous["value"] != "1":
                    self._db.execute("INSERT INTO schema_meta(key,value) "
                        "VALUES('completion_notifications_epoch',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (uuid.uuid4().hex,))
                self._db.execute("INSERT INTO schema_meta(key,value) VALUES('completion_notifications_enabled',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("1" if enabled else "0",))
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise

    def _completion_intent_locked(self, conversation: sqlite3.Row, command: sqlite3.Row | None) -> bool:
        if not conversation['completion_notification']:
            return False
        enabled = self._db.execute("SELECT value FROM schema_meta WHERE "
            "key='completion_notifications_enabled'").fetchone()
        if enabled is not None and enabled['value'] != '1':
            return False
        epoch = self._db.execute("SELECT value FROM schema_meta WHERE "
            "key='completion_notifications_epoch'").fetchone()
        if epoch is None:
            # Existing always-enabled deployments retain native recovery intent.
            return True
        return bool(command is not None and command['completion_notification_epoch'] == epoch['value'])

    def admit_live_completion_receipt(self, conversation_id: str, turn_id: str) -> None:
        """A real turn/started may follow read-before-notification recovery.

        Upgrade only its synthetic recovery receipt, never an old user command
        or a module-disabled/user/Goal opt-out. No execution state is changed.
        """
        self._validate_id(conversation_id, 'conversation')
        self._validate_id(turn_id, 'turn')
        with self._lock:
            try:
                self._begin()
                epoch = self._db.execute("SELECT value FROM schema_meta WHERE key='completion_notifications_epoch'").fetchone()
                conversation = self._conversation_row(conversation_id)
                if epoch is not None:
                    self._db.execute("""UPDATE commands SET completion_notification_epoch=?
                        WHERE conversation_id=? AND result_turn_id=? AND kind='goal_continue' AND origin='system'
                        AND completion_notification_epoch IS NULL AND json_extract(payload_json,'$.native_turn_id')=?
                        AND EXISTS (SELECT 1 FROM turns WHERE id=? AND conversation_id=?
                            AND status IN ('inProgress','active','queued','stopping')
                            AND notification_state IS NULL AND notification_detail='module_unproven')
                        AND EXISTS (SELECT 1 FROM schema_meta WHERE key='completion_notifications_enabled' AND value='1')""",
                        (epoch['value'], conversation_id, turn_id, turn_id, turn_id, conversation_id))
                    proof = self._db.execute("SELECT completion_notification_epoch FROM commands WHERE conversation_id=? "
                        "AND result_turn_id=? AND completion_notification_epoch=? LIMIT 1",
                        (conversation_id, turn_id, epoch['value'])).fetchone()
                    if proof is not None and self._completion_intent_locked(conversation, proof):
                        self._promote_completion_intent_locked(conversation_id, turn_id)
                self._finish(None)
            except BaseException as error:
                self._finish(error)
                raise

    def _promote_completion_intent_locked(self, conversation_id: str, turn_id: str) -> None:
        # Attribution can arrive through bind_turn OR a later thread/read. Only
        # the same epoch's proven command may promote this provisional intent.
        # User/Goal opt-outs and already claimed deliveries are never replaced.
        self._db.execute("UPDATE turns SET notification_requested=1,notification_detail=NULL "
            "WHERE id=? AND conversation_id=? AND notification_requested=0 "
            "AND notification_state IS NULL AND notification_detail='module_unproven' "
            "AND status IN ('inProgress','active','queued','stopping')", (turn_id, conversation_id))

    def skip_completion_notification(self, conversation_id: str, turn_id: str) -> None:
        """A disabled channel consumes no delivery claim and cannot replay later."""
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        with self._lock:
            self._db.execute("""UPDATE turns SET notification_state='skipped',
                notification_detail='module_disabled'
                WHERE id=? AND conversation_id=? AND notification_state IS NULL
                  AND status IN ('completed','failed','interrupted')""", (turn_id, conversation_id))

    def claim_completion_notification(
        self, conversation_id: str, turn_id: str, *, recovery: bool = False
    ) -> bool:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE turns AS t SET notification_state = 'sending'
                WHERE t.id = :turn_id AND t.conversation_id = :conversation_id
                  AND """ + _COMPLETION_NOTIFICATION_ELIGIBILITY,
                {"turn_id": turn_id, "conversation_id": conversation_id,
                 "now": utc_now(), "recovery": int(recovery)},
            )
            return cursor.rowcount == 1

    def release_incomplete_notifications(self) -> int:
        """Quarantine an uncertain send; retrying could duplicate delivery.

        The remote sender has no idempotency contract.  Persisted claim-before-
        send therefore deliberately provides at-most-once attempts, not exactly
        once delivery.  A crash before the HTTP request may lose one notice.
        """
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE turns SET notification_state = 'unknown',
                    notification_detail = 'delivery_uncertain_after_restart'
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
                WHERE """ + _COMPLETION_NOTIFICATION_ELIGIBILITY + """
                ORDER BY t.completed_at ASC, t.ordinal ASC
                """,
                {"now": utc_now(), "recovery": 1},
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
        self, conversation_id: str, turn_id: str, account_id: str, *, resume_pending: bool = False
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
                    f"""
                    SELECT 1 FROM turns t
                    JOIN conversations c ON c.id = t.conversation_id
                    WHERE t.id = ? AND t.conversation_id = ?
                      AND t.status IN ('completed','failed','interrupted')
                      AND t.notification_requested = 1
                      AND c.completion_notification = 1
                      AND {_NATIVE_DELETE_SETTLED_SQL}
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
                    if resume_pending:
                        rows = self._db.execute(
                            """SELECT d.*, s.endpoint, s.origin, s.p256dh, s.auth
                                FROM web_push_deliveries d JOIN web_push_subscriptions s ON s.id=d.subscription_id
                                JOIN web_push_dispatches p ON p.turn_id=d.turn_id
                                WHERE d.turn_id=? AND d.conversation_id=? AND d.state='pending'
                                  AND p.account_id=? AND s.account_id=? AND s.enabled=1""",
                            (turn_id, conversation_id, account, account),
                        ).fetchall()
                        self._finish(None)
                        return [dict(row) for row in rows]
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
                f"""
                UPDATE web_push_deliveries SET state = 'sending', updated_at = ?
                WHERE turn_id = ? AND subscription_id = ? AND state = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM conversations c
                      WHERE c.id = web_push_deliveries.conversation_id
                        AND c.completion_notification = 1
                        AND {_NATIVE_DELETE_SETTLED_SQL}
                  )
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
        native_delete_pending: bool | None = None,
    ) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        self._validate_state(state, CONVERSATION_STATES, "conversation")
        updates = ["state = ?", "updated_at = ?", "revision = revision + 1"]
        values: list[Any] = [state, utc_now()]
        if native_delete_pending is not None:
            updates.append("native_delete_pending = ?")
            values.append(int(native_delete_pending))
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
        origin: str = "user",
        notification_admitted: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_id(conversation_id, "conversation")
        self._validate_id(command_id, "command")
        if target_turn_id is not None:
            self._validate_id(target_turn_id, "turn")
        if kind not in {"start", "steer", "interrupt", "settings", "compact", "goal_continue"}:
            raise AppServerProtocolError("unsupported conversation command")
        if origin not in {"user", "system", "scheduled"}:
            raise AppServerProtocolError("conversation command origin is invalid")
        if STEER_ONLY in (payload or {}):
            raise AppServerProtocolError("reserved maintenance command field")
        payload_json = _json(payload or {})
        now = utc_now()
        with self._lock, self.maintenance_admission.admission() as admission:
            self._conversation_row(conversation_id)
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_goal_commands'").fetchone() and self._db.execute("SELECT 1 FROM native_goal_commands WHERE id=?", (command_id,)).fetchone():
                raise AppServerProtocolError("command ID was reused with new input")
            existing = self._db.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["conversation_id"] != conversation_id
                    or existing["kind"] != kind
                    or existing["text"] != text
                    or _json({k: v for k, v in _object(existing["payload_json"], {}).items() if k != STEER_ONLY}) != payload_json
                    or (existing["origin"] if "origin" in existing.keys() else "user") != origin
                ):
                    raise AppServerProtocolError("command ID was reused with new input")
                return self._decode_command(existing), False
            conversation = self._conversation_row(conversation_id)
            if kind in {'start', 'steer', 'compact', 'settings'} and conversation['project_id'] in self._project_deletes:
                raise AppServerProtocolError('project deletion is in progress')
            rejected = False
            if admission['mode'] != 'open' and kind in {'start', 'steer', 'compact'}:
                data = dict(payload or {})
                steer_live = (kind == 'steer' and origin == 'user'
                    and conversation['state'] == 'active' and conversation['active_turn_id']
                    and not data.get('force_new_turn') and not data.get('goal_seed')
                    and not data.get('plan_confirmation') and not data.get('initial_goal')
                    and not _object(conversation['settings_json'], {}).get('pending_for_next_turn'))
                scheduled_admitted = False
                if origin == 'scheduled' and self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_automation_runs'").fetchone():
                    scheduled_admitted = bool(self._db.execute("SELECT 1 FROM conversation_automation_runs WHERE command_id=? AND conversation_id=? AND status='dispatching'", (command_id, conversation_id)).fetchone())
                rejected = not (steer_live or scheduled_admitted)
                if steer_live:
                    data[STEER_ONLY] = conversation['active_turn_id']
                    payload_json = _json(data)
            self._db.execute(
                """
                INSERT INTO commands(
                    id, conversation_id, kind, state, target_turn_id, text,
                    payload_json, origin, created_at, updated_at, reason, completion_notification_epoch
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    conversation_id,
                    kind,
                    'rejected' if rejected else 'submitted',
                    target_turn_id,
                    text,
                    payload_json,
                    origin,
                    now,
                    now,
                    'maintenance_quiescing' if rejected else None,
                    (self._db.execute("SELECT value FROM schema_meta WHERE key='completion_notifications_epoch'").fetchone() or [None])[0] if notification_admitted else None,
                ),
            )
            if kind in {"start", "steer", "compact"}:
                self._db.execute(
                    "UPDATE conversations SET activity_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return self.command(command_id), not rejected

    def command(self, command_id: str) -> dict[str, Any]:
        self._validate_id(command_id, "command")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("command does not exist")
            return self._decode_command(row)

    # Async AgentMessage questions -------------------------------------------
    # Unlike item/tool/requestUserInput these are ordinary user messages from
    # the App Server.  The Web host owns only its absolute deadline and the
    # one, idempotently claimed reply; the Turn remains native authority.

    def _register_async_questions_locked(
        self,
        conversation_id: str,
        turn_id: str,
        native_item_id: str,
        questions: list[dict[str, Any]],
        *,
        now: str,
    ) -> bool:
        conversation = self._conversation_row(conversation_id)
        thread_id = conversation["thread_id"]
        if not isinstance(thread_id, str) or not thread_id:
            return False
        turn = self._db.execute(
            "SELECT status FROM turns WHERE id = ? AND conversation_id = ?",
            (turn_id, conversation_id),
        ).fetchone()
        if turn is None or turn["status"] in TERMINAL_TURN_STATUSES:
            return False
        changed = False
        scheduled = self._db.execute("SELECT 1 FROM commands WHERE conversation_id=? AND result_turn_id=? AND origin='scheduled' LIMIT 1",(conversation_id,turn_id)).fetchone() is not None
        deadline = (
            datetime.now(timezone.utc) + timedelta(milliseconds=ASYNC_AUTO_WAIT_MS)
        ).isoformat()
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            options = question.get("options")
            recommended = options[0] if isinstance(options, list) and options else None
            # Free-text-only AgentMessage fields have no native recommendation
            # and retain their existing manual reply behaviour without a timer.
            request_deadline = deadline if isinstance(recommended, str) and not scheduled else None
            existing = self._db.execute(
                """SELECT id FROM async_question_requests
                   WHERE conversation_id = ? AND turn_id = ?
                     AND native_item_id = ? AND question_index = ?""",
                (conversation_id, turn_id, native_item_id, index),
            ).fetchone()
            if existing is not None:
                continue
            request_id = "aq" + uuid.uuid4().hex
            self._db.execute(
                """INSERT INTO async_question_requests(
                    id, conversation_id, thread_id, turn_id, native_item_id,
                    question_index, question_json, recommended_answer,
                    deadline_at, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_answer', ?, ?)""",
                (
                    request_id, conversation_id, thread_id, turn_id,
                    native_item_id, index, _json(question), recommended,
                    request_deadline, now, now,
                ),
            )
            changed = True
        if changed:
            self._touch_question_conversation_locked(conversation_id, now)
        return changed

    def pending_async_question_requests(
        self, conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            values: list[Any] = []
            where = "WHERE state = 'awaiting_answer' AND deadline_at IS NOT NULL"
            if conversation_id is not None:
                self._validate_id(conversation_id, "conversation")
                where += " AND conversation_id = ?"
                values.append(conversation_id)
            rows = self._db.execute(
                "SELECT * FROM async_question_requests " + where
                + " ORDER BY deadline_at ASC, created_at ASC",
                values,
            ).fetchall()
            return [self._decode_async_question(row) for row in rows]

    def async_question_request(self, request_id: str) -> dict[str, Any]:
        self._validate_id(request_id, "async question")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM async_question_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("async question does not exist")
            return self._decode_async_question(row)

    def claim_async_question_answer(
        self,
        conversation_id: str,
        request_id: str,
        *,
        command_id: str,
        answer_text: str,
        source: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically let a manual answer or the host deadline win once."""
        self._validate_id(conversation_id, "conversation")
        self._validate_id(request_id, "async question")
        self._validate_id(command_id, "command")
        if source not in {"user", "deadline"}:
            raise AppServerProtocolError("async question answer source is invalid")
        if not isinstance(answer_text, str) or not answer_text.strip():
            raise AppServerProtocolError("async question answer is invalid")
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                row = self._db.execute(
                    "SELECT * FROM async_question_requests WHERE id = ?", (request_id,)
                ).fetchone()
                if row is None or row["conversation_id"] != conversation_id:
                    raise AppServerProtocolError("async question does not belong to conversation")
                if row["command_id"] is not None:
                    if row["command_id"] != command_id:
                        raise AppServerProtocolError("async question is no longer awaiting an answer")
                    self._finish(None)
                    return self._decode_async_question(row), False
                if row["state"] != "awaiting_answer":
                    raise AppServerProtocolError("async question is no longer awaiting an answer")
                # A new claim creates future native work; a replay above
                # does not. Preserve the choice during unresolved deletion,
                # including an old deadline armed before the intent changed.
                if not self.source_delivery_is_eligible(conversation_id):
                    raise AppServerProtocolError("conversation is pending native deletion")
                effective_source = source
                effective_answer = answer_text.strip()
                if source == "user" and self._deadline_passed(row["deadline_at"]):
                    effective_source = "deadline"
                    effective_answer = str(row["recommended_answer"] or "").strip()
                if effective_source == "deadline" and not effective_answer:
                    raise AppServerProtocolError("async question has no recommended answer")
                self._db.execute(
                    """UPDATE async_question_requests
                       SET state = 'answer_claimed', answer_source = ?,
                           answer_text = ?, command_id = ?, updated_at = ?
                       WHERE id = ?""",
                    (effective_source, effective_answer, command_id, now, request_id),
                )
                self._touch_question_conversation_locked(conversation_id, now)
                updated = self._db.execute(
                    "SELECT * FROM async_question_requests WHERE id = ?", (request_id,)
                ).fetchone()
                self._finish(None)
                return self._decode_async_question(updated), True
            except BaseException as error:
                self._finish(error)
                raise

    def mark_async_question_delivery(
        self, request_id: str, *, command_id: str, command_state: str | None
    ) -> dict[str, Any]:
        self._validate_id(request_id, "async question")
        self._validate_id(command_id, "command")
        if command_state not in COMMAND_STATES:
            raise AppServerProtocolError("async question command state is invalid")
        state = {
            "accepted": "answered", "completed": "answered", "queued": "answered",
            "delivery_unknown": "delivery_unknown", "rejected": "rejected",
            "submitted": "answer_claimed",
        }[command_state]
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM async_question_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("async question does not exist")
            if row["command_id"] != command_id:
                raise AppServerProtocolError("async question command does not match")
            if row["state"] == "answer_claimed":
                now = utc_now()
                self._db.execute(
                    """UPDATE async_question_requests
                       SET state = ?, resolved_at = CASE WHEN ? = 'answered'
                           THEN ? ELSE resolved_at END, updated_at = ?
                       WHERE id = ?""",
                    (state, state, now, now, request_id),
                )
                self._touch_question_conversation_locked(row["conversation_id"], now)
                row = self._db.execute(
                    "SELECT * FROM async_question_requests WHERE id = ?", (request_id,)
                ).fetchone()
            return self._decode_async_question(row)

    # Native requestUserInput -------------------------------------------------
    # These records deliberately do not contain a secret answer.  The runtime
    # holds a secret only until it writes the native JSON-RPC response.

    def register_question_request(
        self,
        conversation_id: str,
        *,
        thread_id: str,
        turn_id: str,
        native_item_id: str,
        request_id_json: str,
        request_id_type: str,
        connection_epoch: str,
        fingerprint: str,
        questions: list[dict[str, Any]],
        deadline_at: str | None,
        deadline_source: str | None,
        is_blocking: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Create an opaque request, or bind an allowed native reissue.

        A persisted request is never replayed after a process/connection loss.
        Only a fresh native request with the same logical fingerprint may bind
        it to a new RPC id and connection epoch.
        """
        for value, label in ((conversation_id, "conversation"), (thread_id, "thread"),
                             (turn_id, "turn"), (native_item_id, "native item")):
            self._validate_id(value, label)
        if request_id_type not in {"string", "integer"} or not isinstance(connection_epoch, str) or not connection_epoch or len(connection_epoch) > 160:
            raise AppServerProtocolError("question request identity is invalid")
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                conversation = self._conversation_row(conversation_id)
                turn = self._db.execute(
                    "SELECT conversation_id, status FROM turns WHERE id = ?", (turn_id,)
                ).fetchone()
                if turn is None or turn["conversation_id"] != conversation_id:
                    raise AppServerProtocolError("question request turn is unknown")
                if turn["status"] in TERMINAL_TURN_STATUSES:
                    raise AppServerProtocolError("question request turn has ended")
                exact = self._db.execute(
                    """SELECT * FROM question_requests WHERE thread_id = ?
                       AND request_id_json = ? AND request_id_type = ?
                       AND connection_epoch = ?""",
                    (thread_id, request_id_json, request_id_type, connection_epoch),
                ).fetchone()
                if exact is not None:
                    if (exact["conversation_id"] != conversation_id
                            or exact["fingerprint"] != fingerprint
                            or exact["turn_id"] != turn_id
                            or exact["native_item_id"] != native_item_id):
                        raise AppServerProtocolError("question request ID was reused")
                    self._finish(None)
                    return self._decode_question(exact), False
                reissue = self._db.execute(
                    """SELECT * FROM question_requests WHERE conversation_id = ?
                       AND thread_id = ? AND turn_id = ? AND native_item_id = ?
                       AND fingerprint = ?
                       AND state IN ('awaiting_reissue','delivery_unknown','expired')
                       ORDER BY created_at DESC LIMIT 1""",
                    (conversation_id, thread_id, turn_id, native_item_id, fingerprint),
                ).fetchone()
                if reissue is not None:
                    # Reissue is a new native RPC. Even a deadline that has
                    # passed must be claimed through the normal empty-answer
                    # path so the writer can acknowledge its actual send.
                    state = "awaiting_answer"
                    self._db.execute(
                        """UPDATE question_requests SET native_item_id = ?, request_id_json = ?,
                           request_id_type = ?, connection_epoch = ?, questions_json = ?,
                           deadline_source = ?, state = ?, attempt = attempt + 1,
                           resolved_at = NULL, updated_at = ? WHERE id = ?""",
                        (native_item_id, request_id_json, request_id_type, connection_epoch,
                         _json(questions), deadline_source, state, now, reissue["id"]),
                    )
                    self._touch_question_conversation_locked(conversation_id, now)
                    row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (reissue["id"],)).fetchone()
                    self._finish(None)
                    return self._decode_question(row), True
                question_id = "q" + uuid.uuid4().hex
                self._db.execute(
                    """INSERT INTO question_requests(
                        id, conversation_id, thread_id, turn_id, native_item_id,
                        request_id_json, request_id_type, connection_epoch, fingerprint,
                        questions_json, received_at, deadline_at, deadline_source, is_blocking, state,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_answer', ?, ?)""",
                    (question_id, conversation_id, thread_id, turn_id, native_item_id,
                     request_id_json, request_id_type, connection_epoch, fingerprint,
                     _json(questions), now, deadline_at, deadline_source, int(is_blocking), now, now),
                )
                self._touch_question_conversation_locked(conversation_id, now)
                row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
                self._finish(None)
                return self._decode_question(row), True
            except BaseException as error:
                self._finish(error)
                raise

    def claim_question_answer(
        self, conversation_id: str, question_id: str, *, command_id: str,
        answers: dict[str, str], source: str, answers_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically let a browser or deadline win one request."""
        self._validate_id(conversation_id, "conversation")
        self._validate_id(question_id, "question")
        self._validate_id(command_id, "command")
        if source not in {"user", "deadline"}:
            raise AppServerProtocolError("question answer source is invalid")
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
                if row is None or row["conversation_id"] != conversation_id:
                    raise AppServerProtocolError("question does not belong to conversation")
                previous = self._db.execute("SELECT * FROM question_commands WHERE command_id = ?", (command_id,)).fetchone()
                if previous is not None:
                    if previous["question_id"] != question_id or previous["answers_digest"] != answers_digest:
                        raise AppServerProtocolError("command ID was reused with new answer")
                    if previous["attempt"] != row["attempt"]:
                        raise AppServerProtocolError("question was reissued; submit a new answer command")
                    self._finish(None)
                    return self._decode_question(row), False
                if row["state"] != "awaiting_answer":
                    raise AppServerProtocolError("question is no longer awaiting an answer")
                turn = self._db.execute("SELECT status FROM turns WHERE id = ?", (row["turn_id"],)).fetchone()
                if turn is None or turn["status"] in TERMINAL_TURN_STATUSES:
                    raise AppServerProtocolError("question turn is no longer active")
                if source == "user" and self._deadline_passed(row["deadline_at"]):
                    # The expiry wins inside the transaction, so a click at the
                    # boundary cannot send a different answer than the journal.
                    source = "deadline"
                    answers = recommended_answers(_object(row["questions_json"], []))
                    answers_digest = answer_digest(
                        answers,
                        contains_secret=any(
                            item.get("isSecret")
                            for item in _object(row["questions_json"], [])
                            if isinstance(item, dict)
                        ),
                    )
                if source == "deadline" and row["deadline_at"] is None:
                    raise AppServerProtocolError("question has no automatic deadline")
                public_answers = self._screened_answers(row["questions_json"], answers)
                self._db.execute(
                    """INSERT INTO question_commands(command_id, question_id, answers_digest, attempt, created_at)
                       VALUES(?, ?, ?, ?, ?)""", (command_id, question_id, answers_digest, row["attempt"], now)
                )
                self._db.execute(
                    """UPDATE question_requests SET state = 'answer_claimed', answer_source = ?,
                       answers_json = ?, answers_digest = ?, updated_at = ? WHERE id = ?""",
                    (source, _json(public_answers), answers_digest, now, question_id),
                )
                self._touch_question_conversation_locked(conversation_id, now)
                updated = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
                self._finish(None)
                return self._decode_question(updated), True
            except BaseException as error:
                self._finish(error)
                raise

    def mark_question_response_sent(self, question_id: str) -> dict[str, Any]:
        """Record wire completion, including a resolved-notification race."""
        self._validate_id(question_id, "question")
        with self._lock:
            row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
            if row is None:
                raise AppServerProtocolError("question does not exist")
            if row["state"] in {"answer_claimed", "resolved_before_send"}:
                now = utc_now()
                state = "awaiting_activity" if row["state"] == "resolved_before_send" else "response_sent"
                self._db.execute("UPDATE question_requests SET state = ?, updated_at = ? WHERE id = ?", (state, now, question_id))
                self._touch_question_conversation_locked(row["conversation_id"], now)
                row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
            return self._decode_question(row)

    def mark_question_resolved(
        self, thread_id: str, request_id_json: str, request_id_type: str,
        connection_epoch: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(connection_epoch, str) or not connection_epoch:
            return None
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM question_requests WHERE thread_id = ? AND request_id_json = ?
                   AND request_id_type = ? AND connection_epoch = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (thread_id, request_id_json, request_id_type, connection_epoch),
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            if row["state"] == "response_sent":
                self._db.execute("UPDATE question_requests SET state = 'awaiting_activity', resolved_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]))
            elif row["state"] == "answer_claimed":
                # Socket send completed locally but the on-sent coroutine has
                # not yet been scheduled. Keep the race explicit rather than
                # incorrectly calling a sent answer "cleared".
                self._db.execute("UPDATE question_requests SET state = 'resolved_before_send', resolved_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]))
            elif row["state"] not in {"answered", "expired", "awaiting_activity", "resolved_before_send"}:
                self._db.execute("UPDATE question_requests SET state = 'closed', resolved_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]))
            self._touch_question_conversation_locked(row["conversation_id"], now)
            return self._decode_question(self._db.execute("SELECT * FROM question_requests WHERE id = ?", (row["id"],)).fetchone())

    def note_question_turn_activity(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        now = utc_now()
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM question_requests WHERE thread_id = ? AND turn_id = ?
                   AND state = 'awaiting_activity'""", (thread_id, turn_id),
            ).fetchall()
            if not rows:
                return []
            self._db.execute("UPDATE question_requests SET state = 'answered', updated_at = ? WHERE thread_id = ? AND turn_id = ? AND state = 'awaiting_activity'", (now, thread_id, turn_id))
            for row in rows:
                self._touch_question_conversation_locked(row["conversation_id"], now)
            return [self._decode_question(self._db.execute("SELECT * FROM question_requests WHERE id = ?", (row["id"],)).fetchone()) for row in rows]

    def close_questions_for_turn(self, thread_id: str, turn_id: str) -> list[str]:
        now = utc_now()
        with self._lock:
            rows = self._db.execute("SELECT id, conversation_id FROM question_requests WHERE thread_id = ? AND turn_id = ? AND state IN ('awaiting_answer','answer_claimed','resolved_before_send','response_sent','awaiting_activity','awaiting_reissue','delivery_unknown','expired')", (thread_id, turn_id)).fetchall()
            if rows:
                self._db.execute("UPDATE question_requests SET state = 'closed', resolved_at = COALESCE(resolved_at, ?), updated_at = ? WHERE thread_id = ? AND turn_id = ? AND state IN ('awaiting_answer','answer_claimed','resolved_before_send','response_sent','awaiting_activity','awaiting_reissue','delivery_unknown','expired')", (now, now, thread_id, turn_id))
                for row in rows:
                    self._touch_question_conversation_locked(row["conversation_id"], now)
            return [row["id"] for row in rows]

    def recover_question_requests(self) -> list[dict[str, Any]]:
        """Do not claim old native RPCs survive a Web-process restart."""
        now = utc_now()
        with self._lock:
            rows = self._db.execute("SELECT * FROM question_requests WHERE state IN ('awaiting_answer','answer_claimed','resolved_before_send','response_sent','awaiting_activity')").fetchall()
            for row in rows:
                state = "expired" if self._deadline_passed(row["deadline_at"]) else "awaiting_reissue"
                self._db.execute("UPDATE question_requests SET state = ?, updated_at = ? WHERE id = ?", (state, now, row["id"]))
                self._touch_question_conversation_locked(row["conversation_id"], now)
            return [self._decode_question(self._db.execute("SELECT * FROM question_requests WHERE id = ?", (row["id"],)).fetchone()) for row in rows]

    def mark_questions_connection_lost(self, connection_epoch: str) -> list[dict[str, Any]]:
        """Detach unanswered native RPCs; do not replay them on reconnect."""
        if not isinstance(connection_epoch, str) or not connection_epoch:
            return []
        now = utc_now()
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM question_requests WHERE connection_epoch = ?
                   AND state IN ('awaiting_answer','answer_claimed','resolved_before_send',
                                 'response_sent','awaiting_activity')""",
                (connection_epoch,),
            ).fetchall()
            for row in rows:
                state = "awaiting_reissue" if row["state"] == "awaiting_answer" else "delivery_unknown"
                self._db.execute(
                    "UPDATE question_requests SET state = ?, updated_at = ? WHERE id = ?",
                    (state, now, row["id"]),
                )
                self._touch_question_conversation_locked(row["conversation_id"], now)
            return [self._decode_question(self._db.execute("SELECT * FROM question_requests WHERE id = ?", (row["id"],)).fetchone()) for row in rows]

    def question(self, question_id: str) -> dict[str, Any]:
        self._validate_id(question_id, "question")
        with self._lock:
            row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
            if row is None:
                raise AppServerProtocolError("question does not exist")
            return self._decode_question(row)

    @staticmethod
    def _deadline_passed(deadline_at: str | None) -> bool:
        if not deadline_at:
            return False
        try:
            value = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
            return value <= datetime.now(timezone.utc)
        except ValueError:
            return True

    @staticmethod
    def _screened_answers(questions_json: str, answers: dict[str, str]) -> dict[str, str | None]:
        questions = _object(questions_json, [])
        secret_ids = {item.get("id") for item in questions if isinstance(item, dict) and item.get("isSecret")}
        return {key: None if key in secret_ids else value for key, value in answers.items()}

    def _set_question_state(self, question_id: str, allowed: set[str], state: str) -> dict[str, Any]:
        self._validate_id(question_id, "question")
        with self._lock:
            row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
            if row is None:
                raise AppServerProtocolError("question does not exist")
            if row["state"] in allowed:
                now = utc_now()
                self._db.execute("UPDATE question_requests SET state = ?, updated_at = ? WHERE id = ?", (state, now, question_id))
                self._touch_question_conversation_locked(row["conversation_id"], now)
                row = self._db.execute("SELECT * FROM question_requests WHERE id = ?", (question_id,)).fetchone()
            return self._decode_question(row)

    def _touch_question_conversation_locked(self, conversation_id: str, now: str) -> None:
        self._db.execute("UPDATE conversations SET updated_at = ?, revision = revision + 1 WHERE id = ?", (now, conversation_id))

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
            managed_transaction = not self._db.in_transaction
            if managed_transaction:
                self._begin()
            error = None
            try:
                current = self._db.execute(
                    "SELECT state, result_turn_id, payload_json FROM commands WHERE id = ?", (command_id,)
                ).fetchone()
                if current is None:
                    raise AppServerProtocolError("command does not exist")
                if current["state"] == "completed" and state != "completed":
                    return self.command(command_id)
                if (state == "queued" and self._db.execute("SELECT 1 FROM schema_meta "
                        "WHERE key='message_attachments_rollback_barrier'").fetchone()
                        and json.loads(current["payload_json"]).get("attachments")):
                    state, reason = "rejected", "attachment_rollback_not_delivered"
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
            except BaseException as exception:
                error = exception
                raise
            finally:
                if managed_transaction:
                    self._finish(error)

    def tree_stop_commands(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM commands WHERE conversation_id=? AND kind='interrupt' "
                "ORDER BY created_at DESC, rowid DESC", (conversation_id,),
            ).fetchall()
            return [self._decode_command(row) for row in rows
                    if _object(row['payload_json'], {}).get('scope') == 'conversation_tree']

    def effective_tree_stop(self, conversation_id: str) -> dict[str, Any] | None:
        commands = self.tree_stop_commands(conversation_id)
        # A rejected retry does not release a previous uncertain stop intent.
        return next((c for c in commands if c['state'] in
                    {'submitted', 'accepted', 'delivery_unknown'}), commands[0] if commands else None)

    def pending_tree_stop(self, conversation_id: str) -> bool:
        from engine.conversation_stop import pending_tree_stop
        return pending_tree_stop(self.effective_tree_stop(conversation_id))

    def tree_stop_owned_reviews(self, conversation_id: str) -> set[str]:
        from engine.conversation_stop import _RESULT_PREFIX
        ids: set[str] = set()
        for command in self.tree_stop_commands(conversation_id):
            reason = command.get('reason') or ''
            if command['state'] in {'submitted', 'accepted', 'delivery_unknown'} and reason.startswith(_RESULT_PREFIX):
                try:
                    values = json.loads(reason[len(_RESULT_PREFIX):]).get('owned_review_threads', [])
                    if isinstance(values, list):
                        ids.update(value for value in values if isinstance(value, str))
                except (ValueError, TypeError):
                    pass
        return ids

    def capture_tree_stop_reviews(self, conversation_id: str, thread_ids: set[str]) -> None:
        from engine.conversation_stop import _RESULT_PREFIX
        with self._lock:
            ids = self.tree_stop_owned_reviews(conversation_id) | thread_ids
            for command in self.tree_stop_commands(conversation_id):
                if command['state'] not in {'submitted', 'accepted', 'delivery_unknown'}:
                    continue
                reason = command.get('reason') or ''
                try:
                    details = json.loads(reason[len(_RESULT_PREFIX):]) if reason.startswith(_RESULT_PREFIX) else {}
                except (ValueError, TypeError):
                    details = {}
                details.update(status='unconfirmed', owned_review_threads=sorted(ids))
                self.update_command(command['id'], command['state'],
                    reason=_RESULT_PREFIX + json.dumps(details, separators=(',', ':')))

    def tree_stop_is_current(self, conversation_id: str, *, native_turn_ids=None) -> bool:
        from engine.conversation_stop import _RESULT_PREFIX
        command = self.effective_tree_stop(conversation_id)
        if not command or command['state'] != 'completed':
            return False
        reason = command.get('reason') or ''
        if not reason.startswith(_RESULT_PREFIX):
            return False
        try:
            covered = json.loads(reason[len(_RESULT_PREFIX):]).get('root_turn_ids')
        except (ValueError, TypeError):
            return False
        if not isinstance(covered, list) or not all(isinstance(t, str) for t in covered):
            return False
        with self._lock:
            current_ids = set(native_turn_ids) if native_turn_ids is not None else {
                row[0] for row in self._db.execute('SELECT id FROM turns WHERE conversation_id=?', (conversation_id,))}
            if not current_ids.issubset(set(covered)):
                return False
            later = self._db.execute(
                "SELECT 1 FROM commands WHERE conversation_id=? AND kind IN ('start','steer','goal_continue') "
                "AND state IN ('accepted','completed') AND created_at>? LIMIT 1",
                (conversation_id, command['updated_at']),
            ).fetchone()
            return later is None

    def tree_stop_covers_turn(self, conversation_id: str, turn_id: str) -> bool:
        from engine.conversation_stop import _RESULT_PREFIX
        # Coverage is historical: old questions stay held even after a later
        # explicit user turn invalidates the current-tree stop proof.
        for command in self.tree_stop_commands(conversation_id):
            reason = command.get('reason') or ''
            if command['state'] == 'completed' and reason.startswith(_RESULT_PREFIX):
                try:
                    if turn_id in json.loads(reason[len(_RESULT_PREFIX):]).get('root_turn_ids', []):
                        return True
                except (ValueError, TypeError):
                    continue
        return False

    def cancel_stop_queue(self, conversation_id: str) -> None:
        # Keep message text and attachment references for manual recovery.
        with self._lock:
            self._db.execute(
                "UPDATE commands SET state='rejected', reason='conversation_tree_stopped', "
                "updated_at=? WHERE conversation_id=? AND state='queued'",
                (utc_now(), conversation_id),
            )

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
                            int(self._completion_intent_locked(conversation, command)),
                        ),
                    )
                elif self._completion_intent_locked(conversation, command):
                    self._promote_completion_intent_locked(conversation_id, turn_id)
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
        # Record the per-turn decision at ingress, in the same transaction as
        # the terminal itself, not later when asynchronous delivery runs.
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_goal_projection'").fetchone():
            goal = self._db.execute('SELECT continuing,notification_floor,draft_json FROM native_goal_projection WHERE conversation_id=?', (conversation_id,)).fetchone()
            if goal:
                draft = json.loads(goal['draft_json']) if goal['draft_json'] else None
                in_cycle = goal['continuing'] and goal['notification_floor'] is not None and terminal['ordinal'] >= goal['notification_floor']
                if in_cycle or (draft and draft.get('status') == 'active'):
                    self._db.execute("UPDATE turns SET notification_requested=0,notification_detail='goal_continuing' WHERE conversation_id=? AND id=? AND notification_state IS NULL", (conversation_id, turn_id))
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
            native_turn = params.get("turn")
            live_terminal = (
                method == "turn/completed" and turn_id is not None
                and isinstance(native_turn, dict)
                and native_turn.get("status") in TERMINAL_TURN_STATUSES
            )
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
                    # A read snapshot can have recorded completion before the
                    # actual wire callback. Preserve the observation without
                    # replaying its projection or settling a newer turn.
                    if live_terminal:
                        observed = self.defer_terminal_delivery(
                            conversation_id, turn_id, live_observed=True
                        )
                        return {"changed": False, "conversation_id": conversation_id,
                                "turn_id": turn_id, "terminal_delivery_observed": observed}
                    return {"changed": False, "conversation_id": conversation_id}
            try:
                self._begin()
                if live_terminal and conversation["recovery_state"] in {
                    "deleting_native_thread", "delete_delivery_unknown",
                    "native_thread_deleted", "native_thread_absent",
                }:
                    # Terminal projection clears transient recovery labels.
                    # Promote an old intent before that happens, atomically.
                    self._db.execute(
                        "UPDATE conversations SET native_delete_pending=1 WHERE id=?",
                        (conversation_id,),
                    )
                result = self._apply_notification_locked(
                    conversation_id, method, params, turn_id
                )
                if live_terminal:
                    result["terminal_delivery_observed"] = self.defer_terminal_delivery(
                        conversation_id, turn_id, live_observed=True
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
                tier = settings_value.get("serviceTier")
                # Native thread/settings/update acknowledges Standard as the
                # enum "default". Web/model catalogs represent it as None;
                # retaining the enum would fail the next turn/start preflight.
                settings["service_tier"] = None if tier == 'default' else tier
                self._db.execute(
                    "UPDATE conversations SET settings_json = ? WHERE id = ?",
                    (_json(settings), conversation_id),
                )
                changed = True
        elif method == "internal/context-switch/observed" and turn_id is not None:
            changed = self._record_context_switch_locked(
                conversation_id,
                turn_id,
                call_key_sha256=params.get("callKeySha256"),
                state=params.get("state"),
                server_version=params.get("serverVersion"),
                now=now,
            )
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
                    delivery=None,
                    questions=None,
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

    def _record_context_switch_locked(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        call_key_sha256: Any,
        state: Any,
        server_version: Any,
        now: str,
    ) -> bool:
        """Persist the raw-free lifecycle projection of one new_context call."""
        if (
            not isinstance(call_key_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", call_key_sha256) is None
            or state not in {"call_seen", "output_seen"}
            or not isinstance(server_version, str)
            or not server_version
            or len(server_version) > 120
        ):
            return False
        previous = self._db.execute(
            """
            SELECT call_observed_at, output_observed_at, server_version
            FROM context_switch_events
            WHERE conversation_id = ? AND turn_id = ? AND call_key_sha256 = ?
            """,
            (conversation_id, turn_id, call_key_sha256),
        ).fetchone()
        if previous is None:
            # An output is meaningful only after this process observed the
            # matching call. Do not reconstruct or infer records after a
            # reconnect from any non-raw source.
            if state != "call_seen":
                return False
            self._db.execute(
                """
                INSERT INTO context_switch_events(
                    conversation_id, turn_id, call_key_sha256,
                    call_observed_at, output_observed_at, server_version
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    turn_id,
                    call_key_sha256,
                    now,
                    None,
                    server_version,
                ),
            )
            return True
        if state != "output_seen" or previous["output_observed_at"] is not None:
            return False
        self._db.execute(
            """
            UPDATE context_switch_events
            SET output_observed_at = ?, server_version = ?
            WHERE conversation_id = ? AND turn_id = ? AND call_key_sha256 = ?
            """,
            (now, server_version, conversation_id, turn_id, call_key_sha256),
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
        kind = _item_projection_kind(item)
        status = item.get("status") if isinstance(item.get("status"), str) else (
            "completed" if completed else "inProgress"
        )
        phase = item.get("phase") if isinstance(item.get("phase"), str) else None
        text = None
        detail = None
        delivery = None
        questions = None
        if item.get("type") == "agentMessage":
            text = str(item.get("text") or "")[:_VISIBLE_TEXT_LIMIT]
            if item.get("delivery") == "async":
                delivery = "async"
                questions = _async_questions(item.get("questions"))
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
        changed = self._upsert_projection_locked(
            conversation_id,
            turn_id,
            item_id,
            kind,
            status=status,
            phase=phase,
            text=text,
            detail=detail,
            delivery=delivery,
            questions=questions,
            source=source,
            now=now,
        )
        if delivery == "async" and questions:
            changed = self._register_async_questions_locked(
                conversation_id, turn_id, item_id, questions, now=now
            ) or changed
        return changed

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
        delivery: str | None,
        questions: list[dict[str, Any]] | None,
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
                    phase, text, detail, delivery, questions_json, source,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    delivery,
                    _json(questions) if questions is not None else None,
                    source,
                    now,
                    now,
                ),
            )
            return True
        questions_json = _json(questions) if questions is not None else None
        values = (status, phase, text, detail, delivery, questions_json, source)
        previous = (
            row["status"], row["phase"], row["text"], row["detail"],
            row["delivery"], row["questions_json"], row["source"],
        )
        if values == previous:
            effective = self._db.execute(
                "SELECT row_id FROM conversation_search_item_sources "
                "WHERE turn_id=? AND native_item_id=?", (turn_id, item_id),
            ).fetchone()
            if effective is None or effective["row_id"] == row["row_id"]:
                return False
            # Returning to an older kind can be an otherwise byte-identical
            # upsert. Still project this source correction through SQL triggers.
        self._db.execute(
            """
            UPDATE items SET status = ?, phase = ?, text = ?, detail = ?,
                delivery = ?, questions_json = ?, source = ?,
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
                delivery=None,
                questions=None,
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
            f"""
            SELECT i.phase, i.text, i.delivery FROM items i
            WHERE i.turn_id = ? AND i.kind = 'agent_message'
              AND {self._conversation_search_effective_source_sql('i')}
            ORDER BY i.row_id ASC
            """,
            (turn_id,),
        ).fetchall()
        finals = [
            str(row["text"] or "").strip()
            for row in rows
            if row["phase"] == "final_answer" and row["delivery"] != "async"
        ]
        if finals:
            return finals[-1]
        compatible = [
            str(row["text"] or "").strip() for row in rows
            if row["phase"] is None and row["delivery"] != "async"
        ]
        return compatible[-1] if compatible else None

    @staticmethod
    def _validate_reconcile_checkpoint(
        conversation_id: str, checkpoint: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if checkpoint is None:
            return None
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("conversation_id") != conversation_id
            or not isinstance(checkpoint.get("items"), dict)
            or not isinstance(checkpoint.get("item_sources"), dict)
            or not isinstance(checkpoint.get("turn_statuses"), dict)
            or checkpoint.get("thread_status") is not None
            and not isinstance(checkpoint["thread_status"], str)
        ):
            raise AppServerProtocolError("reconcile checkpoint is malformed")
        for key, revision in checkpoint["items"].items():
            if (
                not isinstance(key, tuple) or len(key) != 3
                or not all(isinstance(value, str) for value in key)
                or isinstance(revision, bool) or not isinstance(revision, int)
            ):
                raise AppServerProtocolError("reconcile checkpoint item is malformed")
        if set(checkpoint["item_sources"]) != set(checkpoint["items"]) or not all(
            value is None or isinstance(value, str)
            for value in checkpoint["item_sources"].values()
        ):
            raise AppServerProtocolError("reconcile checkpoint item source is malformed")
        if not all(
            isinstance(turn_id, str) and isinstance(status, str)
            for turn_id, status in checkpoint["turn_statuses"].items()
        ):
            raise AppServerProtocolError("reconcile checkpoint turn is malformed")
        return checkpoint

    def _checkpoint_changed_turn_statuses_locked(
        self, conversation_id: str, checkpoint: dict[str, Any] | None
    ) -> dict[str, str]:
        """Return current turn statuses changed after the read checkpoint."""
        if checkpoint is None:
            return {}
        rows = self._db.execute(
            "SELECT id, status FROM turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        before = checkpoint["turn_statuses"]
        return {
            row["id"]: row["status"]
            for row in rows
            if before.get(row["id"]) != row["status"]
        }

    def _item_changed_after_checkpoint_locked(
        self,
        conversation_id: str,
        turn_id: str,
        item: dict[str, Any],
        checkpoint: dict[str, Any] | None,
    ) -> bool:
        """Whether a notification created or revised this projection after read."""
        if checkpoint is None:
            return False
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return False
        kind = _item_projection_kind(item)
        row = self._db.execute(
            """
            SELECT revision FROM items
            WHERE conversation_id = ? AND turn_id = ?
              AND native_item_id = ? AND kind = ?
            """,
            (conversation_id, turn_id, item_id, kind),
        ).fetchone()
        if row is None:
            return False
        previous = checkpoint["items"].get((turn_id, item_id, kind))
        return previous is None or row["revision"] != previous

    def delta_baseline_candidates(
        self,
        conversation_id: str,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Find changed delta-only projections needing one authoritative item read."""
        thread = snapshot.get("thread") if isinstance(snapshot, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list):
            raise AppServerProtocolError("thread/read response is malformed")
        with self._lock:
            self._conversation_row(conversation_id)
            checkpoint = self._validate_reconcile_checkpoint(
                conversation_id, checkpoint
            )
            if checkpoint is None:
                return []
            candidates: list[dict[str, Any]] = []
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    continue
                turn_id = turn["id"]
                predecessor_id: str | None = None
                for item in turn.get("items") or []:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                        continue
                    item_id = item["id"]
                    kind = _item_projection_kind(item)
                    row = self._db.execute(
                        """
                        SELECT revision, source FROM items
                        WHERE conversation_id = ? AND turn_id = ?
                          AND native_item_id = ? AND kind = ?
                        """,
                        (conversation_id, turn_id, item_id, kind),
                    ).fetchone()
                    key = (turn_id, item_id, kind)
                    if (
                        row is not None
                        and row["source"] == "notification_delta"
                        and (
                            checkpoint["items"].get(key) is None
                            or row["revision"] != checkpoint["items"][key]
                        )
                    ):
                        candidates.append({
                            "turn_id": turn_id,
                            "item_id": item_id,
                            "kind": kind,
                            "revision": row["revision"],
                            "predecessor_id": predecessor_id,
                        })
                    predecessor_id = item_id
            return candidates

    def hydrate_delta_baseline(
        self,
        conversation_id: str,
        turn_id: str,
        item_id: str,
        *,
        expected_revision: int,
        fresh_native_item: dict[str, Any],
    ) -> bool:
        """Atomically replace a delta-only suffix with one full item baseline."""
        self._validate_id(conversation_id, "conversation")
        self._validate_id(turn_id, "turn")
        self._validate_id(item_id, "item")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise AppServerProtocolError("delta baseline revision is invalid")
        if (
            not isinstance(fresh_native_item, dict)
            or fresh_native_item.get("id") != item_id
        ):
            raise AppServerProtocolError("delta baseline item does not match request")
        kind = _item_projection_kind(fresh_native_item)
        now = utc_now()
        with self._lock:
            try:
                self._begin()
                conversation = self._conversation_row(conversation_id)
                row = self._db.execute(
                    """
                    SELECT revision, source FROM items
                    WHERE conversation_id = ? AND turn_id = ?
                      AND native_item_id = ? AND kind = ?
                    """,
                    (conversation_id, turn_id, item_id, kind),
                ).fetchone()
                if (
                    row is None
                    or row["revision"] != expected_revision
                    or row["source"] != "notification_delta"
                ):
                    self._finish(None)
                    return False
                turn = self._db.execute(
                    "SELECT status FROM turns WHERE id = ? AND conversation_id = ?",
                    (turn_id, conversation_id),
                ).fetchone()
                if turn is None:
                    raise AppServerProtocolError("delta baseline turn is not owned")
                hydrated = dict(fresh_native_item)
                hydrated.setdefault("status", (
                    "completed"
                    if turn["status"] in TERMINAL_TURN_STATUSES
                    else "inProgress"
                ))
                self._upsert_item_locked(
                    conversation_id,
                    turn_id,
                    hydrated,
                    completed=turn["status"] in TERMINAL_TURN_STATUSES,
                    source="item_read",
                    now=now,
                )
                if turn["status"] in TERMINAL_TURN_STATUSES:
                    self._db.execute(
                        "UPDATE turns SET final_text = ? WHERE id = ?",
                        (self._final_text_locked(turn_id), turn_id),
                    )
                self._finish(None)
                return True
            except BaseException as error:
                self._finish(error)
                raise

    def reconcile_thread(
        self,
        conversation_id: str,
        snapshot: dict[str, Any],
        *,
        checkpoint: dict[str, Any] | None = None,
        runtime_inactive_verified: bool = False,
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
                checkpoint = self._validate_reconcile_checkpoint(
                    conversation_id, checkpoint
                )
                protected_turn_statuses = self._checkpoint_changed_turn_statuses_locked(
                    conversation_id, checkpoint
                )
                turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
                # A terminal timestamp is written only after a native terminal
                # observation. Older releases could subsequently overwrite its
                # status with a stale active history entry without clearing it.
                latest_known_native_ordinal = max((
                    ordinal for ordinal, turn in enumerate(turns, 1)
                    if isinstance(turn, dict)
                    and isinstance(turn.get("id"), str)
                    and isinstance(turn.get("status"), str)
                    and turn["status"] in ACTIVE_TURN_STATUSES | TERMINAL_TURN_STATUSES
                ), default=0)
                active: list[str] = []
                unknown_native_turn_status = False
                unresolved_terminal_regression = False
                seen_snapshot_turns: set[str] = set()
                for ordinal, turn in enumerate(turns, 1):
                    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                        continue
                    turn_id = turn["id"]
                    self._validate_id(turn_id, "turn")
                    snapshot_status = (
                        turn.get("status")
                        if isinstance(turn.get("status"), str) else "unknown"
                    )
                    status = protected_turn_statuses.get(turn_id, snapshot_status)
                    if turn_id in seen_snapshot_turns:
                        raise AppServerProtocolError("thread/read returned duplicate turns")
                    seen_snapshot_turns.add(turn_id)
                    existing = self._db.execute(
                        """
                        SELECT conversation_id, status, completed_at, error_json, final_text
                        FROM turns WHERE id = ?
                        """,
                        (turn_id,),
                    ).fetchone()
                    if existing is not None and existing["conversation_id"] != conversation_id:
                        raise AppServerProtocolError("native turn belongs to another conversation")
                    preserve_terminal = False
                    recovered_terminal = False
                    if (
                        existing is not None
                        and existing["completed_at"] is not None
                        and status in ACTIVE_TURN_STATUSES
                    ):
                        if existing["status"] in TERMINAL_TURN_STATUSES:
                            status = existing["status"]
                            preserve_terminal = True
                        elif (
                            existing["status"] in ACTIVE_TURN_STATUSES
                            and ordinal < latest_known_native_ordinal
                        ):
                            # Repair only an already-ended historical turn with
                            # a later native turn. The later turn may already
                            # have finished while recovery was reading history.
                            # Its original outcome was lost: do not invent a
                            # successful completion or interrupt native work.
                            status = "interrupted"
                            preserve_terminal = recovered_terminal = True
                        else:
                            # Do not infer which outcome to restore for the
                            # latest turn: without later-turn evidence keep
                            # this unresolved conflict behind the user gate.
                            unresolved_terminal_regression = True
                        if preserve_terminal:
                            emit(
                                "conversation_terminal_regression_blocked",
                                conversation_id=conversation_id, turn_id=turn_id,
                                snapshot_status=snapshot_status,
                                retained_status=status,
                                recovered_terminal=recovered_terminal,
                            )
                    notification_commands = self._db.execute("SELECT completion_notification_epoch FROM commands WHERE "
                        "conversation_id=? AND result_turn_id=?", (conversation_id, turn_id)).fetchall()
                    notification_intent = bool(status in ACTIVE_TURN_STATUSES and any(
                        self._completion_intent_locked(conversation, command) for command in (notification_commands or [None])))
                    proof_pending = bool(status in ACTIVE_TURN_STATUSES and not notification_intent
                        and conversation['completion_notification']
                        and self._db.execute("SELECT 1 FROM schema_meta WHERE "
                            "key='completion_notifications_enabled' AND value='1'").fetchone()
                        and self._db.execute("SELECT 1 FROM schema_meta WHERE "
                            "key='completion_notifications_epoch'").fetchone())
                    self._db.execute(
                        """
                        INSERT INTO turns(
                            id, conversation_id, ordinal, status, started_at,
                            notification_requested, notification_detail
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET status = excluded.status
                        """,
                        (
                            turn_id,
                            conversation_id,
                            ordinal,
                            status,
                            now,
                            # A newly discovered terminal turn is history, not a
                            # new completion.  Existing live turns retain their
                            # captured intent via ON CONFLICT above.
                            int(notification_intent),
                            'module_unproven' if proof_pending else None,
                        ),
                    )
                    if notification_intent:
                        self._promote_completion_intent_locked(conversation_id, turn_id)
                    for item in ([] if preserve_terminal else turn.get("items") or []):
                        if (
                            isinstance(item, dict)
                            and not self._item_changed_after_checkpoint_locked(
                                conversation_id, turn_id, item, checkpoint
                            )
                        ):
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
                        if (preserve_terminal or turn_id in protected_turn_statuses) and existing is not None:
                            error = _object(existing["error_json"], None)
                        self._db.execute(
                            """
                            UPDATE turns SET completed_at = COALESCE(completed_at, ?),
                                final_text = ?, error_json = ? WHERE id = ?
                            """,
                            (
                                now,
                                existing["final_text"] if preserve_terminal else self._final_text_locked(turn_id),
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
                        if not preserve_terminal and not (
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
                for turn_id, status in protected_turn_statuses.items():
                    if turn_id in seen_snapshot_turns:
                        continue
                    if status in ACTIVE_TURN_STATUSES:
                        active.append(turn_id)
                    elif status not in TERMINAL_TURN_STATUSES:
                        unknown_native_turn_status = True
                unknown = self._db.execute(
                    """
                    SELECT 1 FROM commands
                    WHERE conversation_id = ? AND state = 'delivery_unknown' LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                thread_status = _json(thread.get("status"))
                if (
                    checkpoint is not None
                    and conversation["thread_status"] != checkpoint["thread_status"]
                ):
                    thread_status = conversation["thread_status"]
                runtime_status = _object(thread_status, {}) or {}
                inactive_runtime = (runtime_status.get("type") in {"idle", "notLoaded"}
                                    and not any(status in ACTIVE_TURN_STATUSES
                                                for status in protected_turn_statuses.values()))
                if inactive_runtime and active and not unknown_native_turn_status:
                    # Current runtime state is not historical Turn outcome.
                    # Keep raw Turn statuses/timestamps untouched, and do not
                    # release queued work merely because the root is idle.
                    state = "idle" if runtime_inactive_verified and unknown is None else "needs_user"
                    active_turn_id = None
                    recovery_state = "tree_stop_verified" if state == "idle" else "stale_native_turn_history"
                elif unresolved_terminal_regression:
                    state = "needs_user"
                    active_turn_id = None
                    recovery_state = "ambiguous_native_state"
                elif unknown_native_turn_status:
                    state = "needs_user"
                    active_turn_id = None
                    recovery_state = "unknown_native_turn_status"
                elif len(active) == 1:
                    state = "active"
                    active_turn_id = active[0]
                    recovery_state = "rejoined"
                elif not active and runtime_status.get("type") == "active":
                    state = "needs_user"
                    active_turn_id = None
                    recovery_state = "active_native_turn_unidentified"
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
                        thread_status,
                        recovery_state,
                        _json(token_usage) if token_usage is not None else None,
                        now,
                        conversation_id,
                    ),
                )
                self._finish(None)
                self._browser_history_epochs[conversation_id] = self._browser_history_epochs.get(conversation_id, 0) + 1
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
        *,
        replay_pending: bool = False,
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
                    cached = _object(existing["response_json"], {})
                    cached.pop("_review_ownership", None)
                    return cached
                if replay_pending:
                    # Some tools initiate an externally observable, expensive
                    # operation without a durable remote id.  After a crash a
                    # second request cannot prove the first one did not win.
                    raise AppServerProtocolError(
                        "tool call outcome is unknown and will not be replayed"
                    )
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
                cached = _object(row["response_json"], {})
                cached.pop("_review_ownership", None)
                return cached
            previous = _object(row["response_json"], {})
            stored_response = dict(response)
            if isinstance(previous.get("_review_ownership"), dict):
                stored_response["_review_ownership"] = previous["_review_ownership"]
            self._db.execute(
                """
                UPDATE tool_calls SET state = 'completed', response_json = ?,
                    updated_at = ? WHERE call_id = ?
                """,
                (_json(stored_response), utc_now(), call_id),
            )
        return response

    def owned_review_threads(self, conversation_id: str) -> dict[str, dict[str, Any]]:
        """Persistent ownership outlives runner completion and cleanup markers.

        Legacy successful results contain exact native IDs in the structured
        tool envelope. Never infer ownership from cwd or reviewer prose.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id, response_json FROM tool_calls WHERE conversation_id = ? AND tool = 'code_review'",
                (conversation_id,),
            ).fetchall()
        owners = {}
        for row in rows:
            response = _object(row['response_json'], {})
            owner = response.get('_review_ownership') or response.get('review_lifecycle') or response.get('review_failure') or response.get('review_cleanup')
            if isinstance(owner, dict) and owner.get('review_started') is False:
                continue  # Creation alone cannot spawn agents; review/start was never requested.
            if not isinstance(owner, dict) or not isinstance(owner.get('review_thread_id'), str):
                items = response.get('contentItems') or []
                prefix = 'Official native code review completed; complete JSON follows in ordered chunks.\n'
                encoded = ''.join(item.get('text', '') for item in items if isinstance(item, dict) and item.get('type') == 'inputText')
                if not encoded.startswith(prefix):
                    continue
                try:
                    owner = json.loads(encoded[len(prefix):])
                except (ValueError, TypeError):
                    continue
                if not isinstance(owner, dict) or owner.get('official_channel') != 'app_server_native_review':
                    continue
            thread_id = owner.get('review_thread_id')
            if isinstance(thread_id, str) and thread_id:
                owners[row['call_id']] = {'review_thread_id': thread_id,
                    'interrupt_attempted': bool(owner.get('interrupt_attempted'))}
        return owners

    def mark_review_interrupt_attempt(self, call_id: str, thread_id: str) -> None:
        """Journal delivery before await, allowing a single stop-controller owner."""
        with self._lock:
            row = self._db.execute("SELECT response_json FROM tool_calls WHERE call_id = ? AND tool = 'code_review'", (call_id,)).fetchone()
            if row is None:
                raise AppServerProtocolError('review interrupt owner is unknown')
            response = _object(row['response_json'], {})
            owner = response.get('_review_ownership', {})
            if owner.get('review_thread_id') != thread_id:
                raise AppServerProtocolError('review interrupt owner differs')
            response['_review_ownership'] = {**owner, 'interrupt_attempted': True}
            self._db.execute('UPDATE tool_calls SET response_json = ?, updated_at = ? WHERE call_id = ?',
                             (_json(response), utc_now(), call_id))

    def pending_review_cleanups(self) -> list[dict[str, str | None]]:
        """Return durable native-review lifecycle records needing reconciliation."""
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id, conversation_id, turn_id, response_json FROM tool_calls "
                "WHERE tool = 'code_review' AND state IN ('inProgress', 'completed')"
            ).fetchall()
        pending: list[dict[str, str | None]] = []
        for row in rows:
            response = _object(row["response_json"], {})
            lifecycle = response.get("review_lifecycle") if isinstance(response, dict) else None
            if not isinstance(lifecycle, dict):
                continue
            phase = lifecycle.get("phase")
            if phase not in {"reserved", "thread_created", "start_requested", "turn_started"}:
                continue
            thread_id, turn_id = lifecycle.get("review_thread_id"), lifecycle.get("review_turn_id")
            if phase in {"reserved", "thread_created"}:
                thread_id = None
            if (phase in {"reserved", "thread_created"}) or (
                isinstance(thread_id, str) and (turn_id is None or isinstance(turn_id, str))
            ):
                pending.append({
                    "call_id": row["call_id"], "conversation_id": row["conversation_id"],
                    "turn_id": row["turn_id"], "review_thread_id": thread_id,
                    "review_turn_id": turn_id, "phase": phase,
                })
        return pending

    def set_review_lifecycle(self, call_id: str, lifecycle: dict[str, Any] | None) -> None:
        """Synchronously journal one native reviewer lifecycle transition."""
        self._validate_id(call_id, "tool call")
        with self._lock:
            row = self._db.execute(
                "SELECT response_json FROM tool_calls WHERE call_id = ? AND tool = 'code_review'", (call_id,)
            ).fetchone()
            if row is None:
                raise AppServerProtocolError("review lifecycle tool call is not claimed")
            response = _object(row["response_json"], {})
            if not isinstance(response, dict):
                response = {}
            if lifecycle is None:
                response.pop("review_lifecycle", None)
            else:
                response["review_lifecycle"] = lifecycle
                thread_id = lifecycle.get('review_thread_id')
                if isinstance(thread_id, str) and thread_id:
                    previous = response.get('_review_ownership', {})
                    response['_review_ownership'] = {'review_thread_id': thread_id,
                        'interrupt_attempted': bool(previous.get('interrupt_attempted'))
                        if previous.get('review_thread_id') == thread_id else False,
                        'review_started': lifecycle.get('phase') in {'start_requested','turn_started'}
                        or (previous.get('review_thread_id') == thread_id and bool(previous.get('review_started')))}
            self._db.execute(
                "UPDATE tool_calls SET response_json = ?, updated_at = ? WHERE call_id = ?",
                (_json(response), utc_now(), call_id),
            )

    def clear_review_cleanup(self, call_id: str) -> None:
        """Clear only the unconfirmed-cleanup marker; preserve failure evidence."""
        self._validate_id(call_id, "tool call")
        with self._lock:
            row = self._db.execute(
                "SELECT response_json FROM tool_calls WHERE call_id = ? AND tool = 'code_review'", (call_id,)
            ).fetchone()
            if row is None:
                return
            response = _object(row["response_json"], {})
            if not isinstance(response, dict) or (
                "review_cleanup" not in response and "review_lifecycle" not in response
            ):
                return
            response.pop("review_cleanup", None)
            response.pop("review_lifecycle", None)
            self._db.execute(
                "UPDATE tool_calls SET response_json = ?, updated_at = ? WHERE call_id = ?",
                (_json(response), utc_now(), call_id),
            )

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

    @staticmethod
    def _history_cursor(conversation_id: str, turn: dict[str, Any]) -> str:
        raw = json.dumps([conversation_id, turn["ordinal"], turn["id"]], separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def _history_boundary(self, conversation_id: str, first: Any) -> dict[str, Any]:
        previous = self._db.execute("SELECT id FROM turns WHERE conversation_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT 1",
            (conversation_id, first["ordinal"])).fetchone() if first else None
        return {"cursor": self._history_cursor(conversation_id, first) if previous else None,
                "previous_turn_id": previous["id"] if previous else None}

    def _history_fingerprint(self, conversation_id: str, low: int, high: int) -> str:
        """Validate loaded pages using metadata, never historical body hashes.

        Ordinary item changes have durable revisions/source. Reconcile may
        correct fallback text without an item revision, so its explicit epoch
        invalidates old pages. Restart invalidates browser caches too. This
        tracks application writes, not arbitrary external database surgery.
        None of this alters the native execution journal.
        """
        digest = hashlib.sha256(f"browser-v1:{self._browser_history_nonce}:{self._browser_history_epochs.get(conversation_id, 0)}:{conversation_id}:{low}:{high}".encode())
        bounds = (conversation_id, low, high)
        turn_scope = "SELECT id FROM turns WHERE conversation_id=? AND ordinal BETWEEN ? AND ?"
        statements = [
            ("turns", "SELECT id,ordinal,status,kind,started_at,completed_at,error_json,notification_state FROM turns WHERE conversation_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal,id"),
            ("items", "SELECT i.row_id,i.turn_id,i.native_item_id,i.kind,i.revision,i.source,e.row_id FROM items i LEFT JOIN conversation_search_item_sources e ON e.turn_id=i.turn_id AND e.native_item_id=i.native_item_id WHERE i.turn_id IN (" + turn_scope + ") ORDER BY i.row_id"),
        ]
        for table, turn_column, columns, order in (
            ("commands", "COALESCE(result_turn_id,target_turn_id)", "id,kind,state,target_turn_id,result_turn_id,reason,origin,updated_at", "id"),
            ("artifacts", "turn_id", "id,turn_id,native_item_id,name,mime_type,size,sha256,created_at", "id"),
            ("question_requests", "turn_id", "id,turn_id,state,updated_at", "id"),
            ("async_question_requests", "turn_id", "id,turn_id,state,command_id,updated_at", "id"),
            ("tool_calls", "turn_id", "call_id,turn_id,state,updated_at", "call_id"),
        ):
            statements.append((table, "SELECT " + columns + " FROM " + table + " WHERE " + turn_column + " IN (" + turn_scope + ") ORDER BY " + order))
        for table, statement in statements:
            digest.update(table.encode())
            for row in self._db.execute(statement, bounds):
                digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
        return f"{low}:{high}:{digest.hexdigest()[:24]}"

    def browser_plan_context(self, conversation_id: str) -> dict[str, Any]:
        """Exact Plan inputs without unrelated historical message bodies."""
        with self._lock:
            conversation = self._decode_conversation(self._conversation_row(conversation_id))
            latest = self._db.execute("""SELECT i.*,t.ordinal,
                CASE WHEN e.row_id IS NULL OR e.row_id=i.row_id THEN 1 ELSE 0 END AS is_current_source
                FROM items i JOIN turns t ON t.id=i.turn_id
                LEFT JOIN conversation_search_item_sources e ON e.turn_id=i.turn_id AND e.native_item_id=i.native_item_id
                WHERE i.conversation_id=? AND i.kind='plan' AND i.native_item_id!='turn-plan'
                AND (e.row_id IS NULL OR e.row_id=i.row_id)
                ORDER BY t.ordinal DESC,i.row_id DESC LIMIT 1""", (conversation_id,)).fetchone()
            if latest is None:
                return {"conversation": conversation, "turns": [], "commands": []}
            turns = [dict(row) for row in self._db.execute("SELECT id,ordinal,status FROM turns WHERE conversation_id=? AND ordinal>=? ORDER BY ordinal", (conversation_id, latest["ordinal"]))]
            commands = [self._decode_command(row) for row in self._db.execute("""SELECT * FROM commands WHERE conversation_id=? AND (
                COALESCE(result_turn_id,target_turn_id) IN (SELECT id FROM turns WHERE conversation_id=? AND ordinal>=?)
                OR state IN ('submitted','queued','delivery_unknown') OR payload_json LIKE '%"plan_confirmation"%') ORDER BY created_at""", (conversation_id, conversation_id, latest["ordinal"]))]
            for turn in turns:
                turn["items"] = [self._decode_item(latest)] if turn["id"] == latest["turn_id"] else []
                turn["commands"] = [command for command in commands if (command.get("result_turn_id") or command.get("target_turn_id")) == turn["id"]]
            return {"conversation": conversation, "turns": turns, "commands": commands}

    def browser_view(self, conversation_id: str, *, before: str = "", pages: list[str] | None = None) -> dict[str, Any]:
        """Read one stable 20-turn window, current controls and changed old pages."""
        self._validate_id(conversation_id, "conversation")
        if len(before) > 600 or len(pages or []) > 1000:
            raise ValueError("history cursor is too large")
        with self._lock:
            conversation = self._conversation_row(conversation_id)
            upper = None
            if before:
                try:
                    owner, upper, boundary = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                    if owner != conversation_id or not isinstance(upper, int) or not 1 <= upper < 2**63 or not isinstance(boundary, str):
                        raise ValueError
                    row = self._db.execute("SELECT ordinal FROM turns WHERE conversation_id=? AND id=?", (conversation_id, boundary)).fetchone()
                    if row is None or row["ordinal"] != upper:
                        raise ValueError
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as error:
                    raise ValueError("history boundary changed; reload the browser projection") from error
            rows = self._db.execute("SELECT id,ordinal FROM turns WHERE conversation_id=?" +
                (" AND ordinal<?" if upper is not None else "") + " ORDER BY ordinal DESC LIMIT 20",
                (conversation_id, upper) if upper is not None else (conversation_id,)).fetchall()
            rows = list(reversed(rows))
            ids = [row["id"] for row in rows]
            page_ids = list(ids)
            if conversation["active_turn_id"] and conversation["active_turn_id"] not in ids:
                ids.append(conversation["active_turn_id"])
            # Preserve any live async native question even if its origin is old.
            ids.extend(row[0] for row in self._db.execute("SELECT DISTINCT turn_id FROM async_question_requests WHERE conversation_id=? AND state IN ('awaiting_answer','answer_claimed','delivery_unknown','rejected')", (conversation_id,)) if row[0] not in ids)
            value = self._snapshot(conversation_id, ids)
            token = self._history_fingerprint(conversation_id, rows[0]["ordinal"], rows[-1]["ordinal"]) if rows else None
            replacements = []
            for previous in dict.fromkeys(pages or []):
                match = re.fullmatch(r"([0-9]{1,12}):([0-9]{1,12}):([a-f0-9]{24})", previous)
                if not match:
                    raise ValueError("invalid history page version")
                low, high = int(match[1]), int(match[2])
                if low < 1 or high < low:
                    raise ValueError("invalid history page range")
                # This response already refreshes the entire latest range;
                # do not hash/read/send the same live window a second time.
                if rows and rows[0]["ordinal"] <= low <= high <= rows[-1]["ordinal"]:
                    continue
                changed_ids = [row[0] for row in self._db.execute("SELECT id FROM turns WHERE conversation_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal LIMIT 21", (conversation_id, low, high))]
                if len(changed_ids) > 20:
                    raise ValueError("history page boundary changed; reload the browser projection")
                changed = self._history_fingerprint(conversation_id, low, high)
                if changed == previous:
                    continue
                changed_value = self._snapshot(conversation_id, changed_ids)
                replacements.append({"previous": previous, "token": changed, "value": changed_value,
                    **self._history_boundary(conversation_id, changed_value["turns"][0] if changed_value["turns"] else None)})
            value["history"] = {"mode": "paged", **self._history_boundary(conversation_id, rows[0] if rows else None),
                "token": token, "page_turn_ids": page_ids, "replacements": replacements}
            return value

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        """Compatibility reader: always returns the complete projection."""
        return self._snapshot(conversation_id)

    def _snapshot(self, conversation_id: str, turn_ids: list[str] | None = None) -> dict[str, Any]:
        self._validate_id(conversation_id, "conversation")
        with self._lock:
            conversation = self._decode_conversation(
                self._conversation_row(conversation_id)
            )
            # A bounded reader shares the exact decoders and security projection
            # with full snapshots. No full transcript is loaded then sliced.
            selected = ""
            params: tuple[Any, ...] = (conversation_id,)
            if turn_ids is not None:
                selected = " AND id IN (" + ",".join("?" for _ in turn_ids) + ")" if turn_ids else " AND 0"
                params += tuple(turn_ids)
            related = selected.replace("id IN", "turn_id IN")
            turns = self._db.execute(
                "SELECT * FROM turns WHERE conversation_id = ?" + selected + " ORDER BY ordinal ASC",
                params,
            ).fetchall()
            command_scope = ""
            if turn_ids is not None:
                command_scope = " AND (COALESCE(result_turn_id,target_turn_id) IN (" + ",".join("?" for _ in turn_ids) + ") OR (result_turn_id IS NULL AND target_turn_id IS NULL) OR state IN ('submitted','queued','delivery_unknown'))" if turn_ids else " AND ((result_turn_id IS NULL AND target_turn_id IS NULL) OR state IN ('submitted','queued','delivery_unknown'))"
            commands = self._db.execute(
                "SELECT * FROM commands WHERE conversation_id = ?" + command_scope + " ORDER BY created_at ASC",
                params,
            ).fetchall()
            items = self._db.execute(
                """SELECT i.*, CASE WHEN effective.native_item_id IS NULL
                        OR effective.row_id=i.row_id THEN 1 ELSE 0 END AS is_current_source
                   FROM items i LEFT JOIN conversation_search_item_sources effective
                     ON effective.turn_id=i.turn_id AND effective.native_item_id=i.native_item_id
                   WHERE i.conversation_id=?""" + related.replace("turn_id", "i.turn_id") + " ORDER BY i.row_id ASC",
                params,
            ).fetchall()
            artifacts = self._db.execute(
                "SELECT * FROM artifacts WHERE conversation_id = ?" +
                (" AND (turn_id IS NULL OR " + related.removeprefix(" AND ") + ")" if turn_ids is not None else "") + " ORDER BY created_at ASC",
                params,
            ).fetchall()
            tool_calls = self._db.execute(
                """
                SELECT turn_id, tool, response_json FROM tool_calls
                WHERE conversation_id = ? AND state = 'completed'
                """ + related,
                params,
            ).fetchall()
            questions = self._db.execute(
                "SELECT * FROM question_requests WHERE conversation_id = ?" +
                (" AND (" + related.removeprefix(" AND ") + " OR state NOT IN ('answered','closed','expired'))" if turn_ids is not None else "") + " ORDER BY created_at ASC",
                params,
            ).fetchall()
            async_questions = self._db.execute(
                """SELECT aq.*, cmd.state AS command_state
                   FROM async_question_requests aq
                   LEFT JOIN commands cmd ON cmd.id = aq.command_id
                   WHERE aq.conversation_id = ?""" + related.replace("turn_id", "aq.turn_id") + " ORDER BY aq.created_at ASC",
                params,
            ).fetchall()
            compaction_count, manual_compaction_count = self._db.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM commands
                        WHERE commands.conversation_id = context_compaction_events.conversation_id
                          AND commands.kind = 'compact'
                          AND commands.result_turn_id = context_compaction_events.turn_id
                    ) THEN 1 ELSE 0 END), 0)
                FROM context_compaction_events
                WHERE conversation_id = ? AND completed_observed_at IS NOT NULL
                """,
                (conversation_id,),
            ).fetchone()
            compaction_count = int(compaction_count)
            manual_compaction_count = int(manual_compaction_count)
            context_switch_count = int(self._db.execute(
                """
                SELECT COUNT(*) FROM context_switch_events
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()[0])
            context_switches = self._db.execute(
                """
                SELECT call_observed_at, output_observed_at
                FROM context_switch_events
                WHERE conversation_id = ?
                ORDER BY COALESCE(output_observed_at, call_observed_at) DESC,
                    turn_id DESC
                LIMIT ?
                """,
                (conversation_id, _CONTEXT_SWITCH_DETAIL_LIMIT),
            ).fetchall()
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
            conversation["context_compaction_manual_completed_count"] = manual_compaction_count
            conversation["context_compaction_other_completed_count"] = (
                compaction_count - manual_compaction_count
            )
            conversation["context_compaction_state"] = compaction_state
            # Hashes and App Server version are deliberately omitted even
            # from this broad store snapshot; no caller needs them to render.
            conversation["context_switch_count"] = context_switch_count
            conversation["context_switches"] = [
                {
                    "observed_at": row["output_observed_at"] or row["call_observed_at"],
                    "state": (
                        "output_seen" if row["output_observed_at"] is not None
                        else "call_seen"
                    ),
                }
                for row in context_switches
            ]
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
            "questions": [self._decode_question(row) for row in questions],
            "async_questions": [self._decode_async_question(row) for row in async_questions],
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
        value.pop("completion_notification_epoch", None)
        value["payload"] = _object(value.pop("payload_json"), {})
        value["origin"] = value.get("origin") if value.get("origin") in {"user", "system", "scheduled"} else "user"
        return value

    @staticmethod
    def _decode_item(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["questions"] = _async_questions(
            _object(value.pop("questions_json", None), None)
        )
        if "is_current_source" in value:
            value["is_current_source"] = bool(value["is_current_source"])
        return value

    @staticmethod
    def _decode_question(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["questions"] = _object(value.pop("questions_json"), [])
        value["answers"] = _object(value.pop("answers_json"), None)
        value["is_blocking"] = bool(value["is_blocking"])
        # These are persisted so matching can be strict, but are never a
        # browser/public projection.
        return value

    @staticmethod
    def _decode_async_question(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["question"] = _object(value.pop("question_json"), {})
        return value

    @staticmethod
    def _decode_artifact(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)
