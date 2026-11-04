"""JSONL controller for one disposable full instance recovery drill."""
import hashlib, json, sqlite3, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_instance_manager import IsolatedRecoveryTests
from shared import instance_recovery as recovery
from engine.conversation_store import ConversationStore

case = None
def emit(value): print(json.dumps(value, separators=(',', ':')), flush=True)
def init():
    global case
    case=IsolatedRecoveryTests('runTest'); case.setUp(); case.prepare()
    root=case.root; (root/'.recovery-drill-fixture').write_text('isolated\n')
    # Remove prepare()'s illustrative data: browser registration and the UI-created
    # conversation are the only state subsequently captured.
    for name in ('conversations.sqlite3','auth.sqlite3'):
        (root/'state'/name).unlink(missing_ok=True)
    for name in ('conversation-artifacts','message-attachments'):
        import shutil; shutil.rmtree(root/'state'/name,ignore_errors=True)
    (root/'native-home'/'auth.json').write_text('{"synthetic_only": true}\n')
    return {'root':str(root),'account':case.config['account']}
def source_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ('state', 'workspace', 'native-home')
            for path in (root/name).rglob('*') if path.is_file() and not path.is_symlink()}

def backup_restore():
    root=case.root
    store=ConversationStore(root/'state/conversations.sqlite3')
    row=store._db.execute('SELECT id FROM conversations LIMIT 1').fetchone()
    if not row: raise RuntimeError('browser fixture did not create conversation')
    if store.command('start-drill')['state'] != 'completed':
        raise RuntimeError('original native turn has not completed')
    for ident,state in (('queued-drill','queued'),('sending-drill','submitted')):
        store.begin_command(row[0],ident,kind='start',text='synthetic pending input')
        if state == 'submitted':
            store.update_command(ident,state,reason='native_rpc_sending')
    store.close()
    with mock.patch.object(recovery,'unit_status',return_value='inactive'):
        captured=recovery.backup(case.config)
        before=source_hashes(root)
        target=case.home/'restored-drill'
        result=recovery.restore(case.config,{'backup':captured['backup'],'destination':str(target)})
    source_unchanged=before == source_hashes(root)
    native_auth_excluded=not (target/'native-home'/'auth.json').exists()
    (target/'.recovery-drill-fixture').write_text('isolated\n')
    db=sqlite3.connect(target/'state/conversations.sqlite3'); unknown=[r[0] for r in db.execute("SELECT id FROM commands WHERE state='delivery_unknown' ORDER BY id")]; db.close()
    auth=sqlite3.connect(target/'state/auth.sqlite3'); sessions=auth.execute('SELECT count(*) FROM admin_sessions').fetchone()[0]; revoked=auth.execute('SELECT count(*) FROM passkey_credentials WHERE revoked_at IS NOT NULL').fetchone()[0]; auth.close()
    return {'root':str(target),'source_unchanged':source_unchanged,'native_auth_excluded':native_auth_excluded,'delivery_unknown':unknown,'sessions':sessions,'credentials_revoked':revoked,'commands_replayed':result['restore_summary']['commands_replayed'],'activated':result['restore_summary']['activated']}
def main():
    global case
    try:
        for line in sys.stdin:
            op='decode'
            try:
                command=json.loads(line); op=command.get('op')
                if op=='init': emit({'ok':True,**init()})
                elif op=='backup_restore': emit({'ok':True,**backup_restore()})
                elif op=='shutdown':
                    if case is not None:
                        case.doCleanups(); case = None
                    emit({'ok':True}); return
                else: raise ValueError('unknown operation')
            except Exception as error:
                emit({'ok':False,'error':type(error).__name__, 'operation':op,
                      'code':getattr(error,'code','fixture_error')})
    finally:
        if case is not None: case.doCleanups()
if __name__=='__main__': main()
