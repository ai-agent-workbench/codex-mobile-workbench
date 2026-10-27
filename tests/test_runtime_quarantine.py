import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from shared.runtime_quarantine import quarantine


class PinnedQuarantineTests(unittest.TestCase):
    def test_barrier_does_not_import_candidate_and_never_replays_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / 'old'
            (previous / 'app/shared').mkdir(parents=True)
            # Old release doesn't support fixed attachment delivery.
            (previous / 'app/shared/message_attachments.py').write_text('raise ImportError("old")\n')
            path = root / 'conversation.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE message_attachments(id TEXT PRIMARY KEY);
              CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE commands(id TEXT PRIMARY KEY,conversation_id TEXT,state TEXT,reason TEXT,payload_json TEXT,updated_at TEXT);
              CREATE TABLE conversations(id TEXT PRIMARY KEY,state TEXT,recovery_state TEXT,updated_at TEXT,revision INTEGER);''')
            db.execute("INSERT INTO conversations VALUES('c','running',NULL,NULL,1)")
            payload = json.dumps({'attachments':[{'id':'fixed'}]})
            for id, state, reason in [('unsent','queued',None),('sending','submitted','native_rpc_sending'),('unknown','delivery_unknown','keep')]:
                db.execute('INSERT INTO commands VALUES(?,?,?,?,?,NULL)',(id,'c',state,reason,payload))
            db.commit()
            before = db.execute("SELECT state,reason,payload_json FROM commands WHERE id='unknown'").fetchone()
            with mock.patch.dict('sys.modules', {'shared.message_attachments':None, 'engine.conversation_store':None}):
                self.assertEqual(quarantine(previous,path),2)
            self.assertEqual(db.execute("SELECT state FROM commands WHERE id='unsent'").fetchone()[0],'rejected')
            self.assertEqual(db.execute("SELECT state FROM commands WHERE id='sending'").fetchone()[0],'delivery_unknown')
            self.assertEqual(db.execute("SELECT state,reason,payload_json FROM commands WHERE id='unknown'").fetchone(),before)
            self.assertEqual(db.execute("SELECT state FROM conversations WHERE id='c'").fetchone()[0],'needs_user')
            db.close()
