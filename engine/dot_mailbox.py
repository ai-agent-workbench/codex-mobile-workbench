"""A separate, pull-only dot conversation store. No native execution path.

The parent reference is a correlation supplied by an authorized operator,
not a public ChatGPT API or proof of the caller's dot identity. Local access
uses the existing computer user's file permissions; this creates no token.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import threading
import uuid


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_TABLES = {"dot_meta", "dot_conversations", "dot_messages", "dot_replies"}
_SOURCE = "parent_dot_via_manual_task_relay"
MCP_REPLY_SOURCE = "parent_dot_via_mcp_reply"


class DotMailboxError(ValueError):
    code = "invalid_request"
    status = 400


class DotMailboxDenied(DotMailboxError):
    code = "scope_denied"
    status = 403


class DotMailboxConflict(DotMailboxError):
    code = "mailbox_conflict"
    status = 409


class DotMailboxDisabled(DotMailboxError):
    code = "dot_disabled"
    status = 503


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DotMailboxError(label + " is invalid")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 60_000:
        raise DotMailboxError("message must contain 1 to 60000 characters")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise DotMailboxError("message must be valid UTF-8 text") from error
    return value  # Preserve the supplied reply exactly, including whitespace.


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DotMailboxError("expected_revision must be a positive integer")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DotScope:
    owner_account_id: str
    conversation_id: str
    parent_dot_ref: str
    operations: frozenset[str] = frozenset({"read"})

    def __post_init__(self):
        for label in ("owner_account_id", "conversation_id", "parent_dot_ref"):
            _identifier(getattr(self, label), label)
        if not isinstance(self.operations, frozenset) or not self.operations <= {"read", "reply"}:
            raise DotMailboxDenied("unsupported dot scope operation")

    def require(self, operation):
        if operation not in self.operations:
            raise DotMailboxDenied("operation is outside the selected scope")


class DotMailbox:
    """SQLite transactions enforce ordering/CAS across separate CLI processes."""

    def __init__(self, path: Path, *, read_only=False):
        self.path = Path(path).resolve()
        if self.path.name != "dot-mailbox.sqlite3":
            raise DotMailboxDenied("only the dedicated dot-mailbox.sqlite3 is accepted")
        self.read_only = read_only is True
        if not self.read_only:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path.as_uri() + "?mode=ro" if self.read_only else self.path,
            uri=self.read_only, timeout=5, isolation_level=None, check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        try:
            existing = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if existing and existing != _TABLES:
                raise DotMailboxDenied("database is not the dedicated dot mailbox")
            if existing:
                metadata = dict(self._db.execute("SELECT key,value FROM dot_meta"))
                if metadata != {"schema_version": "1", "backend": "dot_mailbox"}:
                    raise DotMailboxDenied("unsupported dot mailbox schema")
            elif self.read_only:
                raise DotMailboxDenied("read-only mailbox must already exist")
            self._db.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                self._db.execute("PRAGMA query_only=ON")
                return
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS dot_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO dot_meta VALUES('schema_version','1');
                INSERT OR IGNORE INTO dot_meta VALUES('backend','dot_mailbox');
                CREATE TABLE IF NOT EXISTS dot_conversations(
                    id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL UNIQUE,
                    parent_dot_ref TEXT NOT NULL, title TEXT NOT NULL CHECK(title='dot'),
                    revision INTEGER NOT NULL CHECK(revision>0),
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dot_messages(
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES dot_conversations(id),
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL, origin TEXT NOT NULL,
                    accepted_revision INTEGER NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(conversation_id,ordinal)
                );
                CREATE TABLE IF NOT EXISTS dot_replies(
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE REFERENCES dot_messages(id),
                    text TEXT NOT NULL,source TEXT NOT NULL,
                    written_revision INTEGER NOT NULL,created_at TEXT NOT NULL
                );
                COMMIT;
            """)
        except BaseException:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise DotMailboxDenied("mailbox connection is read only")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _conversation(self, conversation_id, owner_account_id, parent_dot_ref=None):
        _identifier(conversation_id, "conversation_id")
        _identifier(owner_account_id, "owner_account_id")
        row = self._db.execute(
            "SELECT * FROM dot_conversations WHERE id=? AND owner_account_id=?",
            (conversation_id, owner_account_id),
        ).fetchone()
        if row is None or (parent_dot_ref is not None and row["parent_dot_ref"] != parent_dot_ref):
            raise DotMailboxDenied("conversation is outside the selected scope")
        return dict(row)

    def open(self, owner_account_id, parent_dot_ref):
        _identifier(owner_account_id, "owner_account_id")
        _identifier(parent_dot_ref, "parent_dot_ref")
        with self._transaction():
            row = self._db.execute("SELECT * FROM dot_conversations WHERE owner_account_id=?",
                                   (owner_account_id,)).fetchone()
            if row:
                if row["parent_dot_ref"] != parent_dot_ref:
                    raise DotMailboxConflict("existing dot conversation has another parent reference")
                return dict(row)
            now = _now()
            conversation_id = "dot_" + uuid.uuid4().hex
            self._db.execute("INSERT INTO dot_conversations VALUES(?,?,?,'dot',1,?,?)",
                             (conversation_id,owner_account_id,parent_dot_ref,now,now))
            return self._conversation(conversation_id, owner_account_id)

    def existing(self, owner_account_id, parent_dot_ref):
        _identifier(owner_account_id,"owner_account_id")
        _identifier(parent_dot_ref,"parent_dot_ref")
        with self._lock:
            row = self._db.execute("SELECT * FROM dot_conversations WHERE owner_account_id=?",
                                   (owner_account_id,)).fetchone()
            if row is not None and row["parent_dot_ref"] != parent_dot_ref:
                raise DotMailboxConflict("existing dot conversation has another parent reference")
            return dict(row) if row else None

    def send(self, conversation_id, owner_account_id, *, message_id, text,
             expected_revision, origin="workbench_user", parent_dot_ref=None):
        _identifier(message_id, "message_id")
        _text(text)
        _revision(expected_revision)
        if origin not in {"workbench_user", "synthetic_test"}:
            raise DotMailboxDenied("unsupported message origin")
        with self._transaction():
            conversation = self._conversation(conversation_id, owner_account_id,parent_dot_ref)
            duplicate = self._db.execute("SELECT * FROM dot_messages WHERE id=?", (message_id,)).fetchone()
            if duplicate:
                if duplicate["conversation_id"] != conversation_id or duplicate["text"] != text or duplicate["origin"] != origin:
                    raise DotMailboxConflict("message ID was already used with different input")
                return dict(duplicate)
            if conversation["revision"] != expected_revision:
                raise DotMailboxConflict("conversation revision has changed")
            ordinal = self._db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM dot_messages WHERE conversation_id=?",
                                       (conversation_id,)).fetchone()[0]
            now = _now()
            self._db.execute("INSERT INTO dot_messages VALUES(?,?,?,?,?,?,?)",
                             (message_id,conversation_id,ordinal,text,origin,expected_revision+1,now))
            self._db.execute("UPDATE dot_conversations SET revision=revision+1,updated_at=? WHERE id=?",
                             (now,conversation_id))
            return dict(self._db.execute("SELECT * FROM dot_messages WHERE id=?", (message_id,)).fetchone())

    def snapshot(self, conversation_id, owner_account_id, *, parent_dot_ref=None):
        # Use one SQLite read transaction so replies and revision cannot be
        # observed from different commits of an external CLI writer.
        with self._lock:
            self._db.execute("BEGIN")
            try:
                conversation = self._conversation(conversation_id,owner_account_id,parent_dot_ref)
                messages = [dict(row) for row in self._db.execute("""
                    SELECT m.*,r.id AS reply_id,r.text AS reply_text,r.source AS reply_source,
                           r.created_at AS replied_at,r.written_revision AS reply_generation
                    FROM dot_messages m LEFT JOIN dot_replies r ON r.message_id=m.id
                    WHERE m.conversation_id=? ORDER BY m.ordinal
                """, (conversation_id,))]
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {"conversation": conversation, "messages": messages}

    def delivery_summary(self, conversation_id, owner_account_id, *, parent_dot_ref):
        """Metadata-only committed reply projection; never returns message text."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._conversation(conversation_id, owner_account_id, parent_dot_ref)
                replies = [dict(row) for row in self._db.execute("""
                    SELECT r.id, r.written_revision, r.created_at
                    FROM dot_replies r JOIN dot_messages m ON m.id=r.message_id
                    WHERE m.conversation_id=? ORDER BY r.written_revision
                """, (conversation_id,))]
                waiting = bool(self._db.execute("""
                    SELECT 1 FROM dot_messages m LEFT JOIN dot_replies r ON r.message_id=m.id
                    WHERE m.conversation_id=? AND r.id IS NULL LIMIT 1
                """, (conversation_id,)).fetchone())
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {"reply_generation": max((r["written_revision"] for r in replies), default=0),
                "waiting": waiting, "replies": replies}

    def read(self, scope: DotScope, *, message_id=None):
        scope.require("read")
        if message_id is not None:
            _identifier(message_id, "message_id")
        value = self.snapshot(scope.conversation_id, scope.owner_account_id,
                              parent_dot_ref=scope.parent_dot_ref)
        if message_id is None:
            message = next((m for m in value["messages"] if m["reply_id"] is None), None)
        else:
            message = next((m for m in value["messages"] if m["id"] == message_id), None)
            if message is None:
                raise DotMailboxDenied("message is outside the selected conversation")
        return {"backend": "dot_mailbox", "conversation_id": scope.conversation_id,
                "parent_dot_ref": scope.parent_dot_ref,
                "revision": value["conversation"]["revision"],
                "transport": "manual_task_relay", "automatic_wakeup": False,
                "message": message}

    def reply(self, scope: DotScope, *, message_id, reply_id, text, expected_revision, source=_SOURCE):
        scope.require("reply")
        if source not in {_SOURCE, MCP_REPLY_SOURCE}:
            raise DotMailboxDenied("unsupported reply source")
        _identifier(message_id, "message_id")
        _identifier(reply_id, "reply_id")
        _text(text)
        _revision(expected_revision)
        with self._transaction():
            conversation = self._conversation(scope.conversation_id,scope.owner_account_id,scope.parent_dot_ref)
            message = self._db.execute("SELECT * FROM dot_messages WHERE id=? AND conversation_id=?",
                                       (message_id,scope.conversation_id)).fetchone()
            if message is None:
                raise DotMailboxDenied("message is outside the selected conversation")
            previous = self._db.execute("SELECT * FROM dot_replies WHERE message_id=?", (message_id,)).fetchone()
            if previous:
                if previous["id"] != reply_id or previous["text"] != text or previous["source"] != source:
                    raise DotMailboxConflict("this message already has a different reply")
                return dict(previous)
            if self._db.execute("SELECT 1 FROM dot_replies WHERE id=?", (reply_id,)).fetchone():
                raise DotMailboxConflict("reply ID was already used")
            first = self._db.execute("""
                SELECT m.id FROM dot_messages m LEFT JOIN dot_replies r ON r.message_id=m.id
                WHERE m.conversation_id=? AND r.id IS NULL ORDER BY m.ordinal LIMIT 1
            """, (scope.conversation_id,)).fetchone()
            if first is None or first["id"] != message_id:
                raise DotMailboxConflict("reply must target the oldest unanswered message")
            if conversation["revision"] != expected_revision:
                raise DotMailboxConflict("conversation revision has changed")
            now = _now()
            self._db.execute("INSERT INTO dot_replies VALUES(?,?,?,?,?,?)",
                             (reply_id,message_id,text,source,expected_revision+1,now))
            self._db.execute("UPDATE dot_conversations SET revision=revision+1,updated_at=? WHERE id=?",
                             (now,scope.conversation_id))
            return dict(self._db.execute("SELECT * FROM dot_replies WHERE id=?", (reply_id,)).fetchone())


class DotConversationService:
    """Default closed; constructing a disabled service does not create a DB."""

    def __init__(self, path: Path, *, enabled=False, read_only=False):
        self.path = Path(path)
        self.enabled = enabled is True
        self.read_only = read_only is True
        self._mailbox = None
        self._init_lock = threading.RLock()

    @property
    def mailbox(self):
        if not self.enabled:
            raise DotMailboxDisabled("dot conversation is disabled")
        with self._init_lock:
            if self._mailbox is None:
                self._mailbox = DotMailbox(self.path,read_only=self.read_only)
        return self._mailbox

    def close(self):
        if self._mailbox is not None:
            self._mailbox.close()
            self._mailbox = None

    def open(self, owner_account_id, parent_dot_ref):
        return self.mailbox.open(owner_account_id,parent_dot_ref)

    def existing(self, owner_account_id, parent_dot_ref):
        return self.mailbox.existing(owner_account_id,parent_dot_ref)

    def send(self, conversation_id, owner_account_id, **values):
        return self.mailbox.send(conversation_id,owner_account_id,**values)

    def read(self, scope, **values):
        return self.mailbox.read(scope,**values)

    def reply(self, scope, **values):
        return self.mailbox.reply(scope,**values)

    def public_snapshot(self, conversation_id, owner_account_id, *, parent_dot_ref=None):
        value = self.mailbox.snapshot(conversation_id,owner_account_id,parent_dot_ref=parent_dot_ref)
        conversation = value["conversation"]
        messages = [{
            "id": m["id"], "ordinal":m["ordinal"], "text":m["text"],
            "origin":m["origin"], "created_at":m["created_at"],
            "state":"replied" if m["reply_id"] else "waiting_for_dot",
            "reply": ({"id":m["reply_id"],"text":m["reply_text"],
                       "source":m["reply_source"],"created_at":m["replied_at"]}
                      if m["reply_id"] else None),
        } for m in value["messages"]]
        return {"schema_version":1,"backend":"dot_mailbox",
                "transport":"manual_task_relay","automatic_wakeup":False,
                "conversation":{key:conversation[key] for key in
                                ("id","title","revision","created_at","updated_at")}
                  | {"kind":"dot","thread_id":None,
                     "state":"waiting_for_dot" if any(m["reply"] is None for m in messages) else "idle",
                     "reply_generation":max((m["reply_generation"] or 0 for m in value["messages"]),default=0)},
                "messages":messages}
