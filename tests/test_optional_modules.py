"""Deployment options exercise real journals; no real model, sender or service."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from engine.conversation_automation import AutomationStore
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from engine.codex_app_server import AppServerDisconnected, AppServerRPCError
from routes import automations as api
from shared.conversation_artifacts import ConversationArtifactService
from tests.test_conversation_runtime import FakeFactory
from tests.test_conversation_service import FakeRuntime
from tests.test_conversation_goals import GoalFactory
from tests.test_settings_center import request


class DeploymentOptionsTests(unittest.TestCase):
    def test_legacy_defaults_and_explicit_core_profile(self):
        code = ('import json; from shared.config import AUTOMATIONS_ENABLED as a, '
                'COMPLETION_NOTIFICATIONS_ENABLED as n; print(json.dumps([a,n]))')
        environment = os.environ.copy()
        for name in ('CODEX_WEB_AUTOMATIONS_ENABLED', 'CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED'):
            environment.pop(name, None)
        for value, expected in ((None, [True, True]), ('0', [False, False]),
                                ('1', [True, True]), ('typo', [False, False])):
            with self.subTest(value=value):
                env = environment.copy()
                if value is not None:
                    env.update(CODEX_WEB_AUTOMATIONS_ENABLED=value,
                               CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED=value)
                result = subprocess.run([sys.executable, '-c', code], env=env,
                                        check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout), expected)


class OptionalModuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optional-modules-', dir=Path.home())
        self.root = Path(self.temp.name)
        self.factory = FakeFactory()
        self.notify_patch = mock.patch('shared.security.notify_task_completion', return_value=(True, 'fixture'))
        self.notify = self.notify_patch.start()
        self.accounts = mock.patch('shared.security.load_accounts', return_value={
            'owner': {'enabled': True, 'role': 'admin'}})
        self.accounts.start()
        self.service = None
        await self.reopen()

    async def asyncTearDown(self):
        if self.service is not None:
            await self.service.close()
        self.accounts.stop()
        self.notify_patch.stop()
        self.temp.cleanup()

    async def reopen(self, *, automations=False, notifications=False, native=False):
        if self.service is not None:
            await self.service.close()
        self.store = ConversationStore(self.root / 'state' / 'conversations.sqlite3')
        artifacts = ConversationArtifactService(self.store, self.root / 'artifacts')
        self.runtime = ConversationRuntime(self.store, artifacts, self.factory) if native else FakeRuntime()
        self.service = ConversationService(self.store, artifacts, self.runtime,
            enable_automations=automations, enable_completion_notifications=notifications)

    def conversation(self, name='c1'):
        self.store.create_conversation(name, title='合成对话', cwd=str(self.root),
            owner_account_id='owner', completion_notification=True,
            settings={'model': 'fixture', 'effort': 'medium', 'permission': 'read-only'})
        self.store.bind_thread(name, {'id': 'thread-' + name}, server_version='fixture')

    def turn(self, name='c1', turn='turn-1', *, complete=True):
        self.store.begin_command(name, 'command-' + turn, kind='start', text='合成输入')
        self.store.bind_turn(name, {'id': turn, 'status': 'inProgress'}, command_id='command-' + turn)
        if complete:
            self.store.apply_notification({'method': 'turn/completed', 'params': {
                'threadId': 'thread-' + name,
                'turn': {'id': turn, 'status': 'completed', 'items': []}}})

    def task(self, *, due=None, **extra):
        values = {'title': '合成任务', 'prompt': '只用于夹具',
                  'schedule': {'kind': 'interval', 'interval_minutes': 1}}
        values.update(extra)
        task = self.service.automations.create('c1', 'owner', **values)
        if due:
            self.store._db.execute('UPDATE conversation_automation_tasks SET next_run_at=? WHERE id=?',
                (due.isoformat().replace('+00:00', 'Z'), task['id']))
        return task

    async def test_disabled_startup_has_no_scheduler_and_does_not_send_pending(self):
        self.conversation()
        self.turn()
        task = self.task(due=datetime.now(timezone.utc) - timedelta(seconds=1))
        with mock.patch('engine.conversation_automation.AutomationScheduler.start',
                        side_effect=AssertionError('must not start scheduler')):
            await self.service.startup()
            await asyncio.sleep(.02)
        self.assertIsNone(self.service._scheduler)
        self.notify.assert_not_called()
        self.assertEqual(self.service.automations.runs(task['id'], 'owner'), [])
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_state'], 'skipped')
        self.assertTrue(self.store.get_conversation('c1')['completion_notification'])

    async def test_notification_enable_consumes_old_intents_but_future_turn_can_send(self):
        self.conversation()
        self.turn()
        await self.service.startup()
        self.turn(turn='turn-2', complete=False)
        await self.reopen(notifications=True)
        await self.service.startup()
        self.notify.assert_not_called()
        self.store.apply_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {'id': 'turn-2', 'status': 'completed', 'items': []}}})
        await self.service._deliver_completion_notification('c1', 'turn-2', self.store.snapshot('c1'))
        self.notify.assert_not_called()
        self.turn(turn='turn-3')
        await self.service._deliver_completion_notification('c1', 'turn-3', self.store.snapshot('c1'))
        self.notify.assert_called_once()
        self.assertEqual(self.store.snapshot('c1')['turns'][-1]['notification_state'], 'sent')

    async def test_disabled_live_notification_is_not_replayed_and_sending_is_not_reset(self):
        self.conversation()
        self.turn()
        await self.service._deliver_completion_notification('c1', 'turn-1', self.store.snapshot('c1'))
        self.assertFalse(self.store.claim_completion_notification('c1', 'turn-1'))
        self.notify.assert_not_called()
        self.turn(turn='turn-2')
        self.assertTrue(self.store.claim_completion_notification('c1', 'turn-2'))
        await self.service._deliver_completion_notification('c1', 'turn-2', self.store.snapshot('c1'))
        self.assertEqual(self.store.snapshot('c1')['turns'][-1]['notification_state'], 'sending')
        await self.service.startup()
        self.assertEqual(self.store.snapshot('c1')['turns'][-1]['notification_state'], 'unknown')

    async def test_legacy_enabled_startup_keeps_existing_fresh_notification_recovery(self):
        self.conversation()
        self.turn()
        await self.reopen(notifications=True)
        await self.service.startup()
        self.notify.assert_called_once()

    async def test_reenable_skips_short_disabled_slots_once_and_keeps_paused_and_future(self):
        self.conversation()
        now = datetime.now(timezone.utc)
        interval = self.task(due=now - timedelta(seconds=10))
        once = self.task(due=now - timedelta(seconds=5), schedule={
            'kind': 'once', 'run_at': (now + timedelta(minutes=1)).isoformat()})
        paused = self.task(initial_state='paused')
        future = self.task(due=now + timedelta(minutes=5))
        journal = self.service.automations
        journal.configure_module(False, now - timedelta(seconds=20))
        await self.reopen(automations=True)
        journal = self.service.automations
        journal.configure_module(True, now)
        self.assertEqual(journal.claim_due(now), [])
        self.assertEqual(journal.get(once['id'], 'owner')['state'], 'finished')
        self.assertEqual(journal.get(paused['id'], 'owner'), paused)
        self.assertEqual(journal.get(future['id'], 'owner')['next_run_at'],
                         (now + timedelta(minutes=5)).isoformat(timespec='microseconds').replace('+00:00', 'Z'))
        for task in (interval, once):
            runs = journal.runs(task['id'], 'owner')
            self.assertEqual([(run['status'], run['reason']) for run in runs], [('skipped', 'module_disabled')])
        journal.configure_module(True, now + timedelta(seconds=1))
        self.assertEqual(len(journal.runs(interval['id'], 'owner')), 1)
        next_slot = datetime.fromisoformat(journal.get(interval['id'], 'owner')['next_run_at'].replace('Z', '+00:00'))
        self.assertEqual(len(journal.claim_due(next_slot)), 1)

    async def test_normal_enabled_restart_does_not_change_maintenance_grace_policy(self):
        self.conversation()
        now = datetime.now(timezone.utc)
        task = self.task(due=now - timedelta(seconds=10))
        journal = self.service.automations
        journal.configure_module(True, now)
        journal.configure_module(True, now)
        claimed = journal.claim_due(now)
        self.assertEqual([item[0]['id'] for item in claimed], [task['id']])

    async def test_reenable_transaction_failure_does_not_partially_advance_tasks(self):
        self.conversation()
        now = datetime.now(timezone.utc)
        tasks = [self.task(due=now - timedelta(seconds=10)) for _ in range(2)]
        journal = self.service.automations
        journal.configure_module(False, now)
        skip = journal._skip_due
        count = 0
        def fail_after_second(task, current, reason):
            nonlocal count
            skip(task, current, reason)
            count += 1
            if count == 2:
                raise OSError('synthetic transaction failure')
        with mock.patch.object(journal, '_skip_due', side_effect=fail_after_second):
            with self.assertRaises(OSError):
                journal.configure_module(True, now)
        for task in tasks:
            self.assertEqual(journal.runs(task['id'], 'owner'), [])
        self.assertEqual(self.store._db.execute("SELECT value FROM schema_meta WHERE key='automation_module_enabled'").fetchone()[0], '0')
        journal.configure_module(True, now)
        self.assertEqual(sum(len(journal.runs(task['id'], 'owner')) for task in tasks), 2)

    async def test_disabled_api_and_tool_reject_mutations_but_allow_read_pause_delete(self):
        await self.reopen(native=True)
        self.conversation()
        task = self.task()
        payload = {'conversation_id': 'c1', 'title': '禁止新增', 'prompt': '合成',
            'schedule': {'kind': 'interval', 'interval_minutes': 1},
            'request_key': 'new', 'confirm_unattended': True}
        with (mock.patch.object(api, 'require_auth', return_value=None),
              mock.patch.object(api, 'logged_in', return_value='owner'),
              mock.patch.object(api, 'conversation_service', return_value=self.service),
              mock.patch.object(api, 'audit')):
            value = json.loads(api.tasks(request()).body)
            self.assertFalse(value['module_enabled'])
            self.assertFalse(value['scheduler_running'])
            self.assertIsNone(self.service._scheduler)
            self.assertEqual((await api.create_task(request(payload))).status_code, 400)
            for change in ({'state': 'active'}, {'prompt': '禁止编辑'}):
                response = await api.update_task(request({'revision': 1, 'request_key': 'mutate',
                    'confirm_unattended': True, **change}), task['id'])
                self.assertEqual(response.status_code, 400)
                self.assertIn('安装配置', json.loads(response.body)['error'])
            self.assertEqual((await api.update_task(request({'revision': 1, 'state': 'paused',
                'request_key': 'pause'}), task['id'])).status_code, 200)
            self.assertEqual((await api.update_task(request({'revision': 2, 'state': 'deleted',
                'request_key': 'delete'}), task['id'])).status_code, 200)
        conversation = self.store.get_conversation('c1')
        for action in ('create', 'update', 'resume'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, '此部署未启用'):
                self.service._automation_tool(conversation, 't', 'call-' + action, {'action': action})
        result = self.service._automation_tool(conversation, 't', 'call-list', {'action': 'list'})
        self.assertTrue(result['success'])

    async def test_core_conversation_keeps_inert_tool_registration_for_later_enable(self):
        await self.reopen(native=True)
        self.store.create_conversation('core', title='核心仍可用', cwd=str(self.root),
            owner_account_id='owner', settings={'permission': 'read-only', 'model': 'fixture', 'effort': 'medium'})
        result = await self.runtime.start_conversation('core', command_id='start-core', text='合成输入')
        self.assertEqual(result['command']['state'], 'accepted')
        client = self.factory.clients[-1]
        thread = next(value for method, value in client.calls if method == 'thread/start')
        self.assertEqual([tool['name'] for tool in thread['dynamic_tools']], ['publish_artifact', 'code_review', 'manage_scheduled_task'])
        turn = next(value for method, value in client.calls if method == 'turn/start')
        self.assertIn('manage_scheduled_task', turn.get('leading_context') or '')
        self.assertIsNone(self.service._scheduler)
        arguments = {'action': 'create', 'title': '后来启用', 'prompt': '合成任务',
                     'schedule': {'kind': 'interval', 'interval_minutes': 60}}
        with self.assertRaisesRegex(ValueError, '此部署未启用'):
            self.service._automation_tool(self.store.get_conversation('core'), 'turn-1', 'off', arguments)
        await self.reopen(automations=True, native=True)
        await self.service.startup()
        result = await self.runtime.handle_server_request('item/tool/call', {
            'threadId': 'thread-1', 'turnId': 'turn-1', 'callId': 'after-enable',
            'tool': 'manage_scheduled_task', 'arguments': arguments})
        self.assertTrue(result['success'])
        self.assertEqual(self.store.get_conversation('core')['thread_id'], 'thread-1')
        self.assertEqual(len(self.service.automations.list('owner', 'core')), 1)

    async def recover_previously_unbound_turn(self, *, terminal_during_recovery=False):
        self.conversation()
        await self.service.startup()
        await self.reopen(notifications=True)
        async def recover():
            self.store.reconcile_thread('c1', {'thread': {'id': 'thread-c1',
                'status': {'type': 'idle' if terminal_during_recovery else 'active'},
                'turns': [{'id': 'late-native',
                           'status': 'completed' if terminal_during_recovery else 'inProgress', 'items': []}]}})
            if terminal_during_recovery:
                await self.service._deliver_completion_notification('c1', 'late-native', self.store.snapshot('c1'), recovery=True)
            return []
        self.runtime.startup_recover = mock.AsyncMock(side_effect=recover)
        await self.service.startup()
        if not terminal_during_recovery:
            self.store.apply_notification({'method': 'turn/completed', 'params': {
                'threadId': 'thread-c1', 'turn': {'id': 'late-native', 'status': 'completed', 'items': []}}})
            await self.service._deliver_completion_notification('c1', 'late-native', self.store.snapshot('c1'))
        self.notify.assert_not_called()

    async def test_reenabled_notification_suppresses_old_active_turn_imported_during_recovery(self):
        await self.recover_previously_unbound_turn()

    async def test_reenabled_notification_suppresses_old_terminal_callback_during_recovery(self):
        await self.recover_previously_unbound_turn(terminal_during_recovery=True)

    async def test_failed_reenable_recovery_keeps_durable_boundary_for_late_import(self):
        self.conversation()
        await self.service.startup()
        await self.reopen(notifications=True)
        self.runtime.startup_recover = mock.AsyncMock(side_effect=OSError('synthetic recovery failure'))
        with self.assertRaises(OSError):
            await self.service.startup()
        self.assertIsNotNone(self.store._db.execute("SELECT value FROM schema_meta WHERE key='completion_notifications_epoch'").fetchone())
        self.store.reconcile_thread('c1', {'thread': {'id': 'thread-c1', 'status': {'type': 'active'},
            'turns': [{'id': 'late-native', 'status': 'inProgress', 'items': []}]}})
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 0)

    async def failed_native_recovery_then_late_import(self, *, read_failure=False):
        await self.reopen(native=True)
        self.conversation()
        self.store.set_conversation_state('c1', 'active')
        self.store.configure_completion_notifications(False)
        self.factory.threads['thread-c1'] = {'id': 'thread-c1', 'status': {'type': 'active'},
            'turns': [{'id': 'late-native', 'status': 'inProgress', 'items': []}]}
        await self.reopen(notifications=True, native=True)
        if read_failure:
            client = await self.runtime._ensure_client()
            original = client.read_thread
            client.read_thread = mock.AsyncMock(side_effect=AppServerRPCError(-32000, 'synthetic read failure'))
        else:
            self.factory.connect_errors = [AppServerDisconnected('synthetic connect failure')]
        recovered = await self.service.startup()
        self.assertEqual(self.store.get_conversation('c1')['state'], 'needs_user')
        if read_failure:
            client.read_thread = original
        await self.runtime.reconcile('c1')
        self.store.apply_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {'id': 'late-native', 'status': 'completed', 'items': []}}})
        await self.service._deliver_completion_notification('c1', 'late-native', self.store.snapshot('c1'))
        self.notify.assert_not_called()

    async def test_real_runtime_connect_failure_normal_return_cannot_revive_old_notification(self):
        await self.failed_native_recovery_then_late_import()

    async def test_real_runtime_read_failure_normal_return_cannot_revive_old_notification(self):
        await self.failed_native_recovery_then_late_import(read_failure=True)

    async def test_recovery_goal_receipt_cannot_prove_a_new_notification(self):
        self.factory = GoalFactory()
        await self.reopen(native=True)
        self.conversation()
        self.store.configure_completion_notifications(False)
        self.store.set_conversation_state('c1', 'active')
        self.runtime.goals.store.put('c1', ever_native=True, continuing=False)
        self.factory.threads['thread-c1'] = {'id': 'thread-c1', 'status': {'type': 'active'},
            'turns': [{'id': 'old-auto', 'status': 'inProgress', 'items': []}]}
        await self.reopen(notifications=True, native=True)
        await self.service.startup()
        turn = self.store.snapshot('c1')['turns'][0]
        self.assertEqual(turn['notification_requested'], 0)
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {'id': 'old-auto', 'status': 'completed', 'items': []}}})
        # A Goal settlement must not bypass the global deployment boundary.
        self.store._db.execute("UPDATE turns SET notification_requested=1,notification_detail=NULL WHERE id='old-auto'")
        await self.service._deliver_completion_notification('c1', 'old-auto', self.store.snapshot('c1'))
        self.notify.assert_not_called()

    async def test_new_live_automatic_turn_after_enable_keeps_completion_notification(self):
        self.factory = GoalFactory()
        await self.reopen(native=True)
        self.store.configure_completion_notifications(False)
        self.conversation()
        self.runtime.goals.store.put('c1', ever_native=True, continuing=False)
        self.factory.threads['thread-c1'] = {'id': 'thread-c1', 'status': {'type': 'idle'}, 'turns': []}
        await self.reopen(notifications=True, native=True)
        await self.service.startup()
        turn = {'id': 'new-live-auto', 'status': 'inProgress', 'items': []}
        self.factory.threads['thread-c1'].update(status={'type': 'active'}, turns=[copy.deepcopy(turn)])
        await self.runtime.handle_notification({'method': 'turn/started', 'params': {
            'threadId': 'thread-c1', 'turn': turn}})
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 1)
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {**turn, 'status': 'completed'}}})
        await self.service._deliver_completion_notification('c1', turn['id'], self.store.snapshot('c1'))
        self.notify.assert_called_once()

    async def test_new_automatic_turn_read_before_live_started_keeps_completion_notification(self):
        self.factory = GoalFactory()
        await self.reopen(native=True)
        self.store.configure_completion_notifications(False)
        self.conversation()
        self.runtime.goals.store.put('c1', ever_native=True, continuing=False)
        self.factory.threads['thread-c1'] = {'id': 'thread-c1', 'status': {'type': 'idle'}, 'turns': []}
        await self.reopen(notifications=True, native=True)
        await self.service.startup()
        turn = {'id': 'new-live-auto', 'status': 'inProgress', 'items': []}
        self.factory.threads['thread-c1'].update(status={'type': 'active'}, turns=[copy.deepcopy(turn)])
        await self.runtime.reconcile('c1')
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 0)
        await self.runtime.handle_notification({'method': 'turn/started', 'params': {
            'threadId': 'thread-c1', 'turn': turn}})
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 1)
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {**turn, 'status': 'completed'}}})
        await self.service._deliver_completion_notification('c1', turn['id'], self.store.snapshot('c1'))
        self.notify.assert_called_once()

    async def test_later_native_message_proof_promotes_only_new_provisional_intent(self):
        self.factory = GoalFactory()
        await self.service.startup()
        await self.reopen(notifications=True, native=True)
        await self.service.startup()
        self.conversation()
        self.runtime.goals.store.put('c1', ever_native=True, continuing=False)
        self.store.begin_command('c1', 'new-uncertain', kind='start', text='new command')
        self.store.update_command('new-uncertain', 'delivery_unknown', reason='native_rpc_sending')
        native_turn = {'id': 'new-native', 'status': 'inProgress', 'items': []}
        self.factory.threads['thread-c1'] = {'id': 'thread-c1', 'status': {'type': 'active'},
            'turns': [native_turn]}
        await self.runtime.reconcile('c1')
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 0)
        native_turn['items'] = [{'id': 'user-input', 'type': 'userMessage',
            'clientUserMessageId': 'new-uncertain', 'content': [{'type': 'text', 'text': 'new command'}]}]
        await self.runtime.reconcile('c1')
        self.assertEqual(self.store.command('new-uncertain')['result_turn_id'], 'new-native')
        self.assertEqual(self.store.snapshot('c1')['turns'][0]['notification_requested'], 1)
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-c1', 'turn': {**native_turn, 'status': 'completed'}}})
        await self.service._deliver_completion_notification('c1', 'new-native', self.store.snapshot('c1'))
        self.notify.assert_called_once()

    async def test_proven_new_command_can_promote_provisional_recovery_intent_only(self):
        self.conversation()
        self.store.begin_command('c1', 'old-command', kind='start', text='before enable')
        self.store.configure_completion_notifications(False)
        self.store.configure_completion_notifications(True)
        self.store.begin_command('c1', 'new-command', kind='start', text='after enable')
        self.store.begin_command('c1', 'old-command', kind='start', text='before enable')
        self.store._db.execute("UPDATE commands SET created_at='2099-01-01T00:00:00Z' WHERE id='old-command'")
        self.store._db.execute("UPDATE commands SET created_at='1999-01-01T00:00:00Z' WHERE id='new-command'")
        native_turns = []
        for turn_id, command_id, expected in (('old-native', 'old-command', 0), ('new-native', 'new-command', 1)):
            with self.subTest(command_id=command_id):
                for previous in native_turns:
                    previous['status'] = 'completed'
                native_turns.append({'id': turn_id, 'status': 'inProgress', 'items': []})
                self.store.reconcile_thread('c1', {'thread': {'id': 'thread-c1', 'status': {'type': 'active'},
                    'turns': copy.deepcopy(native_turns)}})
                self.store.bind_turn('c1', {'id': turn_id, 'status': 'inProgress'}, command_id=command_id)
                turn = next(row for row in self.store.snapshot('c1')['turns'] if row['id'] == turn_id)
                self.assertEqual(turn['notification_requested'], expected)
        self.store._db.execute("UPDATE turns SET notification_requested=0,notification_detail='goal_continuing' WHERE id='new-native'")
        self.store.bind_turn('c1', {'id': 'new-native', 'status': 'inProgress'}, command_id='new-command')
        turn = next(row for row in self.store.snapshot('c1')['turns'] if row['id'] == 'new-native')
        self.assertEqual(turn['notification_requested'], 0)

    async def test_already_journaled_scheduled_command_survives_disabled_restart(self):
        await self.reopen(automations=True, native=True)
        self.store.create_conversation('c1', title='在途任务', cwd=str(self.root), owner_account_id='owner',
            settings={'permission': 'read-only', 'model': 'fixture', 'effort': 'medium'})
        await self.runtime.start_conversation('c1', command_id='initial', text='合成首轮')
        task = self.task(due=datetime.now(timezone.utc))
        await self.service.scheduler.tick()
        run = self.service.automations.runs(task['id'], 'owner')[0]
        self.assertEqual(run['status'], 'queued')
        await self.reopen(native=True)
        await self.service.startup()
        self.assertIsNone(self.service._scheduler)
        self.assertEqual(self.store.command(run['command_id'])['state'], 'queued')
        thread = self.factory.threads['thread-1']
        turn = thread['turns'][0]
        turn.update(status='completed', items=[])
        thread['status'] = {'type': 'idle'}
        await self.runtime.handle_notification({'method': 'turn/completed', 'params': {
            'threadId': 'thread-1', 'turn': copy.deepcopy(turn)}})
        await asyncio.sleep(.03)
        self.assertEqual(self.store.command(run['command_id'])['state'], 'accepted')
        self.assertEqual(len(thread['turns']), 2)
