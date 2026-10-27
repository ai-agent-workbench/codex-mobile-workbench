"""Private durable subscription/outbox state, separate from the dot mailbox."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

from engine.dot_webhooks import RelayError, json_bytes


_TABLES = {"relay_meta","relay_subscriptions","relay_outbox"}


class DotEventStore:
    def __init__(self, path, binding):
        requested = Path(path).absolute()
        if requested.name != "dot-events.sqlite3" or requested.is_symlink():
            raise RelayError(-32012,"only the dedicated events database is accepted")
        self.path = requested
        self.path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        if os.name == "posix":
            directory = self.path.parent.stat()
            if directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode)&0o077:
                raise RelayError(-32012,"events storage directory must be owner-only")
        try:
            fd = os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if os.name == "posix":
            for item in (self.path,self.path.parent):
                metadata = item.stat()
                if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:
                    raise RelayError(-32012,"events storage must be private to the OS user")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path,timeout=5,isolation_level=None,check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        fingerprint = hashlib.sha256(json_bytes(binding)).hexdigest()
        try:
            existing = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and existing != _TABLES:
                raise RelayError(-32012,"database is not the dedicated events store")
            if existing and dict(self._db.execute("SELECT key,value FROM relay_meta")) != {
                    "schema_version":"1","binding":fingerprint}:
                raise RelayError(-32012,"events store binding or schema changed")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS relay_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS relay_subscriptions(
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
                    arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
                    previous_secret TEXT, rotate_until REAL NOT NULL,
                    expires_at REAL NOT NULL,active INTEGER NOT NULL,
                    scan_ordinal INTEGER NOT NULL,generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relay_outbox(
                    subscription_id TEXT NOT NULL REFERENCES relay_subscriptions(id),
                    message_id TEXT NOT NULL,event_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
                    body BLOB NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,
                    next_at REAL NOT NULL,first_at REAL,lease_token TEXT,lease_until REAL,
                    generation INTEGER NOT NULL,last_reason TEXT,
                    PRIMARY KEY(subscription_id,message_id)
                );
                COMMIT;
            """)
            with self.transaction():
                self._db.execute("INSERT OR IGNORE INTO relay_meta VALUES('schema_version','1')")
                self._db.execute("INSERT OR IGNORE INTO relay_meta VALUES('binding',?)",(fingerprint,))
        except BaseException:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _expire(self, now):
        self._db.execute("""UPDATE relay_subscriptions SET active=0,secret='',previous_secret=NULL,
                            generation=generation+1 WHERE active=1 AND expires_at<=?""",(now,))
        self._db.execute("""UPDATE relay_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL,
                            last_reason='subscription_inactive' WHERE status IN ('queued','leased')
                            AND subscription_id IN (SELECT id FROM relay_subscriptions WHERE active=0)""")

    def subscribe(self, *, sid, principal, name, arguments, url, secret, expires_at, head, now):
        with self.transaction():
            self._expire(now)
            previous = self._db.execute("SELECT * FROM relay_subscriptions WHERE id=?",(sid,)).fetchone()
            if previous is None or not previous["active"]:
                if self._db.execute("SELECT COUNT(*) FROM relay_subscriptions WHERE active=1").fetchone()[0]:
                    raise RelayError(-32013,"only one active callback is allowed",{"limit":"subscriptions","max":1})
                generation = previous["generation"]+1 if previous else 1
                self._db.execute("""INSERT INTO relay_subscriptions VALUES(?,?,?,?,?,?,NULL,0,?,1,?,?)
                    ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,previous_secret=NULL,rotate_until=0,
                    expires_at=excluded.expires_at,active=1,scan_ordinal=excluded.scan_ordinal,
                    generation=excluded.generation""",
                    (sid,principal,name,arguments,url,secret,expires_at,head,generation))
            else:
                rotated = previous["secret"] != secret
                self._db.execute("""UPDATE relay_subscriptions SET secret=?,previous_secret=?,rotate_until=?,
                                    expires_at=? WHERE id=?""",
                    (secret,previous["secret"] if rotated else previous["previous_secret"],
                     now+300 if rotated else previous["rotate_until"],expires_at,sid))

    def unsubscribe(self, sid, now):
        with self.transaction():
            self._expire(now)
            self._db.execute("""UPDATE relay_subscriptions SET active=0,secret='',previous_secret=NULL,
                                generation=generation+1 WHERE id=? AND active=1""",(sid,))
            self._db.execute("""UPDATE relay_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL,
                                last_reason='unsubscribed' WHERE subscription_id=?
                                AND status IN ('queued','leased')""",(sid,))

    def enqueue(self, messages, conversation_id, now):
        inserted = 0
        with self.transaction():
            self._expire(now)
            subscriptions = list(self._db.execute("SELECT * FROM relay_subscriptions WHERE active=1"))
            for subscription in subscriptions:
                head = subscription["scan_ordinal"]
                for message in messages:
                    if message["ordinal"] <= head:
                        continue
                    event_id = "evt_dot_"+hashlib.sha256(json_bytes(
                        [conversation_id,message["id"]])).hexdigest()
                    body = json_bytes({"eventId":event_id,"name":subscription["name"],
                        "timestamp":message["created_at"],"data":{
                            "conversation_id":conversation_id,"message_id":message["id"],
                            "ordinal":message["ordinal"],"revision":message["accepted_revision"]},
                        "cursor":None})
                    cursor = self._db.execute("""INSERT OR IGNORE INTO relay_outbox VALUES(
                        ?,?,?,?,?, 'queued',0,?,NULL,NULL,NULL,?,NULL)""",
                        (subscription["id"],message["id"],event_id,message["ordinal"],body,
                         now,subscription["generation"]))
                    inserted += cursor.rowcount
                    head = message["ordinal"]
                self._db.execute("UPDATE relay_subscriptions SET scan_ordinal=? WHERE id=?",
                                 (head,subscription["id"]))
        return inserted

    def claim(self, now):
        with self.transaction():
            self._expire(now)
            self._db.execute("""UPDATE relay_outbox SET status='queued',lease_token=NULL,lease_until=NULL
                                WHERE status='leased' AND lease_until<=?""",(now,))
            row = self._db.execute("""SELECT * FROM relay_outbox WHERE status IN ('queued','leased')
                                      ORDER BY ordinal LIMIT 1""").fetchone()
            if row is None or row["status"] == "leased" or row["next_at"] > now:
                return None
            if row["attempts"] >= 5 or (row["first_at"] is not None and now-row["first_at"] >= 900):
                self._db.execute("""UPDATE relay_outbox SET status='abandoned',last_reason='retry_limit'
                                    WHERE subscription_id=? AND message_id=?""",
                                 (row["subscription_id"],row["message_id"]))
                return None
            token = uuid.uuid4().hex
            self._db.execute("""UPDATE relay_outbox SET status='leased',attempts=attempts+1,
                                first_at=COALESCE(first_at,?),lease_token=?,lease_until=?
                                WHERE subscription_id=? AND message_id=?""",
                             (now,token,now+30,row["subscription_id"],row["message_id"]))
            return {"subscription_id":row["subscription_id"],"message_id":row["message_id"],
                    "generation":row["generation"],"lease_token":token}

    def delivery(self, job, now):
        with self.transaction():
            self._expire(now)
            row = self._db.execute("""SELECT q.*,s.url,s.secret,s.previous_secret,s.rotate_until,
                                      s.principal,s.active,s.expires_at,s.generation AS current_generation
                FROM relay_outbox q JOIN relay_subscriptions s ON s.id=q.subscription_id
                WHERE q.subscription_id=? AND q.message_id=? AND q.lease_token=? AND q.status='leased'""",
                (job["subscription_id"],job["message_id"],job["lease_token"])).fetchone()
            if row is None or not row["active"] or row["generation"] != row["current_generation"]:
                return None
            return dict(row)

    def finish(self, job, *, status, reason, next_at):
        if status not in {"queued","delivered","abandoned","cancelled"}:
            raise ValueError("invalid delivery outcome")
        with self.transaction():
            self._db.execute("""UPDATE relay_outbox SET status=?,last_reason=?,next_at=?,
                                lease_token=NULL,lease_until=NULL WHERE subscription_id=? AND message_id=?
                                AND status='leased' AND lease_token=? AND generation=?""",
                             (status,reason,next_at,job["subscription_id"],job["message_id"],
                              job["lease_token"],job["generation"]))

    def counts(self):
        # Safe operational summary: no callback URLs, signing secrets or bodies.
        with self._lock:
            return dict(self._db.execute("SELECT status,COUNT(*) FROM relay_outbox GROUP BY status"))
