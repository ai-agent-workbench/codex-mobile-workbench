"""End-to-end automation through the real store/runtime with an isolated native fake."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from fastapi.responses import Response

from engine.conversation_automation import AutomationStore, next_due
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.codex_app_server import AppServerDisconnected, AppServerRPCError
from shared.conversation_artifacts import ConversationArtifactService
from routes import automations as api
from tests.test_conversation_runtime import FakeFactory
from tests.test_settings_center import request
from tests.store_fixtures import seeded_store


class AutomationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.home(),prefix='automation-integration-')
        self.root=Path(self.temp.name);self.store=seeded_store(self.root/'state.sqlite3')
        self.factory=FakeFactory();self.artifacts=ConversationArtifactService(self.store,self.root/'artifacts')
        self.runtime=ConversationRuntime(self.store,self.artifacts,self.factory)
        self.accounts=mock.patch('shared.security.load_accounts',return_value={'owner':{'enabled':True,'role':'admin'},'other':{'enabled':True,'role':'admin'}});self.accounts.start()
        self.service=ConversationService(self.store,self.artifacts,self.runtime,enable_automations=True)
        self.store.create_conversation('c1',title='内存巡检',cwd=str(self.root),settings={'permission':'read-only','model':'fixture','effort':'medium'},owner_account_id='owner')
        await self.runtime.start_conversation('c1',command_id='initial',text='first')
        self.patches=[mock.patch.object(api,'require_auth',return_value=None),mock.patch.object(api,'logged_in',return_value='owner'),mock.patch.object(api,'conversation_service',return_value=self.service),mock.patch.object(api,'audit')]
        for p in self.patches:p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.accounts.stop();await self.service.close();self.temp.cleanup()

    async def finish(self,turn_id=None,text='Windows 剩余内存：15 GiB',status='completed'):
        thread=self.factory.threads['thread-1'];native=next(t for t in thread['turns'] if t['id']==(turn_id or thread['turns'][-1]['id']))
        native.update(status=status,items=[{'id':'answer-'+native['id'],'type':'agentMessage','phase':'final_answer','text':text}]);thread['status']={'type':'idle'}
        await self.runtime.handle_notification({'method':'item/completed','params':{'threadId':'thread-1','turnId':native['id'],'item':copy.deepcopy(native['items'][0])}})
        await self.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':copy.deepcopy(native)}})
        await asyncio.sleep(.03)

    def task(self,**extra):
        args={'title':'内存巡检','prompt':'只读检查 Windows 剩余物理内存，输出结果','schedule':{'kind':'interval','interval_minutes':60}};args.update(extra)
        return self.service.automations.create('c1','owner',**args)

    def due(self,task):
        now=datetime.now(timezone.utc)
        self.store._db.execute('UPDATE conversation_automation_tasks SET next_run_at=? WHERE id=?',(now.isoformat().replace('+00:00','Z'),task['id']))
        return now

    def tool_params(self,args,call='tool-call'):
        return {'threadId':'thread-1','turnId':self.factory.threads['thread-1']['turns'][-1]['id'],'callId':call,'tool':'manage_scheduled_task','arguments':args}

    async def test_three_hourly_runs_remain_in_same_history_with_outputs(self):
        await self.finish(text='初始说明');task=self.task()
        for i in range(3):
            await self.service.scheduler.tick(self.due(task));await self.finish(text='第'+str(i+1)+'次：Windows 剩余内存 '+str(15-i)+' GiB');self.service.automations.refresh_runs()
        snapshot=self.service.public_snapshot('c1')
        self.assertEqual(len(snapshot['turns']),4)
        self.assertEqual([t['commands'][0]['origin'] for t in snapshot['turns'][1:]],['scheduled']*3)
        self.assertEqual([t['final_text'] for t in snapshot['turns'][1:]],['第1次：Windows 剩余内存 15 GiB','第2次：Windows 剩余内存 14 GiB','第3次：Windows 剩余内存 13 GiB'])
        self.assertEqual([r['status'] for r in self.service.automations.runs(task['id'],'owner')],['completed']*3)
        self.assertIn('系统 · 定时执行',self.service.export_transcript('c1')['markdown'])
        self.assertTrue(all(params['thread_id']=='thread-1' for method,params in self.factory.clients[0].calls if method=='turn/start'))

    async def test_tool_create_replay_and_scope(self):
        args={'action':'create','title':'巡检','prompt':'检查','schedule':{'kind':'interval','interval_minutes':60}}
        params=self.tool_params(args);a=await self.runtime.handle_server_request('item/tool/call',params);b=await self.runtime.handle_server_request('item/tool/call',params)
        self.assertEqual(a,b);self.assertTrue(a['success']);self.assertEqual(len(self.service.automations.list('owner')),1)
        foreign=self.task();self.store._db.execute("UPDATE conversation_automation_tasks SET conversation_id='c1',owner_account_id='other' WHERE id=?",(foreign['id'],))
        result=await self.runtime.handle_server_request('item/tool/call',self.tool_params({'action':'get','task_id':foreign['id']},call='foreign'))
        self.assertFalse(result['success'])
        with self.assertRaises(AppServerRPCError):
            await self.runtime.handle_server_request('item/tool/call',{**params,'turnId':'unknown','callId':'unknown'})

    async def test_all_permissions_tool_create_update_pause_resume(self):
        for permission in ('read-only', 'workspace-write', 'danger-full-access'):
            with self.subTest(permission=permission):
                self.store.update_settings('c1', {'permission': permission})
                async def invoke(args, suffix):
                    result = await self.runtime.handle_server_request(
                        'item/tool/call', self.tool_params(args, call=permission + suffix))
                    self.assertTrue(result['success'], result)
                    return json.loads(result['contentItems'][0]['text'])['task']
                task = await invoke({'action': 'create', 'title': '巡检', 'prompt': '检查',
                    'schedule': {'kind': 'interval', 'interval_minutes': 60}}, '-create')
                self.assertEqual(task['state'], 'active')
                self.assertIsNotNone(task['next_run_at'])
                self.assertEqual(task['permission'], permission)
                self.assertEqual(task['owner_account_id'], 'owner')
                self.assertEqual(self.store._db.execute('SELECT cwd FROM conversation_automation_tasks WHERE id=?', (task['id'],)).fetchone()['cwd'], str(self.root))
                for suffix, changes in (
                    ('-prompt', {'prompt': '只读统计文件夹'}),
                    ('-schedule', {'schedule': {'kind': 'interval', 'interval_minutes': 2}}),
                ):
                    task = await invoke({'action': 'update', 'task_id': task['id'],
                        'revision': task['revision'], **changes}, suffix)
                    self.assertEqual(task['state'], 'active')
                    self.assertIsNotNone(task['next_run_at'])
                task = await invoke({'action': 'pause', 'task_id': task['id'],
                    'revision': task['revision']}, '-pause')
                self.assertEqual(task['state'], 'paused')
                task = await invoke({'action': 'update', 'task_id': task['id'],
                    'revision': task['revision'], 'prompt': '保持暂停'}, '-paused-update')
                self.assertEqual(task['state'], 'paused')
                task = await invoke({'action': 'resume', 'task_id': task['id'],
                    'revision': task['revision']}, '-resume')
                self.assertEqual(task['state'], 'active')
                self.assertIsNotNone(task['next_run_at'])
                self.assertEqual(task['revision'], 6)

    async def test_automation_context_authorizes_conversation_management(self):
        from shared.automation_tools import AUTOMATION_CONTEXT
        self.assertIn('所有权限模式均可通过工具直接启用', AUTOMATION_CONTEXT)
        self.assertNotIn('必须用户在管理页确认启用', AUTOMATION_CONTEXT)

    async def test_scheduled_turn_cannot_recursively_create_tasks_or_auto_answer(self):
        await self.finish();task=self.task();await self.service.scheduler.tick(self.due(task))
        args={'action':'create','title':'递归','prompt':'检查','schedule':{'kind':'interval','interval_minutes':1}}
        result=await self.runtime.handle_server_request('item/tool/call',self.tool_params(args));self.assertFalse(result['success'])
        with self.assertRaises(AppServerRPCError):
            await self.runtime.handle_server_request('item/tool/requestUserInput',{'threadId':'thread-1','turnId':'turn-2'})
        self.assertEqual(len(self.service.automations.list('owner')),1)

    async def test_scheduled_async_choices_have_no_automatic_answer_deadline(self):
        await self.finish();task=self.task();await self.service.scheduler.tick(self.due(task))
        await self.runtime.handle_notification({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-2','item':{'id':'scheduled-question','type':'agentMessage','phase':'commentary','delivery':'async','text':'需要确认','questions':[{'title':'是否修改系统？','options':['修改（推荐）','不修改']}]}}})
        row=self.store._db.execute("SELECT deadline_at FROM async_question_requests WHERE turn_id='turn-2'").fetchone()
        self.assertIsNotNone(row);self.assertIsNone(row['deadline_at']);self.assertEqual(self.runtime._async_question_deadlines,{})

    async def test_once_finishes_and_expired_paused_task_cannot_enable(self):
        await self.finish();future=(datetime.now(timezone.utc)+timedelta(seconds=.2)).isoformat();task=self.task(schedule={'kind':'once','run_at':future})
        await asyncio.sleep(.25)
        await self.service.scheduler.tick(self.due(task));await self.finish();self.service.automations.refresh_runs()
        self.assertEqual(self.service.automations.get(task['id'],'owner')['state'],'finished')
        with self.assertRaises(ValueError):self.service.automations.update(task['id'],'owner',revision=2,state='active')
        pasttask=self.task(schedule={'kind':'once','run_at':(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()},initial_state='paused')
        self.store._db.execute('UPDATE conversation_automation_tasks SET schedule_json=? WHERE id=?',(json.dumps({'kind':'once','run_at':'2020-01-01T00:00:00Z'}),pasttask['id']))
        with self.assertRaises(ValueError):self.service.automations.update(pasttask['id'],'owner',revision=1,state='active')

    async def test_pending_skips_aggregate_and_deleted_replay_is_not_active(self):
        task=self.task(request_key='replay-current');now=self.due(task);await self.service.scheduler.tick(now)
        for i in range(1,4):await self.service.scheduler.tick(now+timedelta(hours=i))
        runs=self.service.automations.runs(task['id'],'owner');self.assertEqual(len(runs),2);self.assertEqual(runs[0]['skipped_count'],3)
        self.service.automations.update(task['id'],'owner',revision=1,state='deleted')
        replay=self.task(request_key='replay-current');self.assertEqual(replay['state'],'deleted');self.assertEqual(replay['prompt'],'')
        cached=self.store._db.execute("SELECT response_json FROM automation_mutations WHERE request_key='replay-current'").fetchone()[0]
        self.assertNotIn('检查',cached)

    async def test_busy_queues_without_steer_and_pause_blocks_queued_run(self):
        task=self.task();await self.service.scheduler.tick(self.due(task))
        self.assertEqual(self.service.automations.runs(task['id'],'owner')[0]['status'],'queued')
        self.assertFalse(any(m=='turn/steer' for m,_ in self.factory.clients[0].calls))
        self.service.automations.update(task['id'],'owner',revision=task['revision'],state='paused')
        await self.finish();self.service.automations.refresh_runs()
        self.assertEqual(len(self.factory.threads['thread-1']['turns']),1)
        self.assertEqual(self.service.automations.runs(task['id'],'owner')[0]['status'],'failed')

    async def test_edit_blocks_old_queued_prompt_and_delete_does_not_remove_chat(self):
        task=self.task();await self.service.scheduler.tick(self.due(task))
        changed=self.service.automations.update(task['id'],'owner',revision=task['revision'],prompt='新内容')
        await self.finish();self.service.automations.refresh_runs()
        self.assertEqual(len(self.factory.threads['thread-1']['turns']),1)
        self.service.automations.update(task['id'],'owner',revision=changed['revision'],state='deleted')
        self.assertEqual(len(self.store.snapshot('c1')['turns']),1)
        with self.assertRaises(ValueError):self.service.automations.update(task['id'],'owner',revision=changed['revision']+1,state='active')

    async def test_unknown_send_is_never_replayed_after_recovery(self):
        await self.finish();task=self.task();client=self.factory.clients[0];client.start_turn_error=AppServerDisconnected('uncertain')
        await self.service.scheduler.tick(self.due(task));count=len([1 for m,_ in client.calls if m=='turn/start'])
        self.service.automations.recover_runs();await self.service.scheduler.tick(self.due(task))
        self.assertEqual(len([1 for m,_ in client.calls if m=='turn/start']),count)
        self.assertEqual(self.service.automations.runs(task['id'],'owner')[0]['status'],'skipped')

    async def test_permission_drift_and_disabled_owner_block_execution(self):
        await self.finish();task=self.task();self.store.update_settings('c1',{'permission':'danger-full-access'})
        await self.service.scheduler.tick(self.due(task));self.assertEqual(self.service.automations.get(task['id'],'owner')['state'],'paused')
        with self.assertRaises(ValueError):self.service.automations.update(task['id'],'owner',revision=2,state='active')
        task=self.task()
        with mock.patch('shared.security.load_accounts',return_value={}):await self.service.scheduler.tick(self.due(task))
        self.assertEqual(self.service.automations.get(task['id'],'owner')['state'],'paused')
        self.assertEqual(len(self.factory.threads['thread-1']['turns']),1)

    async def test_api_owner_origin_confirm_and_cas(self):
        value={'conversation_id':'c1','title':'巡检','prompt':'检查','schedule':{'kind':'interval','interval_minutes':60},'request_key':'browser-1','confirm_unattended':True}
        for origin in ('','null','https://evil.test'):
            self.assertEqual((await api.create_task(request(value,origin=origin))).status_code,400)
        self.assertEqual((await api.create_task(request({**value,'confirm_unattended':False}))).status_code,400)
        a=await api.create_task(request(value));self.assertEqual(a.status_code,200);b=await api.create_task(request(value));self.assertEqual(json.loads(a.body),json.loads(b.body))
        task=json.loads(a.body)['task'];self.assertNotIn(str(self.root),a.body.decode())
        change={'revision':1,'state':'paused','request_key':'pause-1'}
        self.assertEqual((await api.update_task(request(change),task['id'])).status_code,200)
        self.assertEqual((await api.update_task(request({**change,'request_key':'pause-2'}),task['id'])).status_code,409)
        with mock.patch.object(api,'logged_in',return_value='other'):
            self.assertEqual(json.loads(api.tasks(request()).body)['tasks'],[])
            self.assertEqual(api.task_runs(request(),task['id']).status_code,404)
            self.assertEqual((await api.update_task(request(change),task['id'])).status_code,404)
        with mock.patch.object(api,'require_auth',return_value=Response(status_code=403)):
            self.assertEqual(api.tasks(request()).status_code,403)
            self.assertEqual((await api.create_task(request(value))).status_code,403)

    async def test_delete_conversation_cascades_task_runs_and_mutation_cache(self):
        await self.finish();task=self.task(request_key='create-owned');await self.service.scheduler.tick(self.due(task));await self.finish()
        await self.service.delete('c1')
        for table in ('conversation_automation_tasks','conversation_automation_runs','automation_mutations'):
            self.assertEqual(self.store._db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    async def test_once_past_invalid_and_dst_gap_skipped(self):
        with self.assertRaises(ValueError):self.task(schedule={'kind':'once','run_at':'2020-01-01T00:00:00Z'})
        due=next_due({'kind':'daily','time':'02:30','timezone':'America/New_York'},datetime(2026,3,8,6,tzinfo=timezone.utc))
        self.assertEqual(due,datetime(2026,3,9,6,30,tzinfo=timezone.utc))
        for schedule in ({'kind':'interval','interval_minutes':1,'timezone':'bad'},{'kind':'daily','time':'09:+1'}):
            with self.assertRaises(ValueError):self.task(schedule=schedule)
