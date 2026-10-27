"""Runtime/API native callback acceptance and no-replay regressions."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request
from engine.codex_app_server import AppServerProtocolError, AppServerRPCError, CodexAppServerClient
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from routes import conversation_api
from tests.test_conversation_runtime import FakeFactory
from tests.store_fixtures import seeded_store

COMMAND = 'item/commandExecution/requestApproval'
FILE = 'item/fileChange/requestApproval'
PERMISSIONS = 'item/permissions/requestApproval'
MCP = 'mcpServer/elicitation/request'

class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = seeded_store(self.root/'db.sqlite3')
        self.store.create_conversation('conv', title='native', cwd=str(self.root), owner_account_id='owner', settings={
            'model':'fixture', 'effort':'medium', 'permission':'workspace-write', 'collaboration_mode':'plan'})
        self.store.bind_thread('conv', {'id':'thread'}, server_version='0.159.2')
        self.store.begin_command('conv','start',kind='start',text='test')
        self.store.bind_turn('conv',{'id':'turn','status':'inProgress'},command_id='start')
        self.artifacts = ConversationArtifactService(self.store, self.root/'artifacts')
        self.runtime = ConversationRuntime(self.store, self.artifacts, FakeFactory())
        self.service = ConversationService(self.store,self.artifacts,self.runtime)
        self.tasks=[]

    async def asyncTearDown(self):
        await self.runtime.close()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close();self.tmp.cleanup()

    def params(self, method=COMMAND, **changes):
        value={'threadId':'thread','turnId':'turn','itemId':'item','startedAtMs':1}
        if method == COMMAND: value.update(command='echo native',availableDecisions=['accept','acceptForSession','decline','cancel'])
        if method == PERMISSIONS: value.update(cwd=str(self.root), permissions={'network':{'enabled':True}})
        if method == MCP: value={'threadId':'thread','turnId':None,'serverName':'fixture','mode':'form','message':'Fill in',
                                 'requestedSchema':{'type':'object','properties':{'token':{'type':'string'}},'required':['token']}}
        value.update(changes);return value

    async def ask(self, method=COMMAND, *, rpc=1, conn='connection:1', **changes):
        task=asyncio.create_task(self.runtime.handle_server_request(method,self.params(method,**changes),request_id=rpc,connection_id=conn))
        self.tasks.append(task);await asyncio.sleep(0)
        return task

    def record(self):return self.runtime.interactions.list('conv')[-1]

    async def answer(self, record=None, response=None, command_id='answer', **changes):
        r=record or self.record()
        kwargs={'interaction_id':r['id'],'command_id':command_id,'attempt':r['attempt'],'fingerprint':r['fingerprint'],
                'response':response or {'decision':'accept'},'answered_by':'owner'}
        kwargs.update(changes);return await self.runtime.answer_interaction('conv',**kwargs)

    async def test_approval_waits_for_real_ordered_turn_started_notification(self):
        self.store.create_conversation('early', title='early', cwd=str(self.root), owner_account_id='owner',
            settings={'model':'fixture','effort':'medium','permission':'workspace-write'})
        self.store.bind_thread('early', {'id':'early-thread'}, server_version='0.159.2')
        self.store.begin_command('early','early-start',kind='start',text='early')
        self.runtime._pending_turns['early-thread']=('early','early-start')
        entered, release, registered, sent = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def observe_status(conversation_id, snapshot):
            if conversation_id == 'early' and self.runtime.interactions.list('early'):
                registered.set()
        self.runtime._status_handler=observe_status

        async def delayed_notification(message, *, connection_id=None):
            entered.set()
            await release.wait()
            await self.runtime.handle_notification(message, connection_id=connection_id)

        driver=CodexAppServerClient(self.root/'unused-socket',request_timeout=2,
            notification_handler=delayed_notification,server_request_handler=self.runtime.handle_server_request)
        frames=[]
        async def enqueue(frame):
            frames.append(frame);sent.set()
        driver._enqueue=enqueue
        driver._connected=True;driver._connection_epoch=1
        driver._notification_task=asyncio.create_task(driver._notification_worker(1))
        self.runtime._client=driver
        await driver._notifications.put((1,{'method':'turn/started','params':{
            'threadId':'early-thread','turn':{'id':'early-turn','status':'inProgress'}}}))
        await asyncio.wait_for(entered.wait(),2)
        await driver._handle_raw(json.dumps({'id':8,'method':COMMAND,
            'params':self.params(threadId='early-thread',turnId='early-turn')}),1)
        request=next(iter(driver._callback_tasks));self.tasks.append(request)
        try:
            await asyncio.sleep(.01)
            self.assertFalse(request.done())
            self.assertEqual(self.runtime.interactions.list('early'),[])
            self.assertIsNone(self.store.conversation_for_turn('early-thread','early-turn'))
            release.set()
            await driver.flush_notifications()
            await asyncio.wait_for(registered.wait(),2)
            record=self.runtime.interactions.list('early')[0]
            await self.runtime.answer_interaction('early',interaction_id=record['id'],command_id='early-answer',
                attempt=record['attempt'],fingerprint=record['fingerprint'],response={'decision':'accept'},answered_by='owner')
            await asyncio.wait_for(sent.wait(),2)
            await request
            self.assertEqual(frames,[{'id':8,'result':{'decision':'accept'}}])
            self.assertEqual(self.store.command('early-start')['result_turn_id'],'early-turn')
        finally:
            release.set()
            await driver.close()
            self.runtime._client=None

    async def test_wire_resolved_after_request_is_not_lost_before_registration(self):
        entered, release, called = asyncio.Event(), asyncio.Event(), asyncio.Event()
        frames=[]
        async def blocked_notification(message, *, connection_id=None):
            if message['method']=='fixture/hold':
                entered.set();await release.wait()
            await self.runtime.handle_notification(message,connection_id=connection_id)
        async def request_handler(method, params, **kwargs):
            called.set()
            return await self.runtime.handle_server_request(method,params,**kwargs)
        driver=CodexAppServerClient(self.root/'unused-socket',request_timeout=2,
            notification_handler=blocked_notification,server_request_handler=request_handler)
        async def enqueue(frame): frames.append(frame)
        driver._enqueue=enqueue;driver._connected=True;driver._connection_epoch=1
        driver._notification_task=asyncio.create_task(driver._notification_worker(1))
        self.runtime._client=driver
        try:
            await driver._handle_raw(json.dumps({'method':'fixture/hold','params':{}}),1)
            await asyncio.wait_for(entered.wait(),2)
            await driver._handle_raw(json.dumps({'id':10,'method':COMMAND,'params':self.params()}),1)
            callback=next(iter(driver._callback_tasks));self.tasks.append(callback)
            await driver._handle_raw(json.dumps({'method':'serverRequest/resolved',
                'params':{'threadId':'thread','requestId':10}}),1)
            release.set()
            await driver.flush_notifications()
            await asyncio.wait_for(called.wait(),2)
            await asyncio.gather(callback,return_exceptions=True)
            self.assertFalse(frames)
            self.assertFalse(any(r['state']=='awaiting' for r in self.runtime.interactions.list('conv')))
            self.assertFalse(self.runtime._interaction_waiters)
            self.assertFalse(driver._notification_barriers)
        finally:
            release.set();await driver.close();self.runtime._client=None

    async def test_request_prefix_does_not_wait_for_a_later_notification(self):
        entered, release, registered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        frames=[]
        async def later_notification(message, *, connection_id=None):
            entered.set();await release.wait()
            await self.runtime.handle_notification(message,connection_id=connection_id)
        async def observe_status(conversation_id, snapshot):
            if self.runtime.interactions.list('conv'): registered.set()
        self.runtime._status_handler=observe_status
        driver=CodexAppServerClient(self.root/'unused-socket',request_timeout=2,
            notification_handler=later_notification,server_request_handler=self.runtime.handle_server_request)
        async def enqueue(frame): frames.append(frame)
        driver._enqueue=enqueue;driver._connected=True;driver._connection_epoch=1
        driver._notification_task=asyncio.create_task(driver._notification_worker(1))
        self.runtime._client=driver
        try:
            await driver._handle_raw(json.dumps({'id':12,'method':COMMAND,'params':self.params()}),1)
            callback=next(iter(driver._callback_tasks));self.tasks.append(callback)
            await driver._handle_raw(json.dumps({'method':'fixture/later','params':{}}),1)
            await asyncio.wait_for(entered.wait(),2)
            await asyncio.wait_for(registered.wait(),2)
            self.assertFalse(release.is_set())
            await self.answer(response={'decision':'decline'})
            await asyncio.wait_for(callback,2)
            self.assertEqual(frames,[{'id':12,'result':{'decision':'decline'}}])
        finally:
            release.set();await driver.close();self.runtime._client=None

    async def test_file_details_read_cannot_resurrect_resolved_request(self):
        reading, release = asyncio.Event(), asyncio.Event()
        frames=[]
        driver=CodexAppServerClient(self.root/'unused-socket',request_timeout=2,
            notification_handler=self.runtime.handle_notification,server_request_handler=self.runtime.handle_server_request)
        async def read_item(*args):
            reading.set();await release.wait()
            return {'type':'fileChange','changes':[]}
        async def enqueue(frame): frames.append(frame)
        driver.read_thread_item=read_item;driver._enqueue=enqueue
        driver._connected=True;driver._connection_epoch=1
        driver._notification_task=asyncio.create_task(driver._notification_worker(1));self.runtime._client=driver
        try:
            await driver._handle_raw(json.dumps({'id':13,'method':FILE,'params':self.params(FILE)}),1)
            callback=next(iter(driver._callback_tasks));self.tasks.append(callback)
            await asyncio.wait_for(reading.wait(),2)
            await driver._handle_raw(json.dumps({'method':'serverRequest/resolved','params':{
                'threadId':'thread','requestId':13}}),1)
            await driver.flush_notifications()
            release.set();await asyncio.gather(callback,return_exceptions=True)
            self.assertEqual(self.runtime.interactions.list('conv'),[])
            self.assertFalse(self.runtime._interaction_waiters)
            self.assertFalse(frames)
        finally:
            release.set();await driver.close();self.runtime._client=None

    async def test_file_details_read_rechecks_connection_and_delete_gate(self):
        for invalidation in ('disconnect','delete'):
            with self.subTest(invalidation=invalidation):
                reading, release = asyncio.Event(), asyncio.Event()
                frames=[]
                self.store.set_conversation_state('conv','active',native_delete_pending=False)
                driver=CodexAppServerClient(self.root/'unused-socket',request_timeout=2,
                    notification_handler=self.runtime.handle_notification,server_request_handler=self.runtime.handle_server_request)
                async def read_item(*args):
                    reading.set();await release.wait()
                    return {'type':'fileChange','changes':[]}
                async def enqueue(frame):frames.append(frame)
                driver.read_thread_item=read_item;driver._enqueue=enqueue
                driver._connected=True;driver._connection_epoch=1
                driver._notification_task=asyncio.create_task(driver._notification_worker(1));self.runtime._client=driver
                try:
                    await driver._handle_raw(json.dumps({'id':14,'method':FILE,'params':self.params(FILE)}),1)
                    callback=next(iter(driver._callback_tasks));self.tasks.append(callback)
                    await asyncio.wait_for(reading.wait(),2)
                    if invalidation=='disconnect':driver._connected=False
                    else:self.store.set_conversation_state('conv','needs_user',native_delete_pending=True)
                    release.set();await asyncio.gather(callback,return_exceptions=True)
                    self.assertEqual(self.runtime.interactions.list('conv'),[])
                    self.assertFalse(self.runtime._interaction_waiters)
                    self.assertFalse(any('result' in frame for frame in frames))
                finally:
                    release.set();await driver.close();self.runtime._client=None

    async def test_early_resolution_uses_typed_rpc_and_connection_identity(self):
        await self.runtime.handle_notification({'method':'serverRequest/resolved',
            'params':{'threadId':'thread','requestId':11}},connection_id='connection:1')
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.handle_server_request(COMMAND,self.params(),request_id=11,connection_id='connection:1')
        string_request=await self.ask(rpc='11')
        self.assertFalse(string_request.done())
        new_connection=await self.ask(rpc=11,conn='connection:2',approvalId='new')
        self.assertFalse(new_connection.done())
        self.assertEqual(len(self.runtime.interactions.list('conv')),2)

    async def test_native_callback_from_stale_connection_cannot_bind(self):
        driver=CodexAppServerClient(self.root/'unused-socket')
        self.runtime._client=driver
        try:
            with self.assertRaises(AppServerRPCError):
                await self.runtime.handle_server_request(COMMAND,self.params(),request_id=9,connection_id='old-connection')
            self.assertEqual(self.runtime.interactions.list('conv'),[])
        finally:
            self.runtime._client=None

    async def test_all_native_choices_and_no_timeout(self):
        task=await self.ask();r=self.record()
        self.assertFalse(task.done());self.assertFalse(self.runtime._question_deadlines)
        self.assertEqual(r['display']['availableDecisions'],['accept','acceptForSession','decline','cancel'])
        await self.answer(r,{'decision':'acceptForSession'})
        outcome=await task;self.assertEqual(outcome.result,{'decision':'acceptForSession'})
        await outcome.on_sent();self.assertEqual(self.record()['state'],'response_sent')
        self.assertEqual(self.store.get_conversation('conv')['state'],'active')
        await self.runtime.handle_notification({'method':'serverRequest/resolved','params':{'threadId':'thread','requestId':1}},connection_id='connection:1')
        self.assertEqual(self.record()['state'],'resolved')
        await self.answer(r,{'decision':'acceptForSession'}) # exact HTTP retry, no second wire send
        self.assertEqual(self.record()['state'],'resolved')

    async def test_owner_stale_attempt_cross_conversation_and_invalid_choice(self):
        task=await self.ask()
        for changes in ({'answered_by':'different-owner'},{'attempt':2},{'fingerprint':'wrong'},{'response':{'decision':'approve-everything'}}):
            with self.assertRaises((PermissionError,AppServerProtocolError)):
                await self.answer(**changes)
        self.assertFalse(task.done());self.assertEqual(self.record()['state'],'awaiting')
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_interaction('other',interaction_id=self.record()['id'],command_id='wrong',attempt=1,
                fingerprint=self.record()['fingerprint'],response={'decision':'accept'},answered_by='owner')

    async def test_typed_rpc_and_connection_scoped_resolved(self):
        a=await self.ask(rpc=7,approvalId='a');b=await self.ask(rpc='7',approvalId='b')
        await self.runtime.handle_notification({'method':'serverRequest/resolved','params':{'threadId':'thread','requestId':7}},connection_id='old')
        self.assertFalse(a.done());self.assertFalse(b.done())
        await self.runtime.handle_notification({'method':'serverRequest/resolved','params':{'threadId':'thread','requestId':7}},connection_id='connection:1')
        with self.assertRaises(asyncio.CancelledError):await a
        self.assertFalse(b.done())

    async def test_duplicate_frame_and_turn_terminal_never_reply(self):
        a=await self.ask();duplicate=await self.ask()
        with self.assertRaises(asyncio.CancelledError):await duplicate
        await self.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'interrupted'}}},connection_id='connection:1')
        with self.assertRaises(asyncio.CancelledError):await a
        self.assertEqual(self.record()['state'],'closed')
        with self.assertRaises(AppServerProtocolError):await self.answer()

    async def test_disconnect_requires_fresh_native_attempt_and_fresh_user_answer(self):
        first=await self.ask();old=self.record()
        self.runtime.interactions.connection_lost('connection:1')
        self.runtime._cancel_interaction_waiter(old['id'])
        with self.assertRaises(asyncio.CancelledError):await first
        second=await self.ask(conn='connection:2')
        current=self.record();self.assertEqual(current['id'],old['id']);self.assertEqual(current['attempt'],2)
        with self.assertRaises(AppServerProtocolError):await self.answer(old)
        self.assertFalse(second.done());await self.answer(current,{'decision':'decline'})
        self.assertEqual((await second).result,{'decision':'decline'})

    async def test_permissions_subset_scope_and_mcp_null_turn_secrecy(self):
        request=await self.ask(PERMISSIONS)
        await self.answer(response={'permissions':{'network':{'enabled':True}},'scope':'session','strictAutoReview':False})
        self.assertEqual((await request).result['scope'],'session')
        mcp=await self.ask(MCP,rpc=2)
        r=self.record();self.assertIsNone(r['turn_id'])
        await self.answer(r,{'action':'accept','content':{'token':'private-user-token'}},command_id='mcp-answer')
        outcome=await mcp;self.assertEqual(outcome.result['content']['token'],'private-user-token')
        self.assertNotIn('private-user-token',json.dumps(self.service.public_snapshot('conv')))
        with self.store._lock: dump='\n'.join(self.store._db.iterdump())
        self.assertNotIn('private-user-token',dump)
        public=self.service.public_snapshot('conv')['native_interactions'][-1]
        self.assertNotIn('logical_key',public);self.assertNotIn('answered_by',public);self.assertNotIn('connection_id',public)

    async def test_legacy_scheduled_scope_without_mode_retains_default_authorization(self):
        self.store._db.execute("UPDATE conversations SET settings_json=? WHERE id='conv'",
            (json.dumps({'model':'fixture','effort':'medium','permission':'workspace-write'}),))
        task = self.service.automations.create('conv','owner',title='legacy',prompt='read only',schedule={'kind':'interval','interval_minutes':30})
        task=self.service.automations.get(task['id'],'owner')
        scope={key:task[key] for key in ('owner_account_id','permission')}
        scope['cwd']=str(self.root)
        scope.update(task_id=task['id'], revision=task['revision'])
        with mock.patch.object(self.service,'_automation_owner_enabled',return_value=True):
            self.assertTrue(self.service._scheduled_guard({'payload':{'scheduled':scope}},self.store.get_conversation('conv')))
            self.store._db.execute("UPDATE conversations SET settings_json=? WHERE id='conv'",
                (json.dumps({'model':'fixture','effort':'medium','permission':'workspace-write','collaboration_mode':'plan'}),))
            self.assertFalse(self.service._scheduled_guard({'payload':{'scheduled':scope}},self.store.get_conversation('conv')))

    async def test_scheduled_callbacks_decline_without_creating_human_requests(self):
        self.store._db.execute("UPDATE commands SET origin='scheduled' WHERE id='start'")
        for method in (COMMAND,FILE,PERMISSIONS,MCP):
            outcome=await self.runtime.handle_server_request(method,self.params(method),request_id=3,connection_id='connection:1')
            self.assertTrue(outcome.result in ({'decision':'decline'},{'permissions':{}},{'action':'decline','content':None}))
        self.assertEqual(self.runtime.interactions.list('conv'),[])

    async def test_reconciled_terminal_closes_only_bound_turn_interactions(self):
        command = await self.ask()
        standalone = await self.ask(MCP,rpc=2)
        value=self.runtime._reconcile_native('conv',{'thread':{'id':'thread','status':{'type':'idle'},'turns':[{'id':'turn','status':'completed','items':[]}]}})
        with self.assertRaises(asyncio.CancelledError):await command
        records=self.runtime.interactions.list('conv')
        self.assertEqual(next(r for r in records if r['method']==COMMAND)['state'],'closed')
        self.assertEqual(next(r for r in records if r['method']==MCP)['state'],'awaiting')
        self.assertFalse(standalone.done());self.assertEqual(value['conversation']['state'],'idle')

    async def test_unknown_turn_and_thread_are_not_guessed(self):
        for method,changes in ((COMMAND,{'turnId':'unknown'}),(MCP,{'threadId':'unknown'})):
            with self.assertRaises(AppServerRPCError):
                await self.runtime.handle_server_request(method,self.params(method,**changes),request_id=1,connection_id='connection:1')

    async def test_native_file_change_details_reach_approval(self):
        changes=[{'path':'sample.txt','kind':{'type':'add'},'diff':'+hello'}]
        await self.runtime.handle_notification({'method':'item/started','params':{'threadId':'thread','turnId':'turn',
            'item':{'id':'item','type':'fileChange','status':'inProgress','changes':changes}}},connection_id='connection:1')
        task=await self.ask(FILE)
        self.assertEqual(self.record()['display']['changes'],changes)
        await self.answer(response={'decision':'decline'});await task

    async def test_http_auth_owner_and_redacted_rejection(self):
        task=await self.ask();r=self.record();request=Request({'type':'http','method':'POST','path':'/','headers':[]})
        with mock.patch.object(conversation_api,'_auth',return_value=None),mock.patch.object(conversation_api,'conversation_service',return_value=self.service),mock.patch.object(conversation_api,'logged_in',return_value='other'),mock.patch.object(conversation_api,'audit'):
            result=await conversation_api.answer_native_interaction(request,'conv',r['id'],'cmd',1,r['fingerprint'],'{"decision":"accept"}')
            self.assertEqual(result.status_code,403)
        with mock.patch.object(conversation_api,'_auth',return_value=None),mock.patch.object(conversation_api,'conversation_service',return_value=self.service),mock.patch.object(conversation_api,'logged_in',return_value='owner'),mock.patch.object(conversation_api,'audit') as audit:
            result=await conversation_api.answer_native_interaction(request,'conv',r['id'],'cmd',1,r['fingerprint'],'{"decision":"invalid-private-value"}')
            self.assertEqual(result.status_code,409);self.assertNotIn(b'invalid-private-value',result.body);audit.assert_not_called()
        self.assertFalse(task.done())

class NativePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_restricted_and_full_access_and_scheduled_policy(self):
        for permission,origin,expected in [('read-only','user','on-request'),('workspace-write','user','on-request'),('danger-full-access','user','never'),('workspace-write','scheduled','never')]:
            with tempfile.TemporaryDirectory() as root:
                store=ConversationStore(Path(root)/'db.sqlite3');factory=FakeFactory()
                runtime=ConversationRuntime(store,ConversationArtifactService(store,Path(root)/'artifacts'),factory)
                store.create_conversation('conv',title='x',cwd=root,settings={'model':'fixture','effort':'medium','permission':permission,'collaboration_mode':'plan'})
                runtime.scheduled_guard=lambda *args:True
                await runtime.start_conversation('conv',command_id='start',text='test')
                if origin == 'scheduled':
                    factory.threads['thread-1']['turns'][0]['status']='completed'
                    factory.threads['thread-1']['status']={'type':'idle'}
                    await runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})
                    await runtime.send_message('conv',command_id='msg',text='test',origin=origin,scheduled_context={})
                opts=[c[1]['options'] for c in factory.clients[0].calls if c[0]=='turn/start'][-1]
                self.assertEqual(opts['approvalPolicy'],expected);self.assertEqual(opts['collaborationMode']['mode'],'plan')
                self.assertIsNone(opts['collaborationMode']['settings']['developer_instructions'])
                await runtime.close();store.close()
