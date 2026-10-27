import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import AppServerDisconnected, AppServerRPCError
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_stop import ConversationTreeStop
from shared.conversation_artifacts import ConversationArtifactService
from test_conversation_goals import GoalClient, native_goal
from tests.store_fixtures import seeded_store


class TreeClient(GoalClient):
    supports_conversation_tree_stop = True

    def __init__(self, **handlers):
        super().__init__(shared_threads={}, shared_goals={}, **handlers)
        self.children = []
        self.terminals = {}
        self.current = {}
        self.keep_active = False
        self.unknown_interrupt = False
        self.late_child = None
        self.enumerations = 0

    async def list_descendant_threads(self, root):
        self.enumerations += 1
        self.calls.append(('descendants', {'root': root}))
        if self.late_child and self.enumerations == 2:
            self.children.append(self.late_child)
        related = {root}
        while True:
            expanded = related | {entry['id'] for entry in self.children if entry['parentThreadId'] in related}
            if expanded == related: break
            related = expanded
        return copy.deepcopy([entry for entry in self.children if entry['id'] in related and entry['id'] != root])

    async def read_thread_turn_metadata(self, tid):
        self.calls.append(('turns/metadata', {'thread_id': tid}))
        return [{**copy.deepcopy(turn), 'items':[]} for turn in self.threads[tid]['turns']]

    async def interrupt_turn(self, tid, turn):
        self.calls.append(('turn/interrupt', {'thread_id': tid, 'turn_id': turn}))
        if self.unknown_interrupt:
            raise AppServerDisconnected('delivery uncertain')
        if turn != self.current.get(tid):
            raise AppServerRPCError(-32600, 'stale candidate')
        if not self.keep_active:
            self.threads[tid]['status'] = {'type': 'idle'}
            # Deliberately preserve ghost inProgress history: not a terminal.

    async def list_background_terminals(self, tid):
        if self.threads[tid]['status']['type'] == 'notLoaded':
            raise AssertionError('must not load/list unloaded runtime terminals')
        self.calls.append(('terminals/list', {'thread_id': tid}))
        return copy.deepcopy(self.terminals.get(tid, []))

    async def clean_background_terminals(self, tid):
        self.calls.append(('terminals/clean', {'thread_id': tid}))
        self.terminals[tid] = []

    def add(self, tid, *, active=True, parent=None, turns=None):
        turns = turns or [{'id': tid+'-turn', 'status': 'inProgress', 'items': []}]
        self.threads[tid] = {'id': tid, 'cwd': '/different/'+tid,
                            'status': {'type': 'active' if active else 'idle'}, 'turns': turns}
        if active:
            self.current[tid] = turns[-1]['id']
        if parent:
            self.children.append({'id': tid, 'parentThreadId': parent})


class TreeStopControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TreeClient()
        self.client.add('root', turns=[{'id': 'old', 'status': 'inProgress'},
                                       {'id': 'new', 'status': 'inProgress'}])
        self.client.add('child', parent='root')
        self.client.goals['root'] = native_goal('root')
        self.client.goals['child'] = native_goal('child')

    def controller(self, **kw):
        return ConversationTreeStop(self.client, 'root', observe_root_goal=lambda goal: None,
                                    interval=0, **kw)

    async def test_pauses_goals_and_stops_agents_without_terminating_background_tests(self):
        self.client.terminals['child'] = [{'processId': 'background'}]
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        writes = [(m, p) for m, p in self.client.calls if m in {'goal/set','turn/interrupt','terminals/clean'}]
        self.assertEqual(writes[0], ('goal/set', {'threadId': 'root', 'status': 'paused'}))
        self.assertEqual([p['turn_id'] for m,p in writes if m == 'turn/interrupt'], ['new', 'child-turn'])
        self.assertEqual(result['terminals'], 1)
        self.assertEqual(self.client.terminals['child'], [{'processId':'background'}])
        self.assertFalse(any(m == 'terminals/clean' for m,p in writes))
        self.assertTrue(all(t['status'] == 'inProgress' for t in self.client.threads['root']['turns']))
        self.assertFalse(any(m == 'thread/resume' for m,_ in self.client.calls))

    async def test_independent_review_descendants_and_late_agents_are_stopped(self):
        self.client.add('review', active=False)
        self.client.add('review-child', parent='review')
        self.client.terminals['review-child'] = [{'processId':'background-test'}]
        original = self.client.list_descendant_threads
        scans = 0
        async def enumerate_tree(root):
            nonlocal scans
            if root == 'review':
                scans += 1
                if scans == 2:
                    self.client.add('late-review-child', parent='review')
            return await original(root)
        self.client.list_descendant_threads = enumerate_tree
        result = await self.controller(owned_review_threads=('review',)).run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['threads'], 5)
        self.assertEqual(result['terminals'], 1)
        stopped = {p['thread_id'] for m,p in self.client.calls if m == 'turn/interrupt'}
        self.assertTrue({'review-child','late-review-child'} <= stopped)
        self.assertFalse(any(m == 'terminals/clean' for m,_ in self.client.calls))

    async def test_background_inventory_failure_does_not_misreport_agent_stop(self):
        original = self.client.list_background_terminals
        async def inventory(tid):
            if tid == 'child': raise AppServerDisconnected('inventory unavailable')
            return await original(tid)
        self.client.list_background_terminals = inventory
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['background_errors'], 1)
        self.assertEqual(result['errors'], 0)

    async def test_stalled_background_inventory_cannot_block_child_stop(self):
        entered=asyncio.Event()
        async def stalled(tid):
            entered.set()
            self.assertTrue(all(thread['status']['type']=='idle' for thread in self.client.threads.values()))
            await asyncio.Event().wait()
        self.client.list_background_terminals=stalled
        result=await asyncio.wait_for(self.controller().run(mutate=True),2)
        self.assertTrue(entered.is_set())
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['background_errors'],2)
        self.assertEqual(result['active_threads'],0)

    async def test_root_idle_does_not_skip_running_child(self):
        self.client.threads['root']['status'] = {'type':'idle'}
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([p['thread_id'] for m,p in self.client.calls if m == 'turn/interrupt'], ['child'])

    async def test_acknowledgement_without_idle_readback_is_not_success_or_retried(self):
        self.client.keep_active = True
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'unconfirmed')
        interrupts = [p for m,p in self.client.calls if m == 'turn/interrupt']
        self.assertEqual([p['turn_id'] for p in interrupts if p['thread_id']=='root'], ['new'])

    async def test_uncertain_delivery_only_reads_and_never_retries_same_candidate(self):
        self.client.unknown_interrupt = True
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertEqual([p['turn_id'] for m,p in self.client.calls if m=='turn/interrupt' and p['thread_id']=='root'], ['new'])
        self.client.calls.clear()
        await self.controller(passes=2).run(mutate=False)
        self.assertFalse(any(m in {'goal/set','turn/interrupt','terminals/clean'} for m,_ in self.client.calls))

    async def test_late_descendant_is_included_before_success(self):
        self.client.add('late')
        self.client.late_child = {'id':'late', 'parentThreadId':'child'}
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['threads'], 3)
        self.assertTrue(any(m == 'turn/interrupt' and p['thread_id'] == 'late' for m,p in self.client.calls))

    async def test_unloaded_history_is_not_resumed_or_made_terminal(self):
        self.client.threads['child']['status'] = {'type':'notLoaded'}
        result = await self.controller().run(mutate=True)
        self.assertEqual(result['status'], 'verified')
        self.assertFalse(any(m in {'turn/interrupt','terminals/list','thread/resume'} and p.get('thread_id') == 'child' for m,p in self.client.calls))

    async def test_missing_runtime_turn_id_and_unknown_runtime_status_fail_closed(self):
        self.client.threads['root']['turns'] = []
        result = await self.controller(passes=2).run(mutate=True)
        self.assertEqual(result['status'], 'unconfirmed')
        self.client.threads['root']['status'] = {'type':'systemError'}
        result = await self.controller(passes=2).run(mutate=False)
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertGreater(result['errors'], 0)


class TreeStopRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = seeded_store(Path(self.tmp.name)/'state.sqlite3')
        self.client = TreeClient()
        self.client.add('root', turns=[{'id':'old','status':'inProgress','items':[]},
                                       {'id':'new','status':'inProgress','items':[]}])
        self.client.add('child', parent='root')
        self.client.goals['root'] = native_goal('root')
        self.artifacts = ConversationArtifactService(self.store, Path(self.tmp.name)/'artifacts')
        def factory(**handlers):
            for name, value in handlers.items():
                setattr(self.client, name, value)
            return self.client
        self.runtime = ConversationRuntime(self.store, self.artifacts, factory)
        self.service = ConversationService(self.store, self.artifacts, self.runtime)
        self.store.create_conversation('conversation', title='test', cwd=self.tmp.name,
            settings={'model':'fixture','effort':'medium','permission':'danger-full-access','service_tier':None})
        self.store.bind_thread('conversation', {'id':'root'}, server_version='0.160.0')
        self.store.reconcile_thread('conversation', {'thread':copy.deepcopy(self.client.threads['root'])})
        self.patch = mock.patch('engine.conversation_runtime.ConversationTreeStop',
            side_effect=lambda *a, **kw: ConversationTreeStop(*a, **kw, interval=0))
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.runtime.close()
        self.store.close()
        self.tmp.cleanup()

    async def stop(self, cid='stop'):
        return await self.runtime.interrupt('conversation', command_id=cid, scope='conversation_tree')

    async def test_real_incident_multi_active_can_stop_without_current_id_and_keeps_history(self):
        self.assertIsNone(self.store.get_conversation('conversation')['active_turn_id'])
        result = await self.stop()
        self.assertEqual(result['command']['state'], 'completed')
        self.assertIsNone(result['command']['result_turn_id'])
        self.assertEqual(result['snapshot']['conversation']['state'], 'idle')
        self.assertTrue(all(t['status'] == 'inProgress' and t['completed_at'] is None for t in result['snapshot']['turns']))
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['status'], 'verified')
        writes_before = sum(m == 'turn/interrupt' for m,_ in self.client.calls)
        await self.stop()
        self.assertEqual(sum(m == 'turn/interrupt' for m,_ in self.client.calls), writes_before)

    async def test_stop_uses_metadata_only_not_full_history(self):
        with mock.patch.object(self.client,'read_thread',wraps=self.client.read_thread) as reads:
            await self.stop()
        self.assertTrue(reads.await_args_list)
        self.assertTrue(all(call.kwargs.get('include_turns') is False for call in reads.await_args_list))

    async def test_verifying_old_stop_does_not_reject_newer_pending_stop(self):
        self.store.begin_command('conversation','old-stop',kind='interrupt',payload={'scope':'conversation_tree'})
        self.store.update_command('old-stop','accepted')
        self.store.begin_command('conversation','new-stop',kind='interrupt',payload={'scope':'conversation_tree'})
        self.runtime._record_tree_stop('conversation','old-stop',{'status':'verified','root_turn_ids':['old','new']})
        self.assertEqual(self.store.command('new-stop')['state'],'submitted')
        self.assertTrue(self.store.pending_tree_stop('conversation'))

    async def test_queued_text_kept_without_any_automatic_start_after_stop(self):
        self.store.begin_command('conversation','queued',kind='steer',text='必须保留的草稿')
        self.store.update_command('queued','queued')
        await self.stop()
        await self.runtime._drain_queue('conversation')
        queued = self.store.command('queued')
        self.assertEqual(queued['state'], 'rejected')
        self.assertEqual(queued['text'], '必须保留的草稿')
        self.assertEqual(queued['reason'], 'conversation_tree_stopped')
        self.assertFalse(any(m in {'turn/start','turn/steer'} for m,_ in self.client.calls))

    async def test_partial_stop_blocks_send_and_goal_resume_and_readonly_recovery_does_not_replay(self):
        self.client.keep_active = True
        result = await self.stop()
        self.assertEqual(result['command']['state'], 'accepted')
        self.assertEqual(result['snapshot']['conversation']['state'], 'needs_user')
        sent = await self.runtime.send_message('conversation', command_id='send', text='不应投递')
        self.assertEqual(sent['command']['reason'], 'tree_stop_pending')
        goal = await self.runtime.goals.change('conversation', command_id='resume', action='resume')
        self.assertEqual(goal['command']['reason'], 'tree_stop_pending')
        self.client.calls.clear()
        await self.runtime.startup_recover()
        self.assertFalse(any(m in {'goal/set','turn/interrupt','terminals/clean','thread/resume','turn/start','turn/steer'} for m,_ in self.client.calls))
        for thread in self.client.threads.values():
            thread['status'] = {'type':'idle'}
        await self.runtime.reconcile('conversation')
        self.assertEqual(self.store.command('stop')['state'], 'completed')
        self.assertEqual(self.store.get_conversation('conversation')['state'], 'idle')

    async def test_submitted_stop_survives_restart_even_if_root_appears_idle(self):
        self.store.begin_command('conversation','stop',kind='interrupt',payload={'scope':'conversation_tree'})
        self.store.set_conversation_state('conversation','idle',active_turn_id=None)
        self.client.threads['root']['status'] = {'type':'idle'}
        self.client.goals['root']['status'] = 'paused'
        await self.runtime.startup_recover()
        self.assertTrue(self.store.pending_tree_stop('conversation'))
        self.assertFalse(any(m in {'turn/interrupt','goal/set','thread/resume'} for m,_ in self.client.calls))

    async def test_root_terminal_cannot_hide_accepted_tree_stop_from_startup_recovery(self):
        self.store.begin_command('conversation','stop',kind='interrupt',payload={'scope':'conversation_tree'})
        self.store.update_command('stop','accepted',reason='tree_stop_pending')
        self.store.set_conversation_state('conversation','idle',active_turn_id=None)
        self.assertIn('conversation',[c['id'] for c in self.store.recoverable_conversations()])
        self.client.threads['root']['status'] = {'type':'idle'}
        self.client.goals['root']['status'] = 'paused'
        await self.runtime.startup_recover()
        self.assertEqual(self.store.command('stop')['state'],'accepted')
        self.assertTrue(self.store.pending_tree_stop('conversation'))
        self.assertFalse(any(m in {'turn/interrupt','goal/set','thread/resume'} for m,_ in self.client.calls))

    async def test_old_async_recommendation_does_not_restart_stopped_ghost_history(self):
        await self.runtime.handle_notification({'method':'item/completed','params':{
            'threadId':'root','turnId':'new','item':{'id':'async-question','type':'agentMessage',
            'delivery':'async','phase':'final_answer','text':'请选择',
            'questions':[{'title':'选择方案','options':['方案A','方案B']}]}}})
        question = self.store.snapshot('conversation')['async_questions'][0]
        await self.stop()
        self.store._db.execute('UPDATE async_question_requests SET deadline_at=? WHERE id=?',
            ('2000-01-01T00:00:00+00:00',question['id']))
        self.runtime._restore_async_question_deadlines('conversation')
        await self.runtime._answer_async_question_internal('conversation', question['id'],
            command_id='deadline', answer_text='方案A', source='deadline')
        await asyncio.sleep(0)
        self.assertEqual(self.store.async_question_request(question['id'])['state'],'awaiting_answer')
        self.assertNotIn(question['id'], self.runtime._async_question_deadlines)
        self.assertFalse(any(m in {'turn/start','turn/steer'} for m,_ in self.client.calls))

    async def test_unparented_owned_review_is_included_and_restart_verifies_readonly(self):
        self.store.claim_tool_call('conversation','new','review-call','code_review',{})
        self.store.set_review_lifecycle('review-call', {'phase':'turn_started',
            'review_thread_id':'review', 'review_turn_id':'review-turn'})
        self.client.add('review')  # No native ancestor relationship.
        self.client.terminals['review'] = [{'processId':'review-background'}]
        self.client.keep_active = True
        result = await self.stop()
        self.assertEqual(result['command']['state'],'accepted')
        self.assertTrue(any(m=='turn/interrupt' and p['thread_id']=='review' for m,p in self.client.calls))
        for thread in self.client.threads.values():
            thread['status'] = {'type':'idle'}
        self.client.calls.clear()
        await self.runtime.reconcile('conversation')
        self.assertEqual(self.store.command('stop')['state'],'completed')
        self.assertEqual(self.store.pending_review_cleanups(),[])
        self.assertFalse(any(m in {'turn/interrupt','terminals/clean','goal/set','thread/resume'} for m,_ in self.client.calls))

    async def test_send_racing_final_selection_cannot_start_after_stop_intent(self):
        # A user stop is durable before waiting for the command lock.
        self.client.threads['root']['turns'] = []
        self.client.threads['root']['status'] = {'type':'idle'}
        self.client.goals.clear()
        self.store.set_conversation_state('conversation','idle',active_turn_id=None)
        self.store.begin_command('conversation','send',kind='steer',text='race')
        original = self.runtime._resolve_selection
        async def selection(*args):
            result = await original(*args)
            self.store.begin_command('conversation','stop',kind='interrupt',payload={'scope':'conversation_tree'})
            return result
        with mock.patch.object(self.runtime,'_resolve_selection',side_effect=selection):
            result = await self.runtime._start_turn_locked('conversation','send','race',skill_selectors=[])
        self.assertEqual(result['command']['reason'],'tree_stop_pending')
        self.assertFalse(any(m == 'turn/start' for m,_ in self.client.calls))

    async def test_unsupported_and_cross_scope_idempotency_are_not_false_success(self):
        self.client.supports_conversation_tree_stop = False
        result = await self.stop()
        self.assertEqual(result['command']['state'], 'rejected')
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['status'], 'rejected')
        with self.assertRaises(Exception):
            await self.runtime.interrupt('conversation', command_id='stop', expected_turn_id='old')

    async def test_crash_before_queue_cancellation_cannot_restart_old_message(self):
        self.store.begin_command('conversation','queued',kind='steer',text='旧排队文字')
        self.store.update_command('queued','queued')
        self.store.begin_command('conversation','stop',kind='interrupt',payload={'scope':'conversation_tree'})
        for thread in self.client.threads.values():
            thread['status'] = {'type':'idle'}
        self.client.goals['root']['status'] = 'paused'
        await self.runtime.startup_recover()
        await self.runtime._drain_queue('conversation')
        self.assertEqual(self.store.command('stop')['state'],'completed')
        self.assertEqual(self.store.command('queued')['state'],'rejected')
        self.assertEqual(self.store.command('queued')['text'],'旧排队文字')
        self.assertFalse(any(m in {'turn/start','turn/steer','goal/set','thread/resume'} for m,_ in self.client.calls))

    async def test_late_hydrated_question_in_covered_turn_never_auto_answers(self):
        await self.stop()
        await self.runtime.handle_notification({'method':'item/completed','params':{
            'threadId':'root','turnId':'new','item':{'id':'late-question','type':'agentMessage',
            'delivery':'async','phase':'final_answer','text':'历史问题',
            'questions':[{'title':'选择方案','options':['方案A','方案B']}]}}})
        question = self.store.snapshot('conversation')['async_questions'][0]
        self.assertGreater(question['created_at'], self.store.command('stop')['created_at'])
        await self.runtime._answer_async_question_internal('conversation', question['id'],
            command_id='late-deadline',answer_text='方案A',source='deadline')
        self.assertEqual(self.store.async_question_request(question['id'])['state'],'awaiting_answer')
        self.assertFalse(any(m in {'turn/start','turn/steer'} for m,_ in self.client.calls))

    async def test_old_stop_proof_is_not_reused_after_explicit_new_execution(self):
        await self.stop()
        self.store.begin_command('conversation','next-command',kind='steer',text='显式续接')
        turn = {'id':'next-turn','status':'inProgress','items':[]}
        self.store.bind_turn('conversation', turn, command_id='next-command')
        self.client.threads['root']['turns'].append(turn)
        self.client.threads['root']['status'] = {'type':'idle'}
        self.client.threads['child']['status'] = {'type':'active'}
        value = await self.runtime.reconcile('conversation')
        self.assertFalse(self.store.tree_stop_is_current('conversation'))
        self.assertNotEqual(value['conversation']['recovery_state'],'tree_stop_verified')
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['status'],'historical')

    async def test_failed_unsupported_retry_preserves_previous_stop_barrier(self):
        self.client.keep_active = True
        await self.stop()
        self.client.supports_conversation_tree_stop = False
        retry = await self.stop('retry')
        self.assertEqual(retry['command']['state'],'rejected')
        self.assertEqual(self.store.command('stop')['state'],'accepted')
        self.assertTrue(self.store.pending_tree_stop('conversation'))
        resumed = await self.runtime.goals.change('conversation',command_id='resume-unsupported',action='resume')
        self.assertEqual(resumed['command']['reason'],'tree_stop_pending')
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['status'],'unconfirmed')

    async def test_goal_read_during_pending_stop_never_resumes_thread(self):
        self.client.keep_active = True
        await self.stop()
        self.client.calls.clear()
        await self.runtime.goals.refresh('conversation')
        self.assertFalse(any(m == 'thread/resume' for m,_ in self.client.calls))

    async def test_pre_review_start_journals_are_safely_released(self):
        for i,phase in enumerate(('reserved','thread_created')):
            call = 'unstarted-review-'+str(i)
            self.store.claim_tool_call('conversation','new',call,'code_review',{})
            self.store.set_review_lifecycle(call, {'phase':phase})
        result = await self.stop()
        self.assertEqual(result['command']['state'],'completed')
        self.assertEqual(self.store.pending_review_cleanups(),[])

    async def test_cancelled_review_keeps_ownership_and_reports_unterminated_background_test(self):
        self.store.claim_tool_call('conversation','new','running-review','code_review',{})
        self.store.set_review_lifecycle('running-review', {'phase':'turn_started',
            'review_thread_id':'review','review_turn_id':'review-turn'})
        self.client.add('review')
        self.client.terminals['review'] = [{'processId':'review-background'}]
        began = asyncio.Event()
        async def review_runner():
            began.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.client.threads['review']['status'] = {'type':'idle'}
                self.store.clear_review_cleanup('running-review')
                return {}
        task = asyncio.create_task(review_runner())
        self.runtime._review_tasks[('root','new','running-review')] = task
        await began.wait()
        result = await self.stop()
        await task
        self.assertEqual(result['command']['state'],'completed')
        self.assertEqual(self.client.terminals['review'],[{'processId':'review-background'}])
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['terminals'],1)
        self.assertFalse(any(m == 'terminals/clean' for m,p in self.client.calls))
        import json
        self.assertIn('review',json.loads(result['command']['reason'].split(':',1)[1])['owned_review_threads'])

    async def test_slow_review_cancellation_cannot_block_main_tree_stop(self):
        self.store.claim_tool_call('conversation','new','slow-review','code_review',{})
        self.store.set_review_lifecycle('slow-review', {'phase':'turn_started',
            'review_thread_id':'review','review_turn_id':'review-turn'})
        self.client.add('review')
        began, release = asyncio.Event(), asyncio.Event()
        async def review_runner():
            began.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return {}
        task = asyncio.create_task(review_runner())
        self.runtime._review_tasks[('root','new','slow-review')] = task
        await began.wait()
        try:
            result = await asyncio.wait_for(self.stop(),2)
            self.assertEqual(result['command']['state'],'completed')
            self.assertFalse(task.done())
            self.assertEqual(self.client.goals['root']['status'],'paused')
            self.assertTrue({'root','child','review'}.issubset({p['thread_id'] for m,p in self.client.calls if m=='turn/interrupt'}))
        finally:
            release.set()
            await task

    async def test_pre_admission_review_waiter_cannot_restart_stopped_history(self):
        began, release = asyncio.Event(), asyncio.Event()
        async def wait_for_orphans():
            began.set()
            await release.wait()
        params = {'threadId':'root','turnId':'new','callId':'late-review','tool':'code_review',
                  'arguments':{'instructions':'review','scope':{'type':'uncommittedChanges'}}}
        with mock.patch.object(self.runtime,'_reconcile_review_orphans',side_effect=wait_for_orphans), \
             mock.patch('engine.conversation_runtime.run_native_review',new_callable=mock.AsyncMock) as runner:
            request = asyncio.create_task(self.runtime.handle_server_request('item/tool/call',params))
            await began.wait()
            await self.stop()
            release.set()
            response = await request
        self.assertFalse(response['success'])
        runner.assert_not_awaited()
        self.assertFalse(self.runtime._review_source_turn_live('conversation','new'))

    async def test_settings_compact_delete_and_direct_resume_share_stop_barrier(self):
        self.client.keep_active = True
        await self.stop()
        self.store.set_conversation_state('conversation','idle',active_turn_id=None)
        self.client.calls.clear()
        settings = await self.runtime.update_settings('conversation',command_id='settings',updates={'permission':'read-only'})
        compact = await self.runtime.compact('conversation',command_id='compact')
        self.assertEqual(settings['command']['reason'],'tree_stop_pending')
        self.assertEqual(compact['command']['reason'],'tree_stop_pending')
        with self.assertRaises(Exception):
            await self.runtime.delete_native_thread('conversation')
        with self.assertRaises(Exception):
            await self.runtime._resume_owned_thread(self.client,'root',reuse_loaded=True)
        self.assertEqual(self.store.get_conversation('conversation')['settings']['permission'],'danger-full-access')
        self.assertFalse(any(m in {'thread/resume','goal/settings','thread/delete','thread/compact/start'} for m,_ in self.client.calls))

    async def test_cancellation_lost_journal_cache_survives_readonly_restart(self):
        self.store.claim_tool_call('conversation','new','review-cache','code_review',{})
        self.store.set_review_lifecycle('review-cache', {'phase':'turn_started',
            'review_thread_id':'review','review_turn_id':'review-turn'})
        self.client.add('review',active=False)
        self.client.terminals['review'] = [{'processId':'still-running'}]
        self.store.begin_command('conversation','stop',kind='interrupt',payload={'scope':'conversation_tree'})
        self.runtime._capture_stop_review_ownership('conversation')
        self.store.clear_review_cleanup('review-cache')
        for thread in self.client.threads.values(): thread['status']={'type':'idle'}
        self.client.goals['root']['status']='paused'
        self.client.calls.clear()
        await self.runtime.startup_recover()
        self.assertFalse(self.store.pending_tree_stop('conversation'))
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['terminals'],1)
        self.assertTrue(any(m=='terminals/list' and p['thread_id']=='review' for m,p in self.client.calls))
        self.assertFalse(any(m in {'goal/set','turn/interrupt','terminals/clean','thread/resume'} for m,_ in self.client.calls))
        self.client.terminals['review']=[]
        await self.runtime.reconcile('conversation')
        self.assertEqual(self.store.command('stop')['state'],'completed')

    async def test_completed_review_retains_ownership_and_stops_its_active_child(self):
        self.store.claim_tool_call('conversation','new','done-review','code_review',{})
        self.store.set_review_lifecycle('done-review', {'phase':'turn_started',
            'review_thread_id':'review','review_turn_id':'review-turn'})
        self.store.set_review_lifecycle('done-review', None)
        response = {'contentItems':[{'type':'inputText','text':'unaltered result'}], 'success':True}
        self.assertEqual(self.store.complete_tool_call('done-review',response),response)
        self.assertEqual(self.store.claim_tool_call('conversation','new','done-review','code_review',{}),response)
        self.assertEqual(self.store.pending_review_cleanups(),[])
        self.client.add('review',active=False)
        self.client.add('review-child',parent='review')
        self.client.terminals['review']=[{'processId':'test-after-review'}]
        result=await self.stop()
        self.assertEqual(result['command']['state'],'completed')
        self.assertTrue(any(m=='turn/interrupt' and p['thread_id']=='review-child' for m,p in self.client.calls))
        self.assertEqual(self.service.public_snapshot('conversation')['stop']['terminals'],1)
        self.assertFalse(any(m=='terminals/clean' for m,_ in self.client.calls))

    async def test_legacy_completed_native_result_proves_review_ownership(self):
        import json
        self.store.claim_tool_call('conversation','new','legacy-review','code_review',{})
        encoded=json.dumps({'official_channel':'app_server_native_review','review_thread_id':'review',
                            'review_turn_id':'review-turn','result_text':'irrelevant reviewer prose'})
        self.store.complete_tool_call('legacy-review',{'success':True,'contentItems':[
            {'type':'inputText','text':'Official native code review completed; complete JSON follows in ordered chunks.\n'+encoded}]})
        self.client.add('review',active=False)
        self.client.add('legacy-review-child',parent='review')
        result=await self.stop()
        self.assertEqual(result['command']['state'],'completed')
        self.assertTrue(any(m=='turn/interrupt' and p['thread_id']=='legacy-review-child' for m,p in self.client.calls))

    async def test_never_started_empty_reviewer_cannot_block_agent_stop(self):
        self.store.claim_tool_call('conversation','new','empty-review','code_review',{})
        self.store.set_review_lifecycle('empty-review',{'phase':'thread_created','review_thread_id':'unmaterialized'})
        self.store.set_review_lifecycle('empty-review',None)
        self.assertEqual(self.store.owned_review_threads('conversation'),{})
        result=await self.stop()
        self.assertEqual(result['command']['state'],'completed')
        self.assertFalse(any(p.get('thread_id')=='unmaterialized' or p.get('root')=='unmaterialized'
                             for _,p in self.client.calls))

    async def test_requested_reviewer_ownership_remains_after_lifecycle_clear(self):
        self.store.claim_tool_call('conversation','new','requested-review','code_review',{})
        self.store.set_review_lifecycle('requested-review',{'phase':'thread_created','review_thread_id':'review'})
        self.assertEqual(self.store.owned_review_threads('conversation'),{})
        self.store.set_review_lifecycle('requested-review',{'phase':'start_requested','review_thread_id':'review'})
        self.store.set_review_lifecycle('requested-review',None)
        self.assertEqual(self.store.owned_review_threads('conversation')['requested-review']['review_thread_id'],'review')

    async def test_stalled_background_inventory_has_a_separate_runtime_budget(self):
        async def stalled(tid):
            self.assertTrue(all(thread['status']['type']=='idle' for thread in self.client.threads.values()))
            await asyncio.Event().wait()
        self.client.list_background_terminals=stalled
        result=await asyncio.wait_for(self.stop(),2)
        self.assertEqual(result['command']['state'],'completed')
        public=self.service.public_snapshot('conversation')['stop']
        self.assertEqual(public['status'],'verified')
        self.assertEqual(public['background_errors'],2)

    async def test_review_cleanup_inflight_transfers_without_duplicate_interrupt(self):
        self.store.claim_tool_call('conversation','new','transferred-review','code_review',{})
        self.store.set_review_lifecycle('transferred-review', {'phase':'turn_started',
            'review_thread_id':'review','review_turn_id':'review-turn'})
        self.store.mark_review_interrupt_attempt('transferred-review','review')
        self.client.add('review')
        began=asyncio.Event()
        async def runner():
            began.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: return {}
        task=asyncio.create_task(runner())
        self.runtime._review_tasks[('root','new','transferred-review')]=task
        await began.wait()
        result=await self.stop()
        await task
        self.assertEqual(result['command']['state'],'accepted')
        self.assertFalse(any(m=='turn/interrupt' and p['thread_id']=='review' for m,p in self.client.calls))
        self.client.threads['review']['status']={'type':'idle'}
        await self.runtime.reconcile('conversation')
        self.assertEqual(self.store.command('stop')['state'],'completed')

    async def test_active_runtime_without_visible_turn_is_not_idle(self):
        self.client.threads['root']['turns'] = [{'id':'old','status':'completed','items':[]}]
        value = self.store.reconcile_thread('conversation', {'thread':copy.deepcopy(self.client.threads['root'])})
        self.assertEqual(value['conversation']['state'],'needs_user')
        self.assertEqual(value['conversation']['recovery_state'],'active_native_turn_unidentified')
        self.assertIsNone(value['conversation']['active_turn_id'])

    async def test_idle_runtime_with_ghost_history_is_not_shown_executing_or_released(self):
        self.client.threads['root']['status'] = {'type':'idle'}
        value = self.store.reconcile_thread('conversation', {'thread':copy.deepcopy(self.client.threads['root'])})
        self.assertEqual(value['conversation']['recovery_state'],'stale_native_turn_history')
        self.assertIsNone(value['conversation']['active_turn_id'])
        self.assertEqual(value['conversation']['state'],'needs_user')
