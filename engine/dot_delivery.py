"""Dot-only read receipts and at-most-once notification attempts.

This sidecar never changes the mailbox, reply IDs, native turns or subscriptions.
Only reply IDs/revisions/timestamps are consumed. No conversation text or ntfy
recipient is read here; the existing task sender owns its target configuration.
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone
from engine.dot_mailbox import DotMailboxDenied, DotMailboxError

class DotDeliveryStore:
    def __init__(self, path):
        path = Path(path)
        if path.is_symlink():
            raise DotMailboxDenied("dot delivery database must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS reads(owner TEXT,cid TEXT,seen INTEGER NOT NULL,PRIMARY KEY(owner,cid));
          CREATE TABLE IF NOT EXISTS notification_bindings(owner TEXT,cid TEXT,parent TEXT,baseline INTEGER NOT NULL,
            PRIMARY KEY(owner,cid));
          CREATE TABLE IF NOT EXISTS notices(owner TEXT,cid TEXT,id TEXT,state TEXT NOT NULL,
            PRIMARY KEY(owner,cid,id));
        """)

    def close(self):
        with self.lock: self.db.close()

    def seen(self, owner, cid):
        with self.lock:
            row = self.db.execute("SELECT seen FROM reads WHERE owner=? AND cid=?", (owner,cid)).fetchone()
            return row[0] if row else 0

    def mark_read(self, owner, cid, generation, *, latest):
        if isinstance(generation,bool) or not isinstance(generation,int) or not 0 <= generation <= latest:
            raise DotMailboxError("reply generation is outside the displayed conversation")
        with self.lock:
            self.db.execute("INSERT INTO reads VALUES(?,?,?) ON CONFLICT(owner,cid) DO UPDATE SET seen=MAX(seen,excluded.seen)",
                            (owner,cid,generation))
        return self.seen(owner,cid)

    def arm(self, owner, cid, parent, generation):
        # First enable establishes a high-water mark: historical replies never notify.
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO notification_bindings VALUES(?,?,?,?)",(owner,cid,parent,generation))
            row=self.db.execute("SELECT parent,baseline FROM notification_bindings WHERE owner=? AND cid=?",(owner,cid)).fetchone()
            if row[0] != parent: raise DotMailboxDenied("notification parent binding changed")
            return row[1]

    def claim(self, owner, cid, notice_id, *, eligible=True):
        # Persist before external delivery. A crash in flight leaves sending,
        # which is deliberately never retried (delivery may have succeeded).
        with self.lock:
            result=self.db.execute("INSERT OR IGNORE INTO notices VALUES(?,?,?,?)",
                (owner,cid,notice_id,"sending" if eligible else "expired"))
            return eligible and result.rowcount == 1

    def finish(self, owner, cid, notice_id, success):
        with self.lock:
            self.db.execute("UPDATE notices SET state=? WHERE owner=? AND cid=? AND id=? AND state='sending'",
                            ("sent" if success else "unknown",owner,cid,notice_id))

    def record_link_status(self, owner, cid, valid, status):
        with self.lock:
            self.db.execute("INSERT INTO notices VALUES(?,?,?,?) ON CONFLICT(owner,cid,id) DO UPDATE SET state=excluded.state",
                (owner,cid,"link:canonical",("verified_" if valid else "unverified_")+str(int(status))))

    def notice_state(self, owner, cid, notice_id):
        with self.lock:
            row=self.db.execute("SELECT state FROM notices WHERE owner=? AND cid=? AND id=?",(owner,cid,notice_id)).fetchone()
            return row[0] if row else None


def deliver_pending(store, summary, *, owner, cid, parent, sender, link, now=None):
    baseline=store.arm(owner,cid,parent,summary["reply_generation"])
    now=now or datetime.now(timezone.utc)
    count=0
    for reply in summary["replies"]:
        if reply["written_revision"] <= baseline: continue
        try: age=(now-datetime.fromisoformat(reply["created_at"])).total_seconds()
        except (ValueError,TypeError): age=-1
        if not store.claim(owner,cid,"reply:"+reply["id"],eligible=0 <= age <= 600): continue
        try:
            success,_=sender("dot 有新回复", "dot 有新回复\n\n[打开工作台]("+link+")", tags="white_check_mark")
        except Exception:
            success=False
        store.finish(owner,cid,"reply:"+reply["id"],success)
        count+=1
    return count
