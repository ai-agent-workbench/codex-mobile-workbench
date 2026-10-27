from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from shared import workbench_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state, self.workspace = self.root / 'state', self.root / 'workspace'
        self.state.mkdir(); self.workspace.mkdir()
        (self.workspace / 'c').mkdir()
        (self.workspace / 'c/input.txt').write_text('workspace')
        blob = self.state / 'message-attachments/a1/name.txt'
        blob.parent.mkdir(parents=True); blob.write_bytes(b'attachment')
        self.record = {'id':'a1', 'name':'name.txt', 'batch_id':'b1', 'owner_account_id':'owner',
                       'scope_kind':'conversation', 'scope_id':'c', 'path':str(blob),
                       'original_path':'/original/user/input.txt', 'size':blob.stat().st_size,
                       'sha256':hashlib.sha256(blob.read_bytes()).hexdigest(),
                       'identity':recovery._identity(blob)}
        artifact = self.state / 'conversation-artifacts/a.txt'
        artifact.parent.mkdir(); artifact.write_bytes(b'artifact')
        with sqlite3.connect(self.state / 'conversations.sqlite3') as db:
            db.executescript('''
                CREATE TABLE conversations(id TEXT PRIMARY KEY,cwd TEXT);
                CREATE TABLE message_attachments(id TEXT PRIMARY KEY,batch_id TEXT,owner_account_id TEXT,
                    scope_kind TEXT,scope_id TEXT,record_json TEXT,bound_once INTEGER);
                CREATE TABLE message_attachment_bindings(attachment_id TEXT REFERENCES message_attachments(id),
                    conversation_id TEXT REFERENCES conversations(id));
                CREATE TABLE commands(id TEXT PRIMARY KEY,conversation_id TEXT,state TEXT,reason TEXT,payload_json TEXT);
                CREATE TABLE artifacts(storage_relpath TEXT,size INTEGER,sha256 TEXT);
            ''')
            db.execute('INSERT INTO conversations VALUES(?,?)', ('c',str(self.workspace / 'c')))
            db.execute('INSERT INTO conversations VALUES(?,?)', ('external','/unmaterialized/project'))
            db.execute('INSERT INTO message_attachments VALUES(?,?,?,?,?,?,?)',
                       ('a1','b1','owner','conversation','c',json.dumps(self.record),1))
            db.execute('INSERT INTO message_attachment_bindings VALUES(?,?)', ('a1','c'))
            for command, state, reason in [('terminal','completed','done'), ('unknown','delivery_unknown','native_rpc_sending'),
                                           ('queued','queued','waiting'), ('accepted','accepted','started'),
                                           ('submitted','submitted','native_rpc_sending')]:
                payload = json.dumps({'attachments':[self.record], 'context_digest':'unchanged', 'plan_digest':'unchanged'})
                db.execute('INSERT INTO commands VALUES(?,?,?,?,?)', (command,'c',state,reason,payload))
            db.execute('INSERT INTO artifacts VALUES(?,?,?)',
                       ('a.txt',artifact.stat().st_size,hashlib.sha256(artifact.read_bytes()).hexdigest()))
        (self.state / 'session_secret').write_text('synthetic-private-fixture')
        (self.state / 'session_secret').chmod(0o600)
        self.bundle = self.root / 'bundle'

    def tearDown(self):
        self.temporary.cleanup()

    def _database_rows(self, database):
        with sqlite3.connect(database) as db:
            return db.execute('SELECT id,state,reason,payload_json FROM commands ORDER BY id').fetchall()

    def test_build_verify_and_restore_do_not_change_original_or_activate(self):
        original_db = (self.state / 'conversations.sqlite3').read_bytes()
        original_blob = (self.state / 'message-attachments/a1/name.txt').read_bytes()
        unit = self.root / 'web.service'; unit.write_text('synthetic service')
        manifest = recovery.build_recovery_bundle(self.state,self.workspace,self.bundle,unit_inputs={'web.service':unit})
        self.assertFalse(manifest['writers_stopped'])
        self.assertEqual(manifest['closure'], {'attachments':1,'artifacts':1,'bindings':1,'command_references':5,'checked':True})
        self.assertEqual(recovery.verify_recovery_bundle(self.bundle), manifest)
        self.assertEqual((self.bundle / 'state/session_secret').stat().st_mode & 0o777, 0o400)
        output = self.root / 'restored'
        report = recovery.restore_recovery_bundle(self.bundle, output)
        self.assertFalse(report['activated']); self.assertFalse(report['commands_replayed'])
        self.assertIn('identity_rebind', report['attachments'])
        self.assertEqual(original_db,(self.state / 'conversations.sqlite3').read_bytes())
        self.assertEqual(original_blob,(self.state / 'message-attachments/a1/name.txt').read_bytes())
        self.assertEqual(self._database_rows(self.state / 'conversations.sqlite3'), self._database_rows(output / 'state/conversations.sqlite3'))

    def test_online_backup_captures_committed_wal_without_checkpointing_source(self):
        db = sqlite3.connect(self.state / 'conversations.sqlite3')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("INSERT INTO conversations VALUES('wal','/external')"); db.commit()
            wal = self.state / 'conversations.sqlite3-wal'
            before = wal.read_bytes()
            recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
            self.assertEqual(wal.read_bytes(),before)
            with sqlite3.connect(self.bundle / 'state/conversations.sqlite3') as copy:
                self.assertEqual(copy.execute("SELECT id FROM conversations WHERE id='wal'").fetchone()[0],'wal')
        finally:
            db.close()

    def test_restore_rebinds_registry_and_terminal_refs_but_preserves_inflight_bytes(self):
        before = self._database_rows(self.state / 'conversations.sqlite3')
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        manifest_before = (self.bundle / recovery._MANIFEST).read_bytes()
        output = self.root / 'restored'
        report = recovery.restore_recovery_bundle(self.bundle,output,rebind_attachments=True)
        with sqlite3.connect(output / 'state/conversations.sqlite3') as db:
            record = json.loads(db.execute('SELECT record_json FROM message_attachments').fetchone()[0])
            terminal = json.loads(db.execute("SELECT payload_json FROM commands WHERE id='terminal'").fetchone()[0])
            self.assertEqual(db.execute('SELECT * FROM message_attachment_bindings').fetchone(),('a1','c'))
            self.assertEqual(db.execute("SELECT cwd FROM conversations WHERE id='c'").fetchone()[0],str(output / 'workspace/c'))
            self.assertEqual(db.execute("SELECT cwd FROM conversations WHERE id='external'").fetchone()[0],'/unmaterialized/project')
        new_blob = output / 'state/message-attachments/a1/name.txt'
        self.assertEqual(record['path'],str(new_blob)); self.assertEqual(record['identity'],recovery._identity(new_blob))
        self.assertNotEqual(record['identity'][1],self.record['identity'][1])
        self.assertEqual({k:v for k,v in record.items() if k not in {'path','identity'}},
                         {k:v for k,v in self.record.items() if k not in {'path','identity'}})
        self.assertEqual(terminal['attachments'][0],record)
        self.assertEqual(terminal['context_digest'],'unchanged'); self.assertEqual(terminal['plan_digest'],'unchanged')
        after = self._database_rows(output / 'state/conversations.sqlite3')
        self.assertEqual([row for row in before if row[0]!='terminal'],[row for row in after if row[0]!='terminal'])
        self.assertEqual({row['id'] for row in report['unresolved_commands']},{'unknown','queued','accepted','submitted'})
        self.assertEqual(report['cwd_not_materialized'],['external'])
        self.assertEqual((self.bundle / recovery._MANIFEST).read_bytes(),manifest_before)
        recovery.verify_recovery_bundle(self.bundle)

    def test_verification_does_not_require_live_sources(self):
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        self.state.rename(self.root / 'removed-state'); self.workspace.rename(self.root / 'removed-workspace')
        recovery.verify_recovery_bundle(self.bundle)
        recovery.restore_recovery_bundle(self.bundle,self.root / 'restore',rebind_attachments=True)

    def test_missing_or_drifting_reference_closure_rejected(self):
        for drift in ('missing','hash','registry-columns','command-path','command-identity','artifact'):
            with self.subTest(drift=drift):
                db = self.state / 'conversations.sqlite3'; original = db.read_bytes()
                blob = self.state / 'message-attachments/a1/name.txt'; original_blob = blob.read_bytes()
                if drift == 'missing': blob.unlink()
                elif drift == 'hash': blob.write_bytes(b'changed')
                else:
                    with sqlite3.connect(db) as connection:
                        if drift == 'registry-columns': connection.execute("UPDATE message_attachments SET owner_account_id='different'")
                        elif drift == 'artifact': connection.execute("UPDATE artifacts SET sha256='wrong'")
                        else:
                            payload = json.loads(connection.execute("SELECT payload_json FROM commands WHERE id='terminal'").fetchone()[0])
                            payload['attachments'][0]['path' if drift=='command-path' else 'identity'] = 'invalid'
                            connection.execute("UPDATE commands SET payload_json=? WHERE id='terminal'",(json.dumps(payload),))
                with self.assertRaises(recovery.RecoveryError):
                    recovery.build_recovery_bundle(self.state,self.workspace,self.root / ('bad-'+drift))
                db.write_bytes(original)
                if not blob.exists() or blob.read_bytes()!=original_blob: blob.write_bytes(original_blob)
                # Changing inode/mtime for the next subcase must update the synthetic registry.
                self.record['identity']=recovery._identity(blob)
                with sqlite3.connect(db) as connection:
                    connection.execute('UPDATE message_attachments SET record_json=?',(json.dumps(self.record),))
                    for row in connection.execute('SELECT id,payload_json FROM commands').fetchall():
                        payload=json.loads(row[1]); payload['attachments']=[self.record]
                        connection.execute('UPDATE commands SET payload_json=? WHERE id=?',(json.dumps(payload),row[0]))

    def test_verify_rechecks_closure_even_when_database_hash_is_recomputed(self):
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        db=self.bundle / 'state/conversations.sqlite3'; db.chmod(0o600)
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE artifacts SET sha256='false'")
        db.chmod(0o400)
        path=self.bundle / recovery._MANIFEST; path.chmod(0o600)
        manifest=json.loads(path.read_text()); manifest['files']['state/conversations.sqlite3']['sha256']=recovery._hash(db)
        manifest['databases']['conversations.sqlite3']=manifest['files']['state/conversations.sqlite3']
        path.write_text(json.dumps(manifest)); path.chmod(0o400)
        with self.assertRaises(recovery.RecoveryError): recovery.verify_recovery_bundle(self.bundle)

    def test_source_links_hardlinks_and_extra_bundle_entries_fail_closed(self):
        bad=self.workspace / 'bad'
        for kind in ('link','hardlink'):
            if kind=='link': bad.symlink_to('/tmp')
            else: os.link(self.workspace / 'c/input.txt',bad)
            with self.assertRaises((recovery.RecoveryError,ValueError)):
                recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
            bad.unlink()
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        extra=self.bundle / 'workspace/recovery-bundle.json';extra.write_text('extra');extra.chmod(0o400)
        with self.assertRaises(recovery.RecoveryError): recovery.verify_recovery_bundle(self.bundle)

    def test_tree_enumeration_error_is_not_accepted_as_empty(self):
        with mock.patch.object(recovery,'_entries',side_effect=PermissionError('cannot enumerate')):
            with self.assertRaises(PermissionError): recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(list(self.root.glob('.bundle.building-*')))

    def test_destination_is_disjoint_and_atomic_no_overwrite(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.build_recovery_bundle(self.state,self.workspace,self.state / 'bundle')
        actual=recovery._rename_noreplace
        def race(stage,destination):
            destination.mkdir(); (destination / 'other-owner-data').write_text('preserve')
            actual(stage,destination)
        with mock.patch.object(recovery,'_rename_noreplace',side_effect=race):
            with self.assertRaises(ValueError): recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        self.assertEqual((self.bundle / 'other-owner-data').read_text(),'preserve')
        self.assertFalse(list(self.root.glob('.bundle.building-*')))

    def test_restore_staging_is_private_before_any_byte_is_copied(self):
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        original = recovery.shutil.copytree
        def observe(source,target,*args,**kwargs):
            if Path(source)==self.bundle:
                self.assertTrue(target.exists())
                self.assertEqual(target.stat().st_mode & 0o777,0o700)
            return original(source,target,*args,**kwargs)
        umask=os.umask(0o022)
        try:
            with mock.patch.object(recovery.shutil,'copytree',side_effect=observe):
                recovery.restore_recovery_bundle(self.bundle,self.root/'restored')
        finally: os.umask(umask)

    def test_restore_copy_drift_is_rejected_before_rebind(self):
        recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)
        original=recovery.shutil.copytree
        def tamper(source,target,*args,**kwargs):
            result=original(source,target,*args,**kwargs)
            if Path(source)==self.bundle:
                path=target/'state/message-attachments/a1/name.txt'
                path.chmod(0o600);path.write_bytes(b'changed-during-copy');path.chmod(0o400)
            return result
        with mock.patch.object(recovery.shutil,'copytree',side_effect=tamper):
            with self.assertRaises(recovery.RecoveryError):
                recovery.restore_recovery_bundle(self.bundle,self.root/'restored',rebind_attachments=True)
        self.assertFalse((self.root/'restored').exists())
        self.assertFalse(list(self.root.glob('.restored.restore-*')))
        recovery.verify_recovery_bundle(self.bundle)

    def test_bound_command_without_binding_is_rejected(self):
        with sqlite3.connect(self.state/'conversations.sqlite3') as db:
            db.execute('DELETE FROM message_attachment_bindings')
        with self.assertRaises(recovery.RecoveryError):
            recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)

    def test_foreign_key_failure_rejected(self):
        with sqlite3.connect(self.state / 'conversations.sqlite3') as db:
            db.execute("INSERT INTO message_attachment_bindings VALUES('missing','c')")
        with self.assertRaises(recovery.RecoveryError):
            recovery.build_recovery_bundle(self.state,self.workspace,self.bundle)


if __name__ == '__main__':
    unittest.main()
