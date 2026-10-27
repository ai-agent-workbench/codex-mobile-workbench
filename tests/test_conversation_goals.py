import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import AppServerDisconnected, AppServerProtocolError, AppServerRPCError, CodexAppServerClient
from engine.conversation_goals import validate_goal
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from test_conversation_runtime import FakeClient
from tests.store_fixtures import seeded_store


def native_goal(tid='thread-1', status='active', objective='完成测试目标', **extra):
    return {'threadId': tid, 'objective': objective, 'status': status, 'createdAt': 100,
            'updatedAt': 101, 'tokensUsed': 17, 'timeUsedSeconds': 3, 'tokenBudget': None, **extra}


class GoalClient(FakeClient):
    def __init__(self, *, shared_goals, **kw):
        super().__init__(**kw)
        self.goals = shared_goals
        self.goal_error = None
        self.response_error = None
        self.emit_auto = False

    async def get_thread_goal(self, thread_id):
        self.calls.append(('goal/get', {'threadId': thread_id}))
        if self.goal_error:
            raise self.goal_error
        return copy.deepcopy(self.goals.get(thread_id))

    async def set_thread_goal(self, thread_id, **updates):
        self.calls.append(('goal/set', {'threadId': thread_id, **updates}))
        if self.goal_error:
            raise self.goal_error
        goal = self.goals.setdefault(thread_id, native_goal(thread_id, 'paused'))
        goal.update(updates)
        if self.notification_handler:
            await self.notification_handler({'method': 'thread/goal/updated', 'params': {'threadId': thread_id, 'goal': copy.deepcopy(goal)}})
        if self.emit_auto and updates.get('status') == 'active' and self.threads[thread_id]['status']['type'] == 'idle':
            turn = {'id': 'auto-turn', 'status': 'inProgress', 'items': []}
            self.threads[thread_id]['turns'].append(copy.deepcopy(turn))
            self.threads[thread_id]['status'] = {'type': 'active'}
            await self.notification_handler({'method': 'turn/started', 'params': {'threadId': thread_id, 'turn': copy.deepcopy(turn)}})
        if self.response_error:
            raise self.response_error
        return copy.deepcopy(goal)

    async def clear_thread_goal(self, thread_id):
        self.calls.append(('goal/clear', {'threadId': thread_id}))
        self.goals.pop(thread_id, None)
        await self.notification_handler({'method': 'thread/goal/cleared', 'params': {'threadId': thread_id}})
        return True

    async def update_goal_thread_settings(self, thread_id, **settings):
        self.calls.append(('goal/settings', {'threadId': thread_id, **settings}))


class GoalFactory:
    def __init__(self):
        self.threads = {}
        self.goals = {}
        self.clients = []

    def __call__(self, **handlers):
        client = GoalClient(shared_threads=self.threads, shared_goals=self.goals, **handlers)
        self.clients.append(client)
        return client


class ConversationGoalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = seeded_store(self.root / 'state/conversations.sqlite3')
        self.factory = GoalFactory()
        self.terminals = []
        self.artifacts = ConversationArtifactService(self.store, self.root / 'artifacts')
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory,
            terminal_handler=lambda cid, tid, snap: self.terminals.append(tid))
        self.service = ConversationService(self.store, self.artifacts, self.runtime)
        self.store.create_conversation('conversation-1', title='test', cwd=str(self.root),
            settings={'model': 'fixture', 'effort': 'medium', 'permission': 'danger-full-access',
                      'service_tier': None, 'default_skills': ['preserved-skill']})

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.close()
        self.tmp.cleanup()

    async def start(self):
        return await self.runtime.start_conversation('conversation-1', command_id='first-command', text='首轮用户原文', skill_selectors=['preserved-skill'])

    @property
    def client(self):
        return self.factory.clients[-1]

    async def complete_first(self):
        turn = copy.deepcopy(self.factory.threads['thread-1']['turns'][0])
        turn['status'] = 'completed'
        self.factory.threads['thread-1']['turns'][0] = turn
        self.factory.threads['thread-1']['status'] = {'type': 'idle'}
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': turn}})

    async def change(self, action='set', command='goal-command', objective='完成测试目标', status='active', version=None):
        if version is None:
            public = self.runtime.goals.store.public('conversation-1')
            goal = public['goal'] or public['goal_draft']
            version = str((goal or {}).get('version', (goal or {}).get('revision', '')))
        return await self.runtime.goals.change('conversation-1', command_id=command, action=action,
            objective=objective if action == 'set' else None, status=status if action == 'set' else None, goal_version=version)

    async def test_draft_goal_does_not_create_native_thread_or_change_defaults(self):
        self.runtime.goals.save_draft('conversation-1', '目标内容', 'paused')
        value = self.service.public_snapshot('conversation-1')
        self.assertIsNone(value['goal'])
        self.assertEqual('draft', value['goal_sync_state'])
        self.assertEqual('目标内容', value['goal_draft']['objective'])
        self.assertFalse(self.factory.clients)
        self.assertEqual(['preserved-skill'], value['conversation']['settings']['default_skills'])
        self.assertEqual(6, self.store.snapshot('conversation-1')['schema_version'])

    async def test_new_active_goal_preserves_one_user_first_turn_and_skills(self):
        self.runtime.goals.save_draft('conversation-1', '完成测试目标')
        result = await self.start()
        self.assertEqual('accepted', result['command']['state'])
        calls = self.client.calls
        starts = [value for method, value in calls if method == 'turn/start']
        self.assertEqual(1, len(starts))
        self.assertEqual('首轮用户原文', starts[0]['text'])
        self.assertEqual('preserved-skill', starts[0]['skill_inputs'][0]['name'])
        self.assertIn('完成测试目标', starts[0]['leading_context'])
        goal_calls = [value for method, value in calls if method == 'goal/set']
        self.assertEqual(['active'], [v['status'] for v in goal_calls])
        self.assertEqual('完成测试目标', goal_calls[-1]['objective'])
        self.assertNotIn('tokenBudget', goal_calls[-1], 'no implicit production budget')
        self.assertIsNone(self.runtime.goals.store.entry('conversation-1')['draft'])

    async def test_new_paused_goal_does_not_activate_or_start_extra_turn(self):
        self.runtime.goals.save_draft('conversation-1', '完成测试目标', 'paused')
        await self.start()
        self.assertEqual(['paused'], [v['status'] for m, v in self.client.calls if m == 'goal/set'])
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))

    async def test_goal_set_idle_binds_native_automatic_turn_without_start_rpc(self):
        await self.start()
        await self.complete_first()
        self.client.emit_auto = True
        result = await self.change()
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('auto-turn', self.store.get_conversation('conversation-1')['active_turn_id'])
        self.assertEqual(1, len([m for m, _ in self.client.calls if m == 'turn/start']))
        self.assertTrue(any(c['kind'] == 'goal_continue' for c in self.store.snapshot('conversation-1')['commands']))
        self.assertFalse(any(m['kind'] == 'goal_continue' for m in self.service.public_snapshot('conversation-1')['messages']))

    async def test_unknown_thread_goal_and_turn_are_not_claimed(self):
        await self.runtime.handle_notification({'method': 'thread/goal/updated', 'params': {'threadId': 'other', 'goal': native_goal('other')}})
        await self.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'other', 'turn': {'id': 'other-turn', 'status': 'inProgress'}}})
        self.assertEqual([], self.store.snapshot('conversation-1')['turns'])

    async def test_late_user_turn_started_does_not_add_phantom_goal_command(self):
        await self.start()
        await self.change()
        turn = self.factory.threads['thread-1']['turns'][0]
        await self.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': turn}})
        self.assertFalse(any(c['kind'] == 'goal_continue' for c in self.store.snapshot('conversation-1')['commands']))

    async def test_goal_pause_retains_usage_and_does_not_interrupt_current_turn(self):
        await self.start()
        await self.change()
        result = await self.change('pause', 'pause-command')
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('paused', self.service.public_snapshot('conversation-1')['goal']['status'])
        self.assertEqual(17, self.service.public_snapshot('conversation-1')['goal']['tokensUsed'])
        self.assertFalse(any(m == 'turn/interrupt' for m, _ in self.client.calls))
        self.assertEqual('active', self.store.get_conversation('conversation-1')['state'])

    async def test_stop_pauses_goal_before_native_interrupt(self):
        await self.start()
        await self.change()
        await self.runtime.interrupt('conversation-1', command_id='stop-command', expected_turn_id='turn-1')
        methods = [m for m, _ in self.client.calls]
        self.assertLess(max(i for i, m in enumerate(methods) if m == 'goal/set'), methods.index('turn/interrupt'))
        self.assertEqual('paused', self.runtime.goals.store.public('conversation-1')['goal']['status'])

    async def test_stale_version_and_reused_command_id_cannot_mutate_goal(self):
        await self.start()
        await self.change()
        count = len([m for m, _ in self.client.calls if m == 'goal/set'])
        result = await self.change('pause', 'stale-command', version='wrong')
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual(count, len([m for m, _ in self.client.calls if m == 'goal/set']))
        with self.assertRaises(AppServerProtocolError):
            await self.change(command='goal-command', objective='different')

    async def test_uncertain_goal_activation_is_not_replayed_on_duplicate(self):
        await self.start()
        self.client.response_error = AppServerDisconnected('lost')
        result = await self.change()
        self.assertEqual('delivery_unknown', result['command']['state'])
        count = len([m for m, _ in self.client.calls if m == 'goal/set'])
        result = await self.change(version='', command='goal-command')
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.assertEqual(count, len([m for m, _ in self.client.calls if m == 'goal/set']))
        self.client.response_error = None
        await self.runtime.goals.refresh('conversation-1')
        self.assertEqual('synced', self.service.public_snapshot('conversation-1')['goal_sync_state'])

    async def test_goal_read_failure_keeps_objective_instead_of_reporting_no_goal(self):
        await self.start()
        await self.change()
        self.client.goal_error = AppServerDisconnected('lost')
        with self.assertRaises(AppServerDisconnected):
            await self.runtime.goals.refresh('conversation-1')
        public = self.service.public_snapshot('conversation-1')
        self.assertEqual('unknown', public['goal_sync_state'])
        self.assertEqual('完成测试目标', public['goal']['objective'])

    async def test_native_usage_limited_and_complete_are_not_inferred_from_turn_end(self):
        await self.start()
        await self.change()
        await self.complete_first()
        self.assertEqual('active', self.service.public_snapshot('conversation-1')['goal']['status'])
        goal = native_goal(status='budgetLimited', tokenBudget=10, tokensUsed=17)
        self.factory.goals['thread-1'] = goal
        await self.runtime.handle_notification({'method': 'thread/goal/updated', 'params': {'threadId': 'thread-1', 'goal': goal}})
        self.assertEqual('budgetLimited', self.service.public_snapshot('conversation-1')['goal']['status'])

    async def test_active_goal_updates_all_native_next_turn_settings(self):
        await self.start()
        await self.change()
        await self.runtime.update_settings('conversation-1', command_id='new-settings',
             updates={'effort': 'low', 'permission': 'read-only', 'collaboration_mode': 'plan'})
        args = [v for m, v in self.client.calls if m == 'goal/settings'][-1]
        self.assertEqual(('low', 'read-only', 'plan'), (args['effort'], args['permission'], args['collaboration_mode']))

    async def test_idle_native_goal_is_included_in_restart_recovery(self):
        await self.start()
        await self.change()
        await self.complete_first()
        self.store.set_conversation_state('conversation-1', 'idle', active_turn_id=None)
        self.client.calls.clear()
        await self.runtime.startup_recover()
        self.assertIn('goal/get', [m for m, _ in self.client.calls])
        self.assertFalse(any(m == 'goal/set' for m, _ in self.client.calls))

    async def test_bound_draft_restart_is_quarantined_not_autoactivated(self):
        await self.start()
        self.runtime.goals.save_draft('conversation-1', '保留但不重放')
        await self.runtime.startup_recover()
        self.assertEqual('paused', self.runtime.goals.store.entry('conversation-1')['draft']['status'])
        self.assertFalse(any(m == 'goal/set' for m, _ in self.client.calls))

    async def test_goal_projection_survives_store_reopen_without_core_settings_changes(self):
        self.runtime.goals.save_draft('conversation-1', '持久目标')
        before = self.store.get_conversation('conversation-1')['settings']
        reopened = ConversationStore(self.store.path)
        try:
            self.assertEqual(before, reopened.get_conversation('conversation-1')['settings'])
            self.assertEqual(6, reopened.snapshot('conversation-1')['schema_version'])
        finally:
            reopened.close()

    async def test_delete_refuses_goal_continuation_even_in_idle_gap(self):
        await self.start()
        await self.change()
        await self.complete_first()
        self.store.set_conversation_state('conversation-1', 'idle', active_turn_id=None)
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.delete_native_thread('conversation-1')
        self.assertIn('thread-1', self.factory.threads)

    async def test_project_delete_guard_includes_native_goal_idle_and_unknown_gaps(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        self.assertFalse(self.service.has_active_projects({'goal-project'}))
        await self.start(); await self.change(); await self.complete_first()
        self.store.set_conversation_state('conversation-1','idle',active_turn_id=None)
        self.assertTrue(self.service.has_active_projects({'goal-project'}))
        self.assertFalse(self.service.has_active_projects({'different-project'}))
        self.runtime.goals.disconnected()
        self.assertTrue(self.service.has_active_projects({'goal-project'}), 'uncertain active Goal must retain project deletion protection')
        from routes import projects as project_routes
        import projects
        with mock.patch.object(project_routes,'require_auth',return_value=None), mock.patch.object(project_routes,'conversation_service',return_value=self.service), mock.patch.object(projects,'recent',return_value=[{'id':'goal-project'}]), mock.patch.object(projects,'delete') as delete:
            response=project_routes.projects_manage(mock.Mock(),ids=['goal-project'],action='delete')
            self.assertIn('无法整理项目',response.body.decode())
            delete.assert_not_called()
        await self.change('pause',command='pause-before-project-delete')
        self.assertFalse(self.service.has_active_projects({'goal-project'}))

    async def test_goal_activation_preflight_reserves_project_before_any_rpc(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        await self.start();await self.complete_first()
        entered=asyncio.Event();release=asyncio.Event()
        original=self.client.update_goal_thread_settings
        async def delayed(thread_id,**settings):
            entered.set();await release.wait();await original(thread_id,**settings)
        self.client.update_goal_thread_settings=delayed
        task=asyncio.create_task(self.change())
        try:
            await entered.wait()
            self.assertFalse(self.runtime.goals.continuing('conversation-1'))
            self.assertEqual('idle',self.store.get_conversation('conversation-1')['state'])
            delete=mock.Mock()
            with self.assertRaises(ValueError):self.service.delete_projects(['goal-project'],delete)
            delete.assert_not_called()
        finally:
            release.set()
        self.assertEqual('accepted',(await task)['command']['state'])

    async def test_project_deletion_reservation_rejects_new_goal_and_turn_admission(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        def deleting(_):
            with self.assertRaises(AppServerProtocolError):
                self.runtime.goals.store.begin_command('conversation-1','during-project-delete',payload={'action':'set','status':'active'})
            with self.assertRaises(AppServerProtocolError):
                self.store.begin_command('conversation-1','start-during-delete',kind='start',text='must not start')
            self.assertTrue(self.service.has_active_projects({'goal-project'}))
        self.service.delete_projects(['goal-project'],deleting)
        self.assertFalse(self.service.has_active_projects({'goal-project'}))

    async def test_cancelled_unsent_goal_releases_project_activation_admission(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        await self.start();await self.complete_first()
        lock=self.runtime._conversation_locks['conversation-1']
        await lock.acquire()
        try:
            task=asyncio.create_task(self.change())
            await asyncio.sleep(0)
            self.assertTrue(self.service.has_active_projects({'goal-project'}))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual('rejected',self.runtime.goals.store.command('goal-command')['state'])
        finally:lock.release()
        self.assertFalse(self.service.has_active_projects({'goal-project'}))

    async def test_rejected_optimistic_goal_restores_ordinary_terminal_notice(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start()
        async def rejected(thread_id,**updates):
            await self.complete_first()
            self.assertTrue(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
            raise AppServerRPCError(-32600,'definite activation rejection')
        self.client.set_thread_goal=rejected
        result=await self.change()
        self.assertEqual('rejected',result['command']['state'])
        self.assertFalse(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
        self.assertTrue(self.store.claim_completion_notification('conversation-1','turn-1'))
        await asyncio.sleep(.02)
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))

    async def test_rejected_goal_update_does_not_unsuppress_an_existing_native_cycle(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start();await self.change()
        async def rejected(thread_id,**updates):
            await self.complete_first()
            raise AppServerRPCError(-32600,'definite update rejection')
        self.client.set_thread_goal=rejected
        result=await self.change(command='update-existing-goal')
        self.assertEqual('rejected',result['command']['state'])
        self.assertTrue(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
        self.assertFalse(self.store.claim_completion_notification('conversation-1','turn-1'))

    async def test_cancelled_first_goal_settings_preflight_releases_activation_receipt(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        self.runtime.goals.save_draft('conversation-1','saved first goal')
        entered=asyncio.Event();calls=0
        original=GoalClient.update_goal_thread_settings
        async def delayed(client,thread_id,**settings):
            nonlocal calls
            calls+=1;await original(client,thread_id,**settings)
            if calls==2:
                entered.set();await asyncio.Event().wait()
        with mock.patch.object(GoalClient,'update_goal_thread_settings',new=delayed):
            task=asyncio.create_task(self.start());await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual('accepted',self.store.command('first-command')['state'])
        receipts=list(self.store._db.execute("SELECT state FROM native_goal_commands WHERE origin='system'"))
        self.assertEqual(['rejected'],[row[0] for row in receipts])
        self.assertEqual('paused',self.runtime.goals.store.entry('conversation-1')['draft']['status'])
        await self.complete_first()
        await self.change('clear',command='clear-cancelled-first-goal')
        self.assertFalse(self.service.has_active_projects({'goal-project'}))

    async def test_rejected_nonactivation_during_real_cycle_settlement_keeps_middle_notices_suppressed(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start();await self.change();await self.complete_first()
        final={'id':'pending-cycle-final','status':'inProgress','items':[]}
        self.factory.threads['thread-1']['turns'].append(copy.deepcopy(final))
        self.factory.threads['thread-1']['status']={'type':'active'}
        await self.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':copy.deepcopy(final)}})
        final['status']='completed';self.factory.threads['thread-1']['turns'][-1]=copy.deepcopy(final)
        self.factory.threads['thread-1']['status']={'type':'idle'}
        await self.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':final}})
        self.factory.goals['thread-1']=native_goal(status='complete')
        async def rejected(*args,**kw):raise AppServerRPCError(-32600,'definite mutation rejection')
        with mock.patch.object(self.runtime.goals,'schedule_settlement'),mock.patch.object(self.client,'clear_thread_goal',new=rejected),mock.patch.object(self.client,'set_thread_goal',new=rejected):
            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':'thread-1','goal':self.factory.goals['thread-1']}})
            self.assertTrue(self.runtime.goals.store.entry('conversation-1')['settlement_pending'])
            for action,status in [('clear','active'),('set','paused')]:
                result=await self.change(action,command='rejected-'+action,status=status)
                self.assertEqual('rejected',result['command']['state'])
                self.assertTrue(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
                self.assertFalse(self.store.claim_completion_notification('conversation-1','turn-1'))
            await self.runtime.goals._settle_goal('conversation-1')
        self.assertFalse(self.store.claim_completion_notification('conversation-1','turn-1'))
        self.assertTrue(self.store.claim_completion_notification('conversation-1','pending-cycle-final'))

    async def test_background_queued_steer_definite_rejection_survives_cancel_and_restart(self):
        await self.start()
        self.store.begin_command('conversation-1','background-queued',kind='steer',text='queued correction')
        self.store.update_command('background-queued','queued',reason='wait for native turn')
        self.factory.threads['thread-1']['turns'][0]['status']='completed'
        self.factory.threads['thread-1']['status']={'type':'idle'}
        self.client.steer_error=AppServerRPCError(-32600,'no active turn')
        entered=asyncio.Event()
        async def blocked_read(*args,**kw):
            entered.set();await asyncio.Event().wait()
        with mock.patch.object(self.client,'read_thread',new=blocked_read):
            task=asyncio.create_task(self.runtime.goals.deliver_queue('conversation-1','turn-1'))
            await entered.wait()
            self.assertEqual('queued',self.store.command('background-queued')['state'])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual('queued',self.store.command('background-queued')['state'])
        await self.runtime.close()
        self.runtime=ConversationRuntime(self.store,self.artifacts,self.factory)
        client=await self.runtime._ensure_client();client.turn_no=1
        await self.runtime.startup_recover()
        for _ in range(100):
            if self.store.command('background-queued')['state']=='accepted':break
            await asyncio.sleep(.01)
        self.assertEqual('accepted',self.store.command('background-queued')['state'])
        sent=[call for client in self.factory.clients for method,call in client.calls if method=='turn/start' and call.get('text')=='queued correction']
        self.assertEqual(1,len(sent))

    async def test_cancelled_waiting_ui_mutation_does_not_pause_saved_first_goal(self):
        self.runtime.goals.save_draft('conversation-1','previously approved first goal')
        before=self.runtime.goals.store.entry('conversation-1')
        lock=self.runtime._conversation_locks['conversation-1'];await lock.acquire()
        try:
            task=asyncio.create_task(self.change('clear'));await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:lock.release()
        self.assertEqual('rejected',self.runtime.goals.store.command('goal-command')['state'])
        after=self.runtime.goals.store.entry('conversation-1')
        self.assertEqual(before['draft'],after['draft'])
        self.assertEqual(before['version'],after['version'])

    async def test_cancelled_first_activation_after_terminal_restores_draft_notice(self):
        self.store.set_completion_notification('conversation-1',True)
        self.runtime.goals.save_draft('conversation-1','first draft')
        entered=asyncio.Event();calls=0
        original=GoalClient.update_goal_thread_settings
        async def delayed(client,thread_id,**settings):
            nonlocal calls
            calls+=1;await original(client,thread_id,**settings)
            if calls==2:
                await self.complete_first();entered.set();await asyncio.Event().wait()
        with mock.patch.object(GoalClient,'update_goal_thread_settings',new=delayed):
            task=asyncio.create_task(self.start());await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
        self.assertTrue(self.store.claim_completion_notification('conversation-1','turn-1'))

    async def test_new_native_goal_during_rejected_rpc_keeps_its_terminal_suppression(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start()
        async def rejected(thread_id,**updates):
            goal=native_goal(thread_id)
            self.factory.goals[thread_id]=goal
            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':thread_id,'goal':goal}})
            await self.complete_first()
            raise AppServerRPCError(-32600,'late rejection after independent native update')
        self.client.set_thread_goal=rejected
        result=await self.change()
        self.assertEqual('rejected',result['command']['state'])
        self.assertTrue(self.runtime.goals.continuing('conversation-1'))
        self.assertTrue(self.runtime.goals.store.notice_suppressed('conversation-1','turn-1'))
        self.assertFalse(self.store.claim_completion_notification('conversation-1','turn-1'))

    async def test_cancelled_sent_goal_remains_unknown_and_blocks_project_deletion(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        await self.start();await self.complete_first()
        entered=asyncio.Event()
        async def sending(thread_id,**updates):
            entered.set();await asyncio.Event().wait()
        self.client.set_thread_goal=sending
        task=asyncio.create_task(self.change());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual('delivery_unknown',self.runtime.goals.store.command('goal-command')['state'])
        self.assertTrue(self.runtime.goals.continuing('conversation-1'))
        self.assertTrue(self.service.has_active_projects({'goal-project'}))

    async def test_cancelled_core_admissions_waiting_for_lock_are_rejected(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        lock=self.runtime._conversation_locks['conversation-1']
        calls=[lambda:self.runtime.start_conversation('conversation-1',command_id='cancel-start',text='start'),
               lambda:self.runtime.send_message('conversation-1',command_id='cancel-steer',text='steer'),
               lambda:self.runtime.compact('conversation-1',command_id='cancel-compact')]
        for call in calls:
            await lock.acquire()
            try:
                task=asyncio.create_task(call());await asyncio.sleep(0)
                self.assertTrue(self.service.has_active_projects({'goal-project'}))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            finally:lock.release()
            self.assertFalse(self.service.has_active_projects({'goal-project'}))
        self.assertTrue(all(self.store.command(cid)['state']=='rejected' for cid in ('cancel-start','cancel-steer','cancel-compact')))
        self.assertFalse(self.factory.clients)

    async def test_cancelled_sent_start_remains_unknown_and_blocks_project_deletion(self):
        self.store._db.execute("UPDATE conversations SET project_id='goal-project' WHERE id='conversation-1'")
        entered=asyncio.Event()
        async def sending(client,**settings):
            entered.set();await asyncio.Event().wait()
        with mock.patch.object(GoalClient,'start_thread',new=sending):
            task=asyncio.create_task(self.start());await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual('delivery_unknown',self.store.command('first-command')['state'])
        self.assertEqual('needs_user',self.store.get_conversation('conversation-1')['state'])
        self.assertTrue(self.service.has_active_projects({'goal-project'}))

    async def test_native_goal_activation_during_delete_read_prevents_thread_delete(self):
        await self.start();await self.complete_first()
        original=self.client.read_thread
        async def read_then_goal(thread_id,**kw):
            value=await original(thread_id,**kw)
            goal=native_goal(thread_id)
            self.factory.goals[thread_id]=goal
            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':thread_id,'goal':goal}})
            return value
        self.client.read_thread=read_then_goal
        with self.assertRaises(AppServerProtocolError):await self.runtime.delete_native_thread('conversation-1')
        self.assertTrue(self.runtime.goals.continuing('conversation-1'))
        self.assertIn('thread-1',self.factory.threads)
        self.assertFalse(any(m=='thread/delete' for m,_ in self.client.calls))
        self.assertFalse(self.store.get_conversation('conversation-1')['native_delete_pending'])

    async def test_batched_goal_terminals_persist_suppression_before_delivery_callbacks(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start();await self.change();await asyncio.sleep(.02)
        captured=[];notices=[]
        def deferred(coro,name):
            future=asyncio.get_running_loop().create_future()
            captured.append((coro,name,future))
            return future
        async def claim(cid,tid,snapshot,*,recovery=False):
            if self.store.claim_completion_notification(cid,tid,recovery=recovery):
                notices.append(tid);self.store.record_completion_notification(cid,tid,sent=True,detail='fixture no external delivery')
        with mock.patch.object(self.service,'_deliver_completion_notification',side_effect=claim),mock.patch.object(self.service,'_deliver_web_push_notification',new=mock.AsyncMock()):
            self.runtime._terminal_handler=self.service.handle_terminal
            with mock.patch.object(self.runtime,'_spawn',side_effect=deferred):
                await self.complete_first()
                auto={'id':'batch-final','status':'inProgress','items':[]}
                self.factory.threads['thread-1']['turns'].append(copy.deepcopy(auto))
                self.factory.threads['thread-1']['status']={'type':'active'}
                await self.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':copy.deepcopy(auto)}})
                auto['status']='completed';self.factory.threads['thread-1']['turns'][-1]=copy.deepcopy(auto)
                self.factory.threads['thread-1']['status']={'type':'idle'}
                await self.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':auto}})
                self.factory.goals['thread-1']=native_goal(status='complete')
                await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':'thread-1','goal':self.factory.goals['thread-1']}})
            try:
                for coro,name,future in captured:
                    if name=='goal-terminal-reconcile':await coro
                    else:coro.close()
                    future.set_result(None)
                # Even after Goal completion, a late first-turn callback must
                # use its persisted decision, not the now-inactive Goal.
                await self.service.handle_terminal('conversation-1','turn-1',self.store.snapshot('conversation-1'))
                await self.service.handle_terminal('conversation-1','batch-final',self.store.snapshot('conversation-1'))
                self.assertEqual(['batch-final'],notices)
                turns={t['id']:t for t in self.store.snapshot('conversation-1')['turns']}
                self.assertEqual('goal_continuing',turns['turn-1']['notification_detail'])
                self.assertIsNone(turns['turn-1']['notification_state'])
                self.assertEqual('sent',turns['batch-final']['notification_state'])
            finally:
                for coro,_,future in captured:
                    coro.close()
                    if not future.done():future.cancel()

    async def test_new_goal_cycle_does_not_suppress_an_earlier_ordinary_terminal(self):
        self.store.set_completion_notification('conversation-1',True)
        await self.start();await self.complete_first();await self.change()
        self.assertEqual(2,self.runtime.goals.store.entry('conversation-1')['notification_floor'])
        with mock.patch.object(self.service,'_deliver_completion_notification',new=mock.AsyncMock()) as deliver,mock.patch.object(self.service,'_deliver_web_push_notification',new=mock.AsyncMock()):
            await self.service.handle_terminal('conversation-1','turn-1',self.store.snapshot('conversation-1'))
            deliver.assert_awaited_once()

    async def test_resume_absence_echo_preserves_unconfirmed_first_activation_draft(self):
        for error in (AppServerRPCError(-32600,'activation rejected'),AppServerDisconnected('activation not confirmed')):
            with self.subTest(error=type(error).__name__):
                cid='draft-'+type(error).__name__
                self.store.create_conversation(cid,title='test draft',cwd=str(self.root),settings={'model':'fixture','effort':'medium','permission':'read-only'})
                self.runtime.goals.save_draft(cid,'保留未确认目标')
                client=await self.runtime._ensure_client()
                original=client.set_thread_goal
                async def failed(*args,**kwargs):raise error
                client.set_thread_goal=failed
                first=await self.runtime.start_conversation(cid,command_id='first-'+type(error).__name__,text='用户原文')
                self.assertEqual('accepted',first['command']['state'])
                client.set_thread_goal=original
                entry=self.runtime.goals.store.entry(cid)
                self.assertTrue(entry['ever_native']);self.assertIsNone(entry['goal'])
                self.assertEqual('paused',entry['draft']['status'])
                tid=self.store.get_conversation(cid)['thread_id']
                # The resume absence notification precedes the strong GET.
                await self.runtime.handle_notification({'method':'thread/goal/cleared','params':{'threadId':tid}})
                self.assertEqual('保留未确认目标',self.runtime.goals.store.entry(cid)['draft']['objective'])
                await self.runtime.goals.refresh(cid)
                self.assertEqual('保留未确认目标',self.runtime.goals.store.public(cid)['goal_draft']['objective'])
                self.assertFalse(self.runtime.goals.continuing(cid))
                self.assertIsNone(self.factory.goals.get(tid))

    async def test_terminal_goal_echo_does_not_schedule_reconcile_feedback_loop(self):
        await self.start()
        await self.change()
        goal = native_goal(status='complete')
        message = {'method': 'thread/goal/updated', 'params': {'threadId': 'thread-1', 'goal': goal}}
        with mock.patch.object(self.runtime, '_spawn', wraps=self.runtime._spawn) as spawn:
            await self.runtime.handle_notification(message)
            await self.runtime.handle_notification(message)
            names = [args.args[1] for args in spawn.call_args_list]
            self.assertEqual(1, names.count('goal-terminal-reconcile'))

    async def test_goal_notification_does_not_announce_each_intermediate_turn(self):
        await self.start()
        await self.change()
        await self.complete_first()
        snapshot = self.store.snapshot('conversation-1')
        with mock.patch.object(self.service, '_deliver_completion_notification', new=mock.AsyncMock()) as deliver:
            await self.service.handle_terminal('conversation-1', 'turn-1', snapshot)
        deliver.assert_not_awaited()

    async def test_rejected_first_turn_with_active_draft_can_retry_without_native_gap_lock(self):
        self.runtime.goals.save_draft('conversation-1', '完成测试目标')
        client = await self.runtime._ensure_client()
        client.start_turn_error = AppServerRPCError(-32602, 'fixture rejection')
        first = await self.start()
        self.assertEqual('rejected', first['command']['state'])
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))
        self.assertFalse(any(m == 'goal/set' for m, _ in client.calls))
        client.start_turn_error = None
        retry = await self.runtime.send_message('conversation-1', command_id='explicit-retry', text='重试首轮')
        self.assertEqual('accepted', retry['command']['state'])
        self.assertEqual(1, len(self.factory.threads['thread-1']['turns']))
        self.assertEqual('active', self.runtime.goals.store.public('conversation-1')['goal']['status'])

    async def test_definitely_unsent_steer_can_start_user_turn_in_native_goal_gap(self):
        await self.start()
        await self.change()
        self.store.begin_command('conversation-1', 'correction', kind='steer', text='纠正')
        self.client.steer_error = AppServerRPCError(-32600, 'no active turn')
        self.factory.threads['thread-1']['turns'][0]['status'] = 'completed'
        self.factory.threads['thread-1']['status'] = {'type': 'idle'}
        async with self.runtime._conversation_locks['conversation-1']:
            result = await self.runtime._steer_locked('conversation-1', 'correction', '纠正', skill_selectors=[])
        self.assertEqual('accepted', result['command']['state'])
        starts = [v for m, v in self.client.calls if m == 'turn/start']
        self.assertEqual(2, len(starts))
        self.assertEqual('correction', starts[-1]['client_user_message_id'])

    async def idle_active_goal(self):
        await self.start()
        await self.complete_first()
        await self.change()
        await self.runtime.reconcile('conversation-1')
        self.assertEqual('idle', self.store.get_conversation('conversation-1')['state'])
        self.assertTrue(self.runtime.goals.continuing('conversation-1'))

    async def wait_for_receipt(self, command_id, state='accepted'):
        for _ in range(100):
            if self.store.command(command_id)['state'] == state:
                return self.store.command(command_id)
            await asyncio.sleep(.01)
        self.fail(f'{command_id}: {self.store.command(command_id)}')

    async def test_idle_active_goal_does_not_swallow_explicit_user_message(self):
        await self.idle_active_goal()
        before = copy.deepcopy(self.factory.goals['thread-1'])
        result = await self.runtime.send_message('conversation-1', command_id='go-on', text='去做吧')
        self.assertEqual('accepted', result['command']['state'])
        starts = [v for m, v in self.client.calls if m == 'turn/start']
        self.assertEqual(2, len(starts))
        self.assertEqual('go-on', starts[-1]['client_user_message_id'])
        self.assertEqual('去做吧', starts[-1]['text'])
        self.assertEqual(before, self.factory.goals['thread-1'])

    async def test_restart_starts_only_unsent_goal_queue_and_preserves_ids(self):
        await self.idle_active_goal()
        for command_id, text in [('queued-first', '开始实施'), ('queued-second', '补充要求')]:
            self.store.begin_command('conversation-1', command_id, kind='steer', text=text)
            self.store.update_command(command_id, 'queued', reason='native_goal_next_turn')
        await self.runtime.close()
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory)
        client = await self.runtime._ensure_client()
        client.turn_no = 1
        await self.runtime.startup_recover()
        first = await self.wait_for_receipt('queued-first')
        second = await self.wait_for_receipt('queued-second')
        self.assertEqual(first['result_turn_id'], second['result_turn_id'])
        starts = [v for m, v in client.calls if m == 'turn/start']
        steers = [v for m, v in client.calls if m == 'turn/steer']
        self.assertEqual(['queued-first'], [v['client_user_message_id'] for v in starts])
        self.assertEqual(['queued-second'], [v['client_user_message_id'] for v in steers])

    async def test_new_message_does_not_overtake_older_goal_queue(self):
        await self.idle_active_goal()
        self.store.begin_command('conversation-1', 'old-input', kind='steer', text='先执行这条')
        self.store.update_command('old-input', 'queued', reason='native_goal_next_turn')
        await self.runtime.send_message('conversation-1', command_id='new-input', text='然后这条')
        await self.wait_for_receipt('new-input')
        starts = [v for m, v in self.client.calls if m == 'turn/start']
        self.assertEqual('old-input', starts[-1]['client_user_message_id'])
        self.assertEqual(self.store.command('old-input')['result_turn_id'],
                         self.store.command('new-input')['result_turn_id'])

    async def test_idle_goal_without_user_queue_does_not_manufacture_turn(self):
        await self.idle_active_goal()
        await self.runtime.startup_recover()
        await self.runtime._drain_queue('conversation-1')
        self.assertEqual(1, len([m for m, _ in self.client.calls if m == 'turn/start']))

    async def test_goal_queue_never_replays_unknown_or_dispatches_past_it(self):
        await self.idle_active_goal()
        self.store.begin_command('conversation-1', 'uncertain', kind='steer', text='不确定是否送达')
        self.store.update_command('uncertain', 'delivery_unknown', reason='turn_delivery_unknown')
        self.store.begin_command('conversation-1', 'later', kind='steer', text='仍待发送')
        self.store.update_command('later', 'queued', reason='native_goal_next_turn')
        await self.runtime.reconcile('conversation-1')
        await self.runtime._drain_queue('conversation-1')
        self.assertEqual('delivery_unknown', self.store.command('uncertain')['state'])
        self.assertEqual('queued', self.store.command('later')['state'])
        self.assertEqual(1, len([m for m, _ in self.client.calls if m == 'turn/start']))

    async def test_unknown_early_goal_turn_does_not_prove_user_delivery(self):
        await self.idle_active_goal()
        async def lost_start(thread_id, text, **values):
            self.client.calls.append(('turn/start', {'client_user_message_id': values['client_user_message_id']}))
            turn = {'id': 'native-auto', 'status': 'inProgress', 'items': []}
            self.factory.threads[thread_id]['turns'].append(copy.deepcopy(turn))
            self.factory.threads[thread_id]['status'] = {'type': 'active'}
            await self.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': thread_id, 'turn': turn}})
            self.assertEqual('submitted', self.store.command('lost-input')['state'])
            raise AppServerDisconnected('lost response')
        with mock.patch.object(self.client, 'start_turn', new=lost_start):
            result = await self.runtime.send_message('conversation-1', command_id='lost-input', text='去做吧')
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.assertIsNone(result['command']['result_turn_id'])
        await self.runtime.reconcile('conversation-1')
        self.assertEqual('delivery_unknown', self.store.command('lost-input')['state'])
        self.assertFalse(any(m == 'turn/steer' for m, _ in self.client.calls))
        self.assertEqual(2, len([m for m, _ in self.client.calls if m == 'turn/start']))

    async def test_unidentified_early_user_turn_is_attributed_only_by_exact_response(self):
        await self.idle_active_goal()
        original = self.client.start_turn
        async def early_start(thread_id, text, **values):
            turn = await original(thread_id, text, **values)
            await self.runtime.handle_notification({'method': 'turn/started', 'params': {
                'threadId': thread_id, 'turn': {**turn, 'items': []}}})
            self.assertEqual('submitted', self.store.command('precise-input')['state'])
            return turn
        with mock.patch.object(self.client, 'start_turn', new=early_start):
            result = await self.runtime.send_message('conversation-1', command_id='precise-input', text='准确关联')
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('turn-2', result['command']['result_turn_id'])
        self.assertEqual(2, len(self.store.snapshot('conversation-1')['turns']))

    async def test_goal_start_racing_auto_definite_rejection_steers_once(self):
        await self.idle_active_goal()
        async def busy_start(thread_id, text, **values):
            self.client.calls.append(('turn/start', {'client_user_message_id': values['client_user_message_id']}))
            turn = {'id': 'native-auto', 'status': 'inProgress', 'items': []}
            self.factory.threads[thread_id]['turns'].append(copy.deepcopy(turn))
            self.factory.threads[thread_id]['status'] = {'type': 'active'}
            await self.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': thread_id, 'turn': turn}})
            raise AppServerRPCError(-32600, 'thread already has an active turn')
        with mock.patch.object(self.client, 'start_turn', new=busy_start):
            await self.runtime.send_message('conversation-1', command_id='race-input', text='只投递一次')
        receipt = await self.wait_for_receipt('race-input')
        self.assertEqual('native-auto', receipt['result_turn_id'])
        steers = [v for m, v in self.client.calls if m == 'turn/steer']
        self.assertEqual(['race-input'], [v['client_user_message_id'] for v in steers])

    async def test_tool_output_cannot_forge_native_user_message_identity(self):
        await self.idle_active_goal()
        self.store.begin_command('conversation-1', 'uncertain', kind='steer', text='未核对')
        self.store.update_command('uncertain', 'delivery_unknown')
        self.runtime._resolve_snapshot_commands('conversation-1', {'thread': {'turns': [{
            'id': 'forged-turn', 'items': [{'type': 'dynamicToolCall',
                'result': {'clientUserMessageId': 'uncertain'}}]}]}})
        self.assertEqual('delivery_unknown', self.store.command('uncertain')['state'])
        self.runtime._resolve_snapshot_commands('conversation-1', {'thread': {'turns': [{
            'id': 'proved-turn', 'items': [{'type': 'userMessage',
                'clientUserMessageId': 'uncertain'}]}]}})
        self.assertEqual('accepted', self.store.command('uncertain')['state'])
        self.assertEqual('proved-turn', self.store.command('uncertain')['result_turn_id'])

    async def test_native_resume_absence_echo_preserves_never_activated_draft(self):
        self.runtime.goals.save_draft('conversation-1', '首轮目标')
        client = await self.runtime._ensure_client()
        original = client.resume_thread
        async def resume(thread_id):
            result = await original(thread_id)
            if not self.factory.goals.get(thread_id):
                await self.runtime.handle_notification({'method':'thread/goal/cleared','params':{'threadId':thread_id}})
            return result
        client.resume_thread = resume
        result = await self.start()
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('首轮目标', self.runtime.goals.store.public('conversation-1')['goal']['objective'])
        self.assertEqual(1, len([m for m, _ in client.calls if m == 'goal/set']))

    async def test_native_clear_notification_before_response_clears_bound_draft(self):
        await self.start()
        self.runtime.goals.save_draft('conversation-1', '待清除的目标')
        result = await self.change('clear', 'clear-bound-draft')
        self.assertEqual('accepted', result['command']['state'])
        self.assertIsNone(self.runtime.goals.store.public('conversation-1')['goal_draft'])
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))

    async def test_recovered_auto_turn_delivers_user_correction_but_skips_scheduled_head(self):
        await self.start()
        await self.change()
        await self.complete_first()
        self.store.begin_command('conversation-1', 'scheduled-head', kind='steer', origin='scheduled', text='定时任务')
        self.store.update_command('scheduled-head', 'queued')
        self.store.begin_command('conversation-1', 'user-correction', kind='steer', text='用户纠正')
        self.store.update_command('user-correction', 'queued')
        auto = {'id': 'missed-auto', 'status': 'inProgress', 'items': []}
        self.factory.threads['thread-1']['turns'].append(copy.deepcopy(auto))
        self.factory.threads['thread-1']['status'] = {'type': 'active'}
        self.store.update_settings('conversation-1', {'pending_for_next_turn': True, '_goal_settings_after_turn': 'turn-1'})
        await self.runtime.reconcile('conversation-1', origin='missing-notification')
        for _ in range(40):
            if self.store.command('user-correction')['state'] == 'accepted': break
            await asyncio.sleep(.01)
        self.assertEqual('accepted', self.store.command('user-correction')['state'])
        self.assertEqual('queued', self.store.command('scheduled-head')['state'])
        self.assertFalse(self.store.get_conversation('conversation-1')['settings']['pending_for_next_turn'])
        await self.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': auto}})
        await asyncio.sleep(.02)
        steers = [v for m, v in self.client.calls if m == 'turn/steer']
        self.assertEqual(['用户纠正'], [v['text'] for v in steers])
        self.assertEqual('missed-auto', steers[0]['turn_id'])
        self.assertEqual(1, len([m for m, _ in self.client.calls if m == 'turn/start']))

    async def test_terminal_projection_crash_before_settlement_is_recovered_once(self):
        self.store.set_completion_notification('conversation-1', True)
        await self.start()
        await self.change()
        await self.complete_first()
        self.store._db.execute("UPDATE turns SET notification_requested=1 WHERE id='turn-1'")
        self.runtime.goals.store.suppress_notice('conversation-1', 'turn-1')
        self.factory.goals['thread-1'] = native_goal(status='complete')
        self.runtime.goals.store.put('conversation-1', goal=native_goal(status='complete'), settlement_pending=True)
        self.store.set_conversation_state('conversation-1', 'idle', active_turn_id=None)
        self.assertIn('conversation-1', self.runtime.goals.store.recoverable_ids())
        await self.runtime.close()
        restored = []
        async def terminal(cid, tid, snap):
            turn = next(t for t in self.store.snapshot(cid)['turns'] if t['id']==tid)
            if turn['notification_requested'] and turn['notification_state'] is None:
                restored.append(tid)
                self.store._db.execute("UPDATE turns SET notification_state='delivered' WHERE id=? AND conversation_id=?", (tid,cid))
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory, terminal_handler=terminal)
        self.service.runtime = self.runtime
        await self.runtime.startup_recover()
        for _ in range(50):
            if not self.runtime.goals.store.entry('conversation-1')['settlement_pending']: break
            await asyncio.sleep(.01)
        self.assertFalse(self.runtime.goals.store.entry('conversation-1')['settlement_pending'])
        self.assertEqual(['turn-1'], restored)
        await self.runtime.startup_recover()
        await asyncio.sleep(.02)
        self.assertEqual(['turn-1'], restored)

    async def test_unknown_goal_receipt_does_not_permanently_block_verified_native_thread(self):
        await self.start()
        self.client.response_error = AppServerDisconnected('lost goal receipt')
        result = await self.change()
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.client.response_error = None
        await self.complete_first()
        self.factory.goals['thread-1'] = native_goal(status='complete')
        await self.runtime.reconcile('conversation-1', origin='confirm-complete')
        self.assertEqual('idle', self.store.get_conversation('conversation-1')['state'])
        self.assertEqual('delivery_unknown', self.runtime.goals.store.command('goal-command')['state'], 'receipt uncertainty remains honest and non-replayable')
        result = await self.runtime.send_message('conversation-1', command_id='after-goal-complete', text='下一条用户消息')
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual(1, len([m for m, _ in self.client.calls if m == 'goal/set']))

    async def test_settings_sync_native_terminal_changes_prevent_stale_goal_activation(self):
        for terminal in ('complete','paused','clear'):
            with self.subTest(terminal=terminal):
                # Each subcase is its own owned thread/conversation.
                cid='race-'+terminal
                self.store.create_conversation(cid,title='race',cwd=str(self.root),settings={'model':'fixture','effort':'medium','permission':'read-only'})
                self.runtime.goals.save_draft(cid, '目标-'+terminal)
                client=await self.runtime._ensure_client()
                original=client.update_goal_thread_settings
                calls=0
                async def sync(thread_id, **settings):
                    nonlocal calls
                    await original(thread_id, **settings)
                    calls += 1
                    if calls == 2:
                        if terminal == 'clear':
                            # Confirmed native create then clear is not the
                            # absence echo of an attempted activation.
                            created=native_goal(thread_id,objective='目标-'+terminal)
                            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':thread_id,'goal':created}})
                            await self.runtime.handle_notification({'method':'thread/goal/cleared','params':{'threadId':thread_id}})
                        else:
                            goal=native_goal(thread_id,status=terminal,objective='目标-'+terminal)
                            self.factory.goals[thread_id]=goal
                            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':thread_id,'goal':goal}})
                client.update_goal_thread_settings=sync
                count=len([m for m,_ in client.calls if m=='goal/set'])
                result=await self.runtime.start_conversation(cid,command_id='first-'+terminal,text='原文')
                self.assertEqual('accepted',result['command']['state'])
                self.assertEqual(count,len([m for m,_ in client.calls if m=='goal/set']), 'stale mutation must not be sent')
                goal=self.runtime.goals.store.public(cid)['goal']
                self.assertIsNone(goal) if terminal=='clear' else self.assertEqual(terminal,goal['status'])
                client.update_goal_thread_settings=original

    async def test_goal_first_loaded_thread_does_not_resume_and_reconnect_does(self):
        self.runtime.goals.save_draft('conversation-1', '完成测试目标')
        client=await self.runtime._ensure_client()
        original=client.resume_thread
        client.resume_thread=mock.AsyncMock(side_effect=AppServerRPCError(-32601,'list_turns is not supported yet'))
        result=await self.start()
        self.assertEqual('accepted',result['command']['state'])
        await self.runtime.reconcile('conversation-1',origin='first-goal-confirm')
        self.assertEqual('active',self.store.get_conversation('conversation-1')['state'])
        client.resume_thread.assert_not_awaited()
        # A new connection must prove loading again; never carry cache trust.
        client.resume_thread=original
        client.connection_epoch += 1
        await self.runtime.goals.refresh('conversation-1')
        self.assertEqual(1,len([m for m,_ in client.calls if m=='thread/resume']))

    async def test_unavailable_unestablished_goal_does_not_block_ordinary_reconcile(self):
        await self.start()
        self.client.goal_error=AppServerRPCError(-32601,'unsupported goal')
        with self.assertRaises(AppServerRPCError):
            await self.runtime.goals.refresh('conversation-1')
        self.assertEqual('unavailable',self.runtime.goals.store.public('conversation-1')['goal_sync_state'])
        await self.complete_first()
        snapshot=await self.runtime.reconcile('conversation-1',origin='ordinary-thread')
        self.assertEqual('idle',snapshot['conversation']['state'])
        self.assertIn('thread/read',[m for m,_ in self.client.calls])
        self.client.calls.clear()
        await self.runtime.reconcile('conversation-1',origin='ordinary-thread-again')
        self.assertFalse(any(m=='goal/get' for m,_ in self.client.calls))

    async def test_explicit_next_turn_input_survives_late_same_turn_event_and_goes_to_next_auto(self):
        await self.start();await self.complete_first();self.client.emit_auto=True;await self.change()
        auto=copy.deepcopy(self.factory.threads['thread-1']['turns'][-1])
        result=await self.runtime.send_message('conversation-1',command_id='next-auto-only',text='等下一轮',force_new_turn=True)
        self.assertEqual('queued',result['command']['state'])
        await self.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':auto}})
        await asyncio.sleep(.02)
        self.assertEqual('queued',self.store.command('next-auto-only')['state'])
        self.assertFalse(any(m=='turn/steer' for m,_ in self.client.calls))
        auto['status']='completed';self.factory.threads['thread-1']['turns'][-1]=auto
        await self.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':auto}})
        next_turn={'id':'next-native-auto','status':'inProgress','items':[]}
        self.factory.threads['thread-1']['turns'].append(copy.deepcopy(next_turn))
        self.factory.threads['thread-1']['status']={'type':'active'}
        await self.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':next_turn}})
        for _ in range(50):
            if self.store.command('next-auto-only')['state']=='accepted':break
            await asyncio.sleep(.01)
        self.assertEqual('accepted',self.store.command('next-auto-only')['state'])
        self.assertEqual(['next-native-auto'],[v['turn_id'] for m,v in self.client.calls if m=='turn/steer'])

    async def test_settings_ack_uses_native_active_turn_even_before_delayed_started_notification(self):
        await self.start();await self.change()
        client=self.client
        original=client.update_goal_thread_settings
        turn={'id':'auto-before-settings-ack','status':'inProgress','items':[]}
        async def sync(thread_id, **settings):
            await original(thread_id, **settings)
            self.factory.threads[thread_id]['turns'][0]['status']='completed'
            self.factory.threads[thread_id]['turns'].append(copy.deepcopy(turn))
            self.factory.threads[thread_id]['status']={'type':'active'}
            # turn/started intentionally not delivered before RPC acknowledgement.
        client.update_goal_thread_settings=sync
        result=await self.runtime.update_settings('conversation-1',command_id='boundary-settings',updates={'permission':'read-only'})
        self.assertEqual('completed',result['command']['state'])
        settings=self.store.get_conversation('conversation-1')['settings']
        self.assertEqual('auto-before-settings-ack',settings['_goal_settings_after_turn'])
        self.assertTrue(settings['pending_for_next_turn'])
        await self.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':turn}})
        await asyncio.sleep(.02)
        self.assertTrue(self.store.get_conversation('conversation-1')['settings']['pending_for_next_turn'], 'old-configuration auto turn must not consume next-turn settings')
        client.update_goal_thread_settings=original

    async def test_first_unknown_activation_keeps_conservative_gate_and_quarantines_draft(self):
        self.runtime.goals.save_draft('conversation-1','完成测试目标')
        client=await self.runtime._ensure_client()
        async def lose_receipt(thread_id, **updates):
            client.calls.append(('goal/set',{'threadId':thread_id,**updates}))
            self.factory.goals[thread_id]=native_goal(thread_id)
            raise AppServerDisconnected('goal response and notification lost')
        client.set_thread_goal=lose_receipt
        await self.start()
        entry=self.runtime.goals.store.entry('conversation-1')
        self.assertEqual('paused',entry['draft']['status'])
        self.assertTrue(entry['continuing'],'unknown activation must not be interpreted as inactive')
        self.runtime.goals.quarantine_drafts()
        self.assertTrue(self.runtime.goals.continuing('conversation-1'))
        await self.runtime.goals.refresh('conversation-1')
        self.assertIsNone(self.runtime.goals.store.entry('conversation-1')['draft'])
        self.assertEqual('active',self.runtime.goals.store.public('conversation-1')['goal']['status'])
        self.assertEqual(1,len([m for m,_ in client.calls if m=='goal/set']))

    async def test_terminal_goal_discovered_by_get_restores_notice_without_terminal_notification(self):
        self.store.set_completion_notification('conversation-1', True)
        await self.start();await self.change();await self.complete_first();await asyncio.sleep(.02)
        self.store._db.execute("UPDATE turns SET notification_requested=1,notification_state=NULL WHERE id='turn-1'")
        self.runtime.goals.store.suppress_notice('conversation-1','turn-1')
        self.factory.goals['thread-1']=native_goal(status='complete')
        self.terminals.clear()
        await self.runtime.goals.refresh('conversation-1')
        for _ in range(50):
            if not self.runtime.goals.store.entry('conversation-1')['settlement_pending']:break
            await asyncio.sleep(.01)
        turn=next(t for t in self.store.snapshot('conversation-1')['turns'] if t['id']=='turn-1')
        self.assertTrue(turn['notification_requested'])
        self.assertEqual(['turn-1'],self.terminals)
        await self.runtime.goals.refresh('conversation-1');await asyncio.sleep(.02)
        self.assertEqual(['turn-1'],self.terminals,'same terminal read must not create a reconciliation echo loop')

    async def test_first_goal_during_ordinary_fast_rpc_gets_full_settings_boundary(self):
        await self.start()
        original = self.client.update_thread_settings
        async def update(thread_id, service_tier):
            await original(thread_id, service_tier)
            goal = native_goal(thread_id)
            self.factory.goals[thread_id] = goal
            await self.runtime.handle_notification({'method':'thread/goal/updated', 'params':{'threadId':thread_id,'goal':goal}})
        self.client.update_thread_settings = update
        result = await self.runtime.update_settings('conversation-1',command_id='fast-first-goal',updates={'service_tier':'priority'})
        self.assertEqual('completed', result['command']['state'])
        settings = self.store.get_conversation('conversation-1')['settings']
        self.assertEqual('turn-1', settings['_goal_settings_after_turn'])
        self.assertEqual('priority', settings['service_tier'])
        synced = [v for m,v in self.client.calls if m=='goal/settings'][-1]
        self.assertEqual('priority', synced['service_tier'])
        self.assertEqual('danger-full-access', synced['permission'])

    async def test_offline_goal_final_turn_restores_only_goal_cycle_notice(self):
        self.store.set_completion_notification('conversation-1', True)
        await self.start(); await self.complete_first(); await self.change()
        # No turn/started, turn/completed or terminal Goal notification arrives.
        final = {'id':'offline-goal-final','status':'completed','items':[]}
        self.factory.threads['thread-1']['turns'].append(final)
        self.factory.threads['thread-1']['status']={'type':'idle'}
        self.factory.goals['thread-1']=native_goal(status='complete')
        self.terminals.clear()
        await self.runtime.goals.refresh('conversation-1')
        for _ in range(50):
            if not self.runtime.goals.store.entry('conversation-1')['settlement_pending']:break
            await asyncio.sleep(.01)
        turns = {t['id']:t for t in self.store.snapshot('conversation-1')['turns']}
        self.assertTrue(turns['offline-goal-final']['notification_requested'])
        self.assertEqual(2, turns['offline-goal-final']['ordinal'])
        self.assertIsNone(self.runtime.goals.store.entry('conversation-1')['notification_floor'])
        self.assertIn('offline-goal-final', self.terminals)
        # An ordinary historical terminal outside this cycle still gets no
        # notification merely because a terminal Goal can be read again.
        self.factory.threads['thread-1']['turns'].append({'id':'ordinary-history','status':'completed','items':[]})
        await self.runtime.reconcile('conversation-1',origin='ordinary-history')
        historical=next(t for t in self.store.snapshot('conversation-1')['turns'] if t['id']=='ordinary-history')
        self.assertFalse(historical['notification_requested'])

    async def test_terminal_rpc_response_before_notification_settles_notice(self):
        self.store.set_completion_notification('conversation-1', True)
        await self.start(); await self.change(); await self.complete_first()
        self.runtime.goals.store.suppress_notice('conversation-1','turn-1')
        async def response_only(thread_id, **updates):
            self.factory.goals[thread_id].update(updates)
            return copy.deepcopy(self.factory.goals[thread_id])
        self.client.set_thread_goal=response_only
        self.terminals.clear()
        result=await self.change('complete',command='complete-response-only')
        self.assertEqual('accepted',result['command']['state'])
        for _ in range(50):
            if not self.runtime.goals.store.entry('conversation-1')['settlement_pending']:break
            await asyncio.sleep(.01)
        turn=next(t for t in self.store.snapshot('conversation-1')['turns'] if t['id']=='turn-1')
        self.assertTrue(turn['notification_requested'])
        self.assertIn('turn-1',self.terminals)
        self.terminals.clear()
        await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':'thread-1','goal':copy.deepcopy(self.factory.goals['thread-1'])}})
        await asyncio.sleep(.02)
        self.assertFalse(self.runtime.goals.store.entry('conversation-1')['settlement_pending'])
        self.assertEqual([],self.terminals,'late RPC echo must not settle again')

    async def test_goal_receipt_is_additive_and_command_namespace_is_shared(self):
        await self.start()
        self.client.response_error=AppServerDisconnected('lost receipt')
        result=await self.change()
        self.assertEqual('delivery_unknown',result['command']['state'])
        self.assertIsNone(self.store._db.execute("SELECT 1 FROM commands WHERE id='goal-command'").fetchone())
        self.assertEqual('delivery_unknown',self.runtime.goals.store.command('goal-command')['state'])
        with self.assertRaises(AppServerProtocolError):
            self.store.begin_command('conversation-1','goal-command',kind='start',text='collision')
        with self.assertRaises(AppServerProtocolError):
            await self.change(command='first-command')
        again=await self.change(command='goal-command',version='')
        self.assertEqual('delivery_unknown',again['command']['state'])
        self.assertEqual(1,len([m for m,_ in self.client.calls if m=='goal/set']))

    async def test_offline_goal_notice_respects_disabled_current_preference(self):
        self.store.set_completion_notification('conversation-1', True)
        await self.start(); await self.complete_first(); await self.change()
        self.store.set_completion_notification('conversation-1', False)
        self.factory.threads['thread-1']['turns'].append({'id':'offline-no-notice','status':'completed','items':[]})
        self.factory.goals['thread-1']=native_goal(status='complete')
        await self.runtime.goals.refresh('conversation-1')
        for _ in range(50):
            if not self.runtime.goals.store.entry('conversation-1')['settlement_pending']:break
            await asyncio.sleep(.01)
        turn=next(t for t in self.store.snapshot('conversation-1')['turns'] if t['id']=='offline-no-notice')
        self.assertFalse(turn['notification_requested'])

    async def test_negative_activation_ack_restores_cycle_floor_and_respects_new_native_state(self):
        await self.start(); await self.complete_first()
        await self.change(status='paused')
        self.client.goal_error=AppServerRPCError(-32600,'fixture rejection')
        # Allow the authoritative preflight read, reject only the mutation.
        self.client.goal_error=None
        original=self.client.set_thread_goal
        async def rejected(*args,**kwargs):
            raise AppServerRPCError(-32600,'fixture rejection')
        self.client.set_thread_goal=rejected
        result=await self.change('resume',command='rejected-resume')
        self.assertEqual('rejected',result['command']['state'])
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))
        self.assertIsNone(self.runtime.goals.store.entry('conversation-1')['notification_floor'])
        self.client.set_thread_goal=original
        await self.change('resume',command='accepted-resume')
        async def sync_then_rejected(thread_id,**kwargs):
            final=native_goal(thread_id,status='complete')
            self.factory.goals[thread_id]=final
            await self.runtime.handle_notification({'method':'thread/goal/updated','params':{'threadId':thread_id,'goal':final}})
            raise AppServerRPCError(-32600,'settings rejected')
        self.client.update_goal_thread_settings=sync_then_rejected
        result=await self.change('resume',command='concurrent-complete')
        self.assertEqual('rejected',result['command']['state'])
        self.assertEqual('complete',self.runtime.goals.store.public('conversation-1')['goal']['status'])
        self.assertFalse(self.runtime.goals.continuing('conversation-1'))

    async def test_goal_submitted_receipt_recovers_unknown_without_entering_turn_journal(self):
        self.runtime.goals.store.begin_command('conversation-1','unfinished-goal',payload={'action':'pause'})
        await self.runtime.close()
        self.runtime=ConversationRuntime(self.store,self.artifacts,self.factory)
        self.service.runtime=self.runtime
        receipt=self.runtime.goals.store.command('unfinished-goal')
        self.assertEqual('delivery_unknown',receipt['state'])
        self.assertEqual([],self.store.commands_with_state('conversation-1','delivery_unknown'))
        self.assertEqual('new',self.store.get_conversation('conversation-1')['state'])

    async def test_goal_http_forms_prefix_auth_origin_and_command_contract(self):
        from fastapi import FastAPI
        from middleware.request_origin import RequestOriginMiddleware
        from routes import conversation_api
        from shared import config
        from test_request_origin_middleware import call, scope, status, ORIGIN
        from urllib.parse import urlencode
        from fastapi.responses import JSONResponse
        app = FastAPI()
        app.include_router(conversation_api.router, prefix='/workbench')
        app.add_middleware(RequestOriginMiddleware, router=app.router)
        def payload(messages):
            return json.loads(b''.join(m.get('body', b'') for m in messages if m['type']=='http.response.body'))
        fields = {'command_id':'http-goal','action':'set','objective':'手机提交目标','status':'paused','goal_version':''}
        headers=[(b'content-type',b'application/x-www-form-urlencoded')]
        path='/workbench/conversation/conversation-1/goal'
        with mock.patch.object(config,'PASSKEY_ORIGIN',ORIGIN), mock.patch.object(conversation_api,'conversation_service',return_value=self.service), mock.patch.object(conversation_api,'_auth',return_value=None) as auth:
            messages,_=await call(app,scope(path,headers=headers),urlencode(fields).encode())
            self.assertEqual(200,status(messages))
            data=payload(messages)
            self.assertEqual('accepted',data['command']['state'])
            self.assertEqual('手机提交目标',data['snapshot']['goal_draft']['objective'])
            self.assertEqual('paused',data['snapshot']['goal_draft']['status'])
            self.assertIsNone(data['snapshot']['goal_draft']['tokenBudget'])
            self.assertFalse(self.factory.clients, 'unbound goal edit must not create a native thread')
            messages,_=await call(app,scope(path,method='GET'),b'')
            self.assertEqual(200,status(messages))
            self.assertEqual('手机提交目标',payload(messages)['snapshot']['goal_draft']['objective'])
            count=auth.call_count
            messages,received=await call(app,scope(path,headers=headers,origin='https://evil.test'),urlencode(fields).encode())
            self.assertEqual(403,status(messages))
            self.assertEqual([],received,'origin rejection must precede body parsing')
            self.assertEqual(count,auth.call_count)
            auth.return_value=JSONResponse({'error':'authentication_required'},401)
            messages,_=await call(app,scope(path,headers=headers),urlencode({**fields,'command_id':'unauth'}).encode())
            self.assertEqual(401,status(messages))
            self.assertFalse(any(c['id']=='unauth' for c in self.store.snapshot('conversation-1')['commands']))
        # Explicit budgets are typed and validated at the HTTP boundary.
        with mock.patch.object(config,'PASSKEY_ORIGIN',ORIGIN), mock.patch.object(conversation_api,'conversation_service',return_value=self.service), mock.patch.object(conversation_api,'_auth',return_value=None):
            messages,_=await call(app,scope(path,headers=headers),urlencode({**fields,'token_budget':'not-an-int'}).encode())
            self.assertEqual(422,status(messages))



class GoalWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_names_null_and_invalid_foreign_identity(self):
        client = object.__new__(CodexAppServerClient)
        client.request = mock.AsyncMock(return_value={'goal': None})
        self.assertIsNone(await client.get_thread_goal('thread-1'))
        client.request.assert_awaited_with('thread/goal/get', {'threadId': 'thread-1'})
        client.request.return_value = {'goal': native_goal('other')}
        with self.assertRaises(AppServerProtocolError):
            await client.get_thread_goal('thread-1')
        with self.assertRaises(AppServerProtocolError):
            validate_goal(native_goal(tokensUsed=float('nan')), 'thread-1')

    async def test_resume_rpc_omits_objective_and_budget(self):
        client = object.__new__(CodexAppServerClient)
        client.request = mock.AsyncMock(return_value={'goal': native_goal()})
        await client.set_thread_goal('thread-1', status='active')
        client.request.assert_awaited_with('thread/goal/set', {'threadId': 'thread-1', 'status': 'active'})
