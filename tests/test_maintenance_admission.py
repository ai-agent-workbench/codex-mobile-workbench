import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shared.maintenance_admission import MaintenanceAdmission, STEER_ONLY, busy_counts
from engine.codex_app_server import AppServerProtocolError
from tests.store_fixtures import seeded_store
import test_conversation_runtime as fixture
import test_conversation_goals as goal_fixture


LEGACY_CREATED = '2026-01-01T00:00:00+00:00'
LEGACY_COMPLETED = '2026-01-02T00:00:00+00:00'
PROCESS_BIRTH = '2026-02-01T00:00:00+00:00'


def seed_legacy_review(store, call_id='legacy-review'):
    """Synthetic pre-lifecycle projection; never copied from live sessions."""
    ordinal = store._db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM turns').fetchone()[0]
    store._db.execute("INSERT INTO turns(id,conversation_id,ordinal,status,completed_at) VALUES(?, 'conversation-1', ?, 'completed', ?)",
                      (call_id + '-turn', ordinal, LEGACY_COMPLETED))
    store._db.execute("""INSERT INTO tool_calls(call_id,conversation_id,turn_id,tool,arguments_sha256,state,created_at,updated_at)
                      VALUES(?, 'conversation-1', ?, 'code_review', ?, 'inProgress', ?, ?)""",
                      (call_id, call_id + '-turn', 'a' * 64, LEGACY_CREATED, LEGACY_CREATED))


class LegacyReviewProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = seeded_store(Path(self.tmp.name) / 'conversations.sqlite3')
        self.store.create_conversation('conversation-1', title='fixture', cwd=self.tmp.name, settings={})
        seed_legacy_review(self.store)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_known_history_is_not_live_but_no_process_proof_stays_conservative(self):
        for i in range(14):
            seed_legacy_review(self.store, 'legacy-' + str(i))
        # This permanent reference is kept even after actual review completion;
        # only its presence is NOT evidence of an unfinished lifecycle.
        owner = {'_review_ownership': {'review_thread_id': 'native-fixture',
                                     'review_started': True, 'interrupt_attempted': False}}
        for i in range(3):
            self.store._db.execute('UPDATE tool_calls SET response_json=? WHERE call_id=?',
                                   (json.dumps(owner), 'legacy-' + str(i)))
        before = list(self.store._db.execute('SELECT * FROM tool_calls ORDER BY call_id'))
        changes = self.store._db.total_changes
        self.assertEqual(15, busy_counts(self.store._db)['tool_calls'])
        self.assertEqual(0, busy_counts(self.store._db, web_started_at=PROCESS_BIRTH)['tool_calls'])
        self.assertEqual(before, list(self.store._db.execute('SELECT * FROM tool_calls ORDER BY call_id')))
        self.assertEqual(changes, self.store._db.total_changes)
        self.assertFalse(self.store._db.in_transaction)

    def test_uncertain_current_or_owned_reviews_remain_busy(self):
        cases = [
            ('current-update', {'updated_at': PROCESS_BIRTH}, {}),
            ('current-completion', {}, {'completed_at': PROCESS_BIRTH}),
            ('invalid-created', {'created_at': 'bad-date'}, {}),
            ('invalid-calendar', {'created_at': '2026-02-30T00:00:00+00:00'}, {}),
            ('missing-completion', {}, {'completed_at': None}),
            ('naive-time', {'updated_at': '2026-01-01T01:00:00'}, {}),
            ('inverted-time', {'updated_at': '2025-12-31T00:00:00+00:00'}, {}),
            ('parent-still-running', {}, {'status': 'inProgress'}),
            ('unknown-tool', {'tool': 'other_tool'}, {}),
            ('unknown-state', {'state': 'reserved'}, {}),
            ('malformed-response', {'response_json': '[]'}, {}),
            *[(name, {'response_json': json.dumps({name: value})}, {}) for name, value in (
                ('review_lifecycle', {'phase': 'turn_started'}),
                ('review_cleanup', {'status': 'unconfirmed'}),
                ('review_lifecycle', {}), ('review_cleanup', None),
                ('_review_ownership', {}), ('review_failure', {}),
                ('review_thread_id', 'native-fixture'), ('review_turn_id', 'native-fixture'))],
        ]
        for label, tool, turn in cases:
            with self.subTest(label=label):
                self.store._db.execute("UPDATE tool_calls SET tool='code_review',state='inProgress',created_at=?,updated_at=?,response_json=NULL",
                                       (LEGACY_CREATED, LEGACY_CREATED))
                self.store._db.execute("UPDATE turns SET status='completed',completed_at=?", (LEGACY_COMPLETED,))
                for table, values in (('tool_calls', tool), ('turns', turn)):
                    for field, value in values.items():
                        self.store._db.execute('UPDATE ' + table + ' SET ' + field + '=?', (value,))
                self.assertEqual(1, busy_counts(self.store._db, web_started_at=PROCESS_BIRTH)['tool_calls'])

    def test_orphan_and_mismatched_parent_do_not_prove_history(self):
        self.store._db.execute('PRAGMA foreign_keys=OFF')
        self.store._db.execute("UPDATE tool_calls SET turn_id='missing'")
        self.assertEqual(1, busy_counts(self.store._db, web_started_at=PROCESS_BIRTH)['tool_calls'])
        self.store._db.execute("UPDATE tool_calls SET turn_id='legacy-review-turn'")
        self.store._db.execute("UPDATE turns SET conversation_id='different'")
        self.assertEqual(1, busy_counts(self.store._db, web_started_at=PROCESS_BIRTH)['tool_calls'])

    def test_invalid_process_birth_cannot_prove_idle(self):
        for value in ('bad', '', '2026-01-01T00:00:00', '9999-01-01T00:00:00+00:00', 123):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, '进程起点不可验证'):
                busy_counts(self.store._db, web_started_at=value)

    def test_endpoint_filters_history_but_live_review_task_still_blocks(self):
        import os
        from types import SimpleNamespace
        from starlette.requests import Request
        from routes.maintenance_admission import status
        runtime = SimpleNamespace(_review_tasks={}, _background=set(), _notification_handlers=0,
                                  _client=None, _ensure_client=mock.AsyncMock(return_value=None))
        service = SimpleNamespace(store=self.store, runtime=runtime)
        self.store.maintenance_admission.change('quiescing', 'fixture-window')
        token = self.store.maintenance_admission.token_path.read_text()
        request = Request({'type': 'http', 'method': 'GET', 'path': '/internal/maintenance-admission',
                           'client': ('127.0.0.1', 123), 'headers': [(b'x-workbench-maintenance-token', token.encode())]})
        with mock.patch('routes.maintenance_admission.web_started_at', return_value=PROCESS_BIRTH) as birth, \
             mock.patch('routes.maintenance_admission.conversation_service', return_value=service), \
             mock.patch('routes.maintenance_admission.native_busy_counts', new=mock.AsyncMock(return_value={})):
            result = json.loads(asyncio.run(status(request)).body)
            self.assertEqual(0, result['busy']['tool_calls']); self.assertTrue(result['idle'])
            birth.assert_called_once_with(os.getpid())
            runtime._review_tasks['actual-review'] = SimpleNamespace(done=lambda: False)
            result = json.loads(asyncio.run(status(request)).body)
            self.assertEqual(0, result['busy']['tool_calls'])
            self.assertEqual(1, result['busy']['native_reviews']); self.assertFalse(result['idle'])

    def test_process_birth_cache_is_keyed_by_pid(self):
        from routes.maintenance_admission import web_started_at
        web_started_at.cache_clear()
        try:
            with mock.patch('shared.instance_primitives.process_identity', return_value='pinned') as identity, \
                 mock.patch('shared.instance_primitives.process_started_at', return_value=PROCESS_BIRTH) as birth:
                self.assertEqual(PROCESS_BIRTH, web_started_at(111))
                self.assertEqual(PROCESS_BIRTH, web_started_at(111))
                self.assertEqual(PROCESS_BIRTH, web_started_at(222))
                self.assertEqual(2, identity.call_count)
                self.assertEqual([mock.call({'pid': 111, 'starttime': 'pinned'}),
                                  mock.call({'pid': 222, 'starttime': 'pinned'})], birth.call_args_list)
        finally:
            web_started_at.cache_clear()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'conversations.sqlite3'
        self.store = seeded_store(self.path)
        self.store.create_conversation('conversation-1', title='fixture', cwd=self.tmp.name, settings={'model':'fixture','effort':'medium','permission':'danger-full-access'})
        self.gate = self.store.maintenance_admission

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_close_open_owner_and_corrupt_fail_closed(self):
        self.assertEqual('open', self.gate.read()['mode'])
        self.gate.change('quiescing', 'first-window')
        with self.assertRaises(RuntimeError):
            self.gate.change('open', 'unrelated-window')
        self.assertEqual('quiescing', self.gate.read()['mode'])
        self.gate.change('open', 'first-window')
        self.gate.path.write_text('{bad')
        self.assertEqual('unavailable', self.gate.read()['mode'])
        value, created = self.store.begin_command('conversation-1', 'closed', kind='start', text='fixture')
        self.assertFalse(created)
        self.assertEqual('rejected', value['state'])

    def test_rejected_new_work_does_not_become_queued_or_replay_on_open(self):
        self.gate.change('quiescing', 'window')
        for kind in ('start', 'compact', 'steer'):
            command, created = self.store.begin_command('conversation-1', 'id-' + kind,
                                                       kind=kind, text='synthetic')
            self.assertFalse(created)
            self.assertEqual('maintenance_quiescing', command['reason'])
        self.gate.change('open', 'window')
        repeated, created = self.store.begin_command('conversation-1', 'id-start', kind='start', text='synthetic')
        self.assertFalse(created)
        self.assertEqual('rejected', repeated['state'])
        for kind in ('interrupt', 'settings', 'goal_continue'):
            command, created = self.store.begin_command('conversation-1', 'control-' + kind, kind=kind)
            self.assertTrue(created)

    def test_close_waits_for_nested_registration_commit_not_database_lock(self):
        entered = threading.Event(); finished = threading.Event()
        def close():
            entered.set(); self.gate.change('quiescing', 'window'); finished.set()
        with self.store._lock, self.gate.admission():
            self.store._begin()
            self.store.begin_command('conversation-1', 'before', kind='start', text='fixture')
            thread = threading.Thread(target=close); thread.start(); entered.wait(2)
            self.assertFalse(finished.wait(.05))
            self.store._finish(None)
        thread.join(2); self.assertTrue(finished.is_set())
        # EX was released before any DB acquisition: shutdown may write freely.
        with self.store._lock:
            self.store._begin(); self.store._finish(None)
        self.assertEqual(1, busy_counts(self.store._db)['commands'])

    def test_caller_cannot_forge_steer_ticket(self):
        with self.assertRaises(AppServerProtocolError):
            self.store.begin_command('conversation-1', 'forged', kind='steer', payload={STEER_ONLY: False})


class RuntimeGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = fixture.ConversationRuntimeTests()
        await self.t.asyncSetUp()
        self.gate = self.t.store.maintenance_admission

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def test_inflight_native_completion_and_steer_are_not_interrupted(self):
        await self.t.start()
        self.gate.change('quiescing', 'window')
        calls = self.t.factory.clients[0].calls
        before_starts = sum(name == 'turn/start' for name, _ in calls)
        result = await self.t.runtime.send_message('conversation-1', command_id='steer-live', text='补充')
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual(before_starts, sum(name == 'turn/start' for name, _ in calls))
        result = await self.t.runtime.send_message('conversation-1', command_id='new-turn', text='新任务', force_new_turn=True)
        self.assertEqual('rejected', result['command']['state'])
        turn = copy.deepcopy(self.t.factory.threads['thread-1']['turns'][-1])
        turn['status'] = 'completed'
        await self.t.runtime.handle_notification({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': turn}})
        self.assertEqual('idle', self.t.store.get_conversation('conversation-1')['state'])
        self.assertFalse(any(name == 'turn/interrupt' for name, _ in calls))
        self.assertEqual(0, busy_counts(self.t.store._db)['commands'])

    async def test_steer_admitted_during_drain_cannot_turn_into_new_work(self):
        await self.t.start(); self.gate.change('quiescing', 'window')
        command, created = self.t.store.begin_command('conversation-1', 'race', kind='steer', text='补充')
        self.assertTrue(created)
        self.assertTrue(command['payload'][STEER_ONLY])
        self.t.store.set_conversation_state('conversation-1', 'idle', active_turn_id=None)
        result = await self.t.runtime._start_turn_locked('conversation-1', 'race', '补充', skill_selectors=None)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual(1, sum(name == 'turn/start' for name, _ in self.t.factory.clients[0].calls))
        # Retry input comparison ignores only the internal ticket.
        again, created = self.t.store.begin_command('conversation-1', 'race', kind='steer', text='补充')
        self.assertFalse(created); self.assertEqual('rejected', again['state'])

    async def test_new_thread_rejected_without_native_rpc(self):
        self.gate.change('quiescing', 'window')
        result = await self.t.start()
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual([], self.t.factory.clients)

    async def test_previously_admitted_command_drains_after_close(self):
        self.t.store.begin_command('conversation-1', 'first-command', kind='start', text='先前已接受', payload={'skills': []})
        self.gate.change('quiescing', 'window')
        # A repeated HTTP request must not redeliver the persisted command.
        result = await self.t.runtime.start_conversation('conversation-1', command_id='first-command', text='先前已接受')
        self.assertEqual('submitted', result['command']['state'])
        self.assertEqual([], self.t.factory.clients)


class GoalGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = goal_fixture.ConversationGoalTests(); await self.t.asyncSetUp()
        await self.t.start(); self.gate = self.t.store.maintenance_admission

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def test_new_goal_activation_rejected_but_pause_and_native_notifications_allowed(self):
        self.gate.change('quiescing', 'window')
        store = self.t.runtime.goals.store
        for i, payload in enumerate(({'action': 'resume'}, {'action': 'set', 'status': 'active'})):
            receipt, created = store.begin_command('conversation-1', 'goal-' + str(i), payload=payload)
            self.assertFalse(created); self.assertEqual('rejected', receipt['state'])
        receipt, created = store.begin_command('conversation-1', 'pause', payload={'action': 'pause'})
        self.assertTrue(created)
        await self.t.runtime.handle_notification({'method': 'thread/goal/updated', 'params': {
            'threadId': 'thread-1', 'goal': goal_fixture.native_goal()}})
        self.assertEqual(1, busy_counts(self.t.store._db)['native_goal_projection'])
        auto = {'id': 'native-auto', 'status': 'inProgress', 'items': []}
        await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': auto}})
        self.assertEqual('native-auto', self.t.store.get_conversation('conversation-1')['active_turn_id'])


class FirstGoalAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_form_draft_activates_when_first_turn_was_admitted_before_close(self):
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            t.runtime.goals.save_draft('conversation-1', '完成测试目标')
            original = t.runtime.goals.finish_first
            async def close_then_finish(cid, parent):
                t.store.maintenance_admission.change('quiescing', 'window')
                await original(cid, parent)
            with mock.patch.object(t.runtime.goals, 'finish_first', side_effect=close_then_finish):
                result = await t.start()
            self.assertEqual('accepted', result['command']['state'])
            self.assertEqual(['active'], [v['status'] for name, v in t.client.calls if name == 'goal/set'])
            self.assertIsNone(t.runtime.goals.store.entry('conversation-1')['draft'])
            self.assertEqual(1, busy_counts(t.store._db)['native_goal_projection'])
        finally:
            await t.asyncTearDown()

    async def test_rejected_form_with_active_draft_does_not_hold_drain(self):
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            t.store.maintenance_admission.change('quiescing', 'window')
            t.runtime.goals.save_draft('conversation-1', '完成测试目标')
            result = await t.start()
            self.assertEqual('rejected', result['command']['state'])
            self.assertFalse(t.factory.clients)
            self.assertEqual(0, busy_counts(t.store._db)['native_goal_projection'])
            self.assertIsNotNone(t.runtime.goals.store.entry('conversation-1')['draft'])
        finally:
            await t.asyncTearDown()


class AutomationGateTests(unittest.TestCase):
    def setUp(self):
        import test_conversation_automation as automation_fixture
        self.t = automation_fixture.AutomationStoreTests(); self.t.setUp()
        self.gate = self.t.store.maintenance_admission

    def tearDown(self):
        self.t.tearDown()

    def test_closed_scheduler_preserves_tasks_without_claim_or_replay(self):
        task = self.t.create()
        due = self.t.make_due(task['id'])
        before = self.t.automations.get(task['id'], 'owner-1')
        self.gate.change('quiescing', 'window')
        self.assertEqual([], self.t.automations.claim_due(due))
        self.assertEqual(before, self.t.automations.get(task['id'], 'owner-1'))
        self.assertEqual([], self.t.automations.runs(task['id'], 'owner-1'))
        self.gate.change('open', 'window')
        # Existing missed-maintenance policy still skips an overdue slot.
        from datetime import timedelta
        self.assertEqual([], self.t.automations.claim_due(due + timedelta(minutes=3)))
        self.assertEqual('missed_maintenance', self.t.automations.runs(task['id'], 'owner-1')[0]['reason'])

    def test_already_claimed_run_drains_after_close(self):
        task = self.t.create(); due = self.t.make_due(task['id'])
        _, run = self.t.automations.claim_due(due)[0]
        self.gate.change('quiescing', 'window')
        self.assertEqual(1, busy_counts(self.t.store._db)['conversation_automation_runs'])
        command, created = self.t.store.begin_command('conversation-1', run['command_id'],
            kind='steer', text='预先接受的定时任务', origin='scheduled')
        self.assertTrue(created); self.assertNotIn(STEER_ONLY, command['payload'])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        AutomationGateTests.setUp(self)
        from engine.conversation_goals import GoalStore
        self.goals = GoalStore(self.t.store)
        self.connection = self.t.store._db

    def tearDown(self):
        self.t.tearDown()

    def test_atomic_legacy_barrier_keeps_existing_rows_and_allows_completion(self):
        from shared.maintenance_bootstrap import change, definitions
        import sqlite3
        task = self.t.create(); due = self.t.make_due(task['id'])
        _, run = self.t.automations.claim_due(due)[0]
        self.t.store.begin_command('conversation-1', 'existing', kind='start', text='先前接受')
        tasks_before = self.t.automations.get(task['id'], 'owner-1')
        self.assertEqual(definitions('window'), change(self.connection, 'window', install=True))
        self.assertFalse(self.connection.in_transaction)
        for kind in ('start', 'steer', 'compact'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.t.store.begin_command('conversation-1', 'late-' + kind, kind=kind, text='新任务')
        self.t.store.update_command('existing', 'completed')
        _, created = self.t.store.begin_command('conversation-1', run['command_id'],
                    kind='steer', text='先前已接受的定时任务', origin='scheduled')
        self.assertTrue(created)
        self.goals.begin_command('conversation-1', 'prior-goal', payload={'action':'set','status':'active'}, origin='system')
        with self.assertRaises(sqlite3.IntegrityError):
            self.goals.begin_command('conversation-1', 'new-goal', payload={'action':'resume'})
        self.goals.begin_command('conversation-1', 'pause-goal', payload={'action':'pause'})
        self.assertEqual(tasks_before, self.t.automations.get(task['id'], 'owner-1'))
        self.assertEqual({}, change(self.connection, 'window', install=False))
        self.assertFalse(self.connection.in_transaction)
        self.t.store.begin_command('conversation-1', 'after', kind='start', text='恢复后')

    def test_old_scheduler_transaction_rolls_back_without_pausing_or_failing_tasks(self):
        from shared.maintenance_bootstrap import change
        import sqlite3
        task = self.t.create(); due = self.t.make_due(task['id'])
        before = self.t.automations.get(task['id'], 'owner-1')
        change(self.connection, 'window', install=True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.t.automations.claim_due(due)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(before, self.t.automations.get(task['id'], 'owner-1'))
        self.assertEqual([], self.t.automations.runs(task['id'], 'owner-1'))

    def test_owner_or_definition_drift_does_not_overwrite(self):
        from shared.maintenance_bootstrap import change, inspect
        change(self.connection, 'window', install=True)
        before = inspect(self.connection)
        with self.assertRaises(RuntimeError):
            change(self.connection, 'other-window', install=False)
        self.assertEqual(before, inspect(self.connection))


class LocalStatusTests(unittest.TestCase):
    def test_control_token_required_even_for_proxy_loopback(self):
        from starlette.requests import Request
        from routes.maintenance_admission import status
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            store = seeded_store(Path(root) / 'conversations.sqlite3')
            try:
                runtime = SimpleNamespace(_review_tasks={}, _background=set(), _notification_handlers=0, _client=None, _ensure_client=mock.AsyncMock(return_value=None))
                service = SimpleNamespace(store=store, runtime=runtime)
                def request(peer='127.0.0.1', token=None):
                    return Request({'type':'http', 'method':'GET', 'path':'/internal/maintenance-admission',
                        'client':(peer,123), 'headers': [] if token is None else [(b'x-workbench-maintenance-token', token.encode())]})
                with mock.patch('routes.maintenance_admission.conversation_service', return_value=service), \
                     mock.patch('routes.maintenance_admission.native_busy_counts', new=mock.AsyncMock(return_value={})):
                    self.assertEqual(403, asyncio.run(status(request())).status_code)
                    store.maintenance_admission.change('quiescing', 'window')
                    token = store.maintenance_admission.token_path.read_text()
                    self.assertEqual(403, asyncio.run(status(request(token='wrong'))).status_code)
                    self.assertEqual(403, asyncio.run(status(request('192.0.2.1', token))).status_code)
                    import json
                    result = json.loads(asyncio.run(status(request(token=token))).body)
                    self.assertTrue(result['idle'])
                    self.assertEqual('quiescing', result['state']['mode'])
                    self.assertNotIn(token, json.dumps(result))
            finally:
                store.close()


class GracefulServerTests(unittest.TestCase):
    def test_real_uvicorn_sigterm_exits_after_sqlite_close_without_writer_deadlock(self):
        import json
        import os
        import signal
        import socket
        import subprocess
        import sys
        import urllib.request
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'conversations.sqlite3'
            store = seeded_store(path)
            gate = store.maintenance_admission
            gate.change('quiescing', 'fixture-window')
            token = gate.token_path.read_text()
            store.close()
            listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
            env = {**os.environ, 'FIXTURE_ROOT': str(root),
                   'CODEX_WEB_STATE_DIR': str(root / 'unused-state'),
                   'FIXTURE_SOCKET_FD': str(listener.fileno())}
            with (root / 'server.log').open('w') as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'fixtures/maintenance_admission_server.py')],
                    env=env, stdout=log, stderr=log, pass_fds=(listener.fileno(),))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                request = urllib.request.Request('http://127.0.0.1:' + str(listener.getsockname()[1]) + '/internal/maintenance-admission',
                    headers={'X-Workbench-Maintenance-Token': token})
                try:
                    end = time.monotonic() + 10
                    result = None
                    while time.monotonic() < end:
                        if process.poll() is not None:
                            self.fail('隔离 ASGI 进程启动失败：' + (root / 'server.log').read_text())
                        try:
                            with opener.open(request, timeout=.25) as response:
                                result = json.load(response)
                            break
                        except OSError:
                            time.sleep(.05)
                    self.assertIsNotNone(result)
                    self.assertTrue(result['idle'])
                    self.assertEqual('quiescing', result['state']['mode'])
                    from shared.maintenance_admission import attest
                    proof = attest(gate, 'fixture-window', gate.identity()['module_sha256'], base_url='http://127.0.0.1:' + str(listener.getsockname()[1]))
                    self.assertTrue(proof['idle'])
                    # This is the actual native signal/graceful lifespan, not a
                    # mocked systemctl success or forced production shutdown.
                    process.send_signal(signal.SIGTERM)
                    # Uvicorn re-raises the captured signal AFTER lifespan
                    # shutdown; -SIGTERM is its documented native completion.
                    self.assertIn(process.wait(timeout=10), (0, -signal.SIGTERM))
                    self.assertEqual('graceful', (root / 'shutdown-finished').read_text())
                    self.assertEqual('quiescing', gate.read()['mode'])
                finally:
                    listener.close()
                    if process.poll() is None:
                        process.kill(); process.wait()


class BootstrapDrainTests(unittest.TestCase):
    def setUp(self):
        patcher=mock.patch('shared.maintenance_native.standalone_counts',return_value={})
        patcher.start(); self.addCleanup(patcher.stop)
        from engine.conversation_goals import GoalStore
        from engine.conversation_automation import AutomationStore
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'conversations.sqlite3'
        self.store = seeded_store(self.path)
        self.store.create_conversation('conversation-1', title='fixture', cwd=self.tmp.name, settings={'permission':'danger-full-access'})
        GoalStore(self.store); AutomationStore(self.store)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_existing_turn_drains_and_new_registrations_fail_without_shutdown_lock(self):
        import sqlite3
        from shared.maintenance_bootstrap import quiesce, restore_legacy_admission
        self.store.begin_command('conversation-1', 'active-command', kind='start', text='already admitted')
        finished = threading.Event()
        def complete():
            # Wait for the real guard, then finish through a DIFFERENT SQLite
            # connection, proving no writer is held while drain waits.
            gate_path = self.store.maintenance_admission.path
            until = time.monotonic() + 5
            while not gate_path.exists() and time.monotonic() < until:
                time.sleep(.01)
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE commands SET state='completed' WHERE id='active-command'")
            finished.set()
        thread = threading.Thread(target=complete); thread.start()
        try:
            gate = quiesce(self.path.parent, 'window', web_started_at='2026-01-01T00:00:00+00:00', timeout=5)
            self.assertTrue(finished.wait(2))
            self.assertEqual('quiescing', gate.read()['mode'])
            # Original idle legacy selection restoration is a caller contract;
            # only its exact owner can remove the guard.
            restore_legacy_admission(self.path.parent, 'window')
            self.assertEqual('open', gate.read()['mode'])
        finally:
            thread.join(5)

    def test_native_read_does_not_make_later_local_callback_registration_invisible(self):
        from shared.maintenance_bootstrap import quiesce
        def native_scan():
            self.store.begin_command('conversation-1','late-control',kind='interrupt')
            return {'native_active_threads':0}
        with mock.patch('shared.maintenance_native.standalone_counts',side_effect=native_scan):
            with self.assertRaisesRegex(RuntimeError,'未自然结束.*commands=1'):
                quiesce(self.path.parent,'window',web_started_at='2026-01-01T00:00:00+00:00',timeout=0)
        self.assertEqual('quiescing',self.store.maintenance_admission.read()['mode'])

    def test_legacy_review_history_allows_drain_without_rewriting_the_projection(self):
        from shared.maintenance_bootstrap import quiesce
        seed_legacy_review(self.store)
        with mock.patch('shared.maintenance_native.standalone_counts', return_value={'native_active_threads': 0}) as scan:
            gate = quiesce(self.path.parent, 'window', web_started_at=PROCESS_BIRTH, timeout=0)
        scan.assert_called_once()
        self.assertEqual('quiescing', gate.read()['mode'])
        self.assertEqual('inProgress', self.store._db.execute('SELECT state FROM tool_calls').fetchone()[0])

    def test_active_native_review_still_blocks_even_when_projection_is_historical(self):
        from shared.maintenance_bootstrap import quiesce
        seed_legacy_review(self.store)
        with mock.patch('shared.maintenance_native.standalone_counts', return_value={'native_active_threads': 1}) as scan:
            with self.assertRaisesRegex(RuntimeError, '未自然结束.*native_active_threads=1'):
                quiesce(self.path.parent, 'window', web_started_at=PROCESS_BIRTH, timeout=0)
        scan.assert_called_once()
        self.assertEqual('quiescing', self.store.maintenance_admission.read()['mode'])



class MaintenanceLifecycleTests(unittest.TestCase):
    def test_accepted_attestation_reopens_only_its_owned_gate(self):
        from shared.maintenance_bootstrap import reopen_after_acceptance
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            store = seeded_store(state / 'conversations.sqlite3')
            try:
                store.maintenance_admission.change('quiescing', 'window-owner')
                with mock.patch('shared.maintenance_admission.attest', return_value={'idle': False}) as attest:
                    result = reopen_after_acceptance(state, 'window-owner', 'a' * 64)
                self.assertEqual('open', result['mode'])
                self.assertEqual('open', store.maintenance_admission.read()['mode'])
                self.assertEqual({'require_idle': False}, attest.call_args.kwargs)
                store.maintenance_admission.change('quiescing', 'another-owner')
                with mock.patch('shared.maintenance_admission.attest', return_value={'idle': True}):
                    with self.assertRaises(RuntimeError):
                        reopen_after_acceptance(state, 'window-owner', 'a' * 64)
                self.assertEqual('another-owner', store.maintenance_admission.read()['owner'])
            finally:
                store.close()

    def test_attestation_waits_for_http_readiness_before_removing_guard(self):
        from shared.maintenance_bootstrap import remove_after_attestation
        events = []
        with tempfile.TemporaryDirectory() as root, \
             mock.patch('shared.maintenance_admission.attest', side_effect=[ConnectionRefusedError(), {'idle': True}]), \
             mock.patch('shared.maintenance_bootstrap.change', side_effect=lambda *args, **kw: events.append('remove')), \
             mock.patch('sqlite3.connect'), mock.patch('time.sleep') as sleep:
            self.assertEqual({'idle': True}, remove_after_attestation(Path(root), 'window', 'a' * 64))
            sleep.assert_called_once_with(.25)
            self.assertEqual(['remove'], events)


class LegacyTerminalBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher=mock.patch('shared.maintenance_native.standalone_counts',return_value={})
        patcher.start(); self.addCleanup(patcher.stop)

    async def test_real_terminal_handler_cannot_be_cancelled_before_channel_claim(self):
        from shared.maintenance_bootstrap import quiesce
        from routes.ws import manager
        from engine.conversation_automation import AutomationStore
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        AutomationStore(t.store)
        blocked = asyncio.Event(); release = asyncio.Event()
        async def broadcast(*args, **kwargs):
            blocked.set(); await release.wait()
        try:
            t.store.set_completion_notification('conversation-1', True)
            await t.start(); await t.complete_first()
            turn = t.store.snapshot('conversation-1')['turns'][0]
            snapshot = t.store.snapshot('conversation-1')
            with mock.patch.object(manager, 'broadcast', side_effect=broadcast), \
                 mock.patch('shared.security.notify_task_completion', return_value=(True,'fixture')), \
                 mock.patch.object(t.service, '_web_push_owner', return_value='fixture-admin'):
                handler = asyncio.create_task(t.service.handle_terminal('conversation-1', turn['id'], snapshot))
                await asyncio.wait_for(blocked.wait(), 2)
                self.assertFalse(any(busy_counts(t.store._db).values()))
                drain = asyncio.create_task(asyncio.to_thread(quiesce, t.store.path.parent, 'window', web_started_at='2026-01-01T00:00:00+00:00', timeout=5))
                try:
                    await asyncio.sleep(.12)
                    self.assertFalse(drain.done(), 'unclaimed terminal delivery must hold the legacy drain')
                    release.set(); await handler
                    await asyncio.wait_for(drain, 6)
                    row = t.store._db.execute('SELECT notification_state FROM turns WHERE id=?', (turn['id'],)).fetchone()
                    self.assertEqual('sent', row[0])
                    self.assertTrue(t.store._db.execute('SELECT 1 FROM web_push_dispatches WHERE turn_id=?', (turn['id'],)).fetchone())
                finally:
                    release.set(); await handler
                    if not drain.done():
                        await drain
        finally:
            await t.asyncTearDown()

    async def test_fixed_boundary_does_not_expire_pending_callback_or_count_unjournalled_history(self):
        from shared.maintenance_bootstrap import legacy_terminal_counts
        from datetime import datetime, timezone, timedelta
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            t.store.set_completion_notification('conversation-1', True)
            await t.start(); await t.complete_first()
            tid = t.store.snapshot('conversation-1')['turns'][0]['id']
            fixed = datetime.now(timezone.utc).isoformat()
            self.assertEqual(1, legacy_terminal_counts(t.store._db, fixed)['legacy_terminal_delivery'])
            # Advancing wall-clock does NOT change this fixed boundary.
            with mock.patch('time.time', return_value=time.time()+3600):
                self.assertEqual(1, legacy_terminal_counts(t.store._db, fixed)['legacy_terminal_delivery'])
            old = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
            t.store._db.execute('UPDATE turns SET completed_at=?,delete_deferred_terminal=0 WHERE id=?', (old,tid))
            self.assertEqual(0, legacy_terminal_counts(t.store._db, fixed)['legacy_terminal_delivery'])
            # A genuine old live Push journal is uncertain, not history to skip.
            t.store._db.execute('UPDATE turns SET delete_deferred_terminal=1 WHERE id=?', (tid,))
            self.assertEqual(1, legacy_terminal_counts(t.store._db, fixed)['legacy_terminal_delivery'])
        finally:
            await t.asyncTearDown()


class NotificationIngressGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher=mock.patch('routes.maintenance_admission.native_busy_counts',new=mock.AsyncMock(return_value={}))
        patcher.start(); self.addCleanup(patcher.stop)

    async def test_real_endpoint_counts_terminal_ingress_during_image_archival(self):
        import json
        from types import SimpleNamespace
        from routes.maintenance_admission import status
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        entered = threading.Event(); release = threading.Event()
        original = t.artifacts.publish
        def publish(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture timed out')
            return original(*args, **kwargs)
        try:
            await t.start(); await asyncio.sleep(.02)
            gate = t.store.maintenance_admission; gate.change('quiescing','window')
            source = t.root / 'generated.png'; source.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            turn = copy.deepcopy(t.factory.threads['thread-1']['turns'][0])
            turn.update(status='completed', items=[{'id':'image-1','type':'imageGeneration',
                'status':'completed','savedPath':str(source),'result':'ok'}])
            with mock.patch.object(t.artifacts, 'publish', side_effect=publish), \
                 mock.patch('routes.maintenance_admission.conversation_service', return_value=t.service):
                incoming = asyncio.create_task(t.runtime.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':turn}}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual('idle', t.store.get_conversation('conversation-1')['state'])
                    self.assertFalse(any(busy_counts(t.store._db).values()))
                    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),
                        headers={'x-workbench-maintenance-token':gate._read_token()})
                    actual = json.loads((await status(request)).body)
                    self.assertFalse(actual['idle'])
                    self.assertEqual(1, actual['busy']['notification_handlers'])
                finally:
                    release.set(); await incoming
            self.assertEqual(0, t.runtime._notification_handlers)
            self.assertIsNotNone(t.store.artifact_for_key('conversation-1','image:image-1'))
        finally:
            release.set(); await t.asyncTearDown()


class AdmissionRaceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_thread_goal_seed_keeps_prior_admission_when_lock_wait_closes_gate(self):
        from contextlib import asynccontextmanager
        from engine.conversation_actions import ConversationActions
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            await t.start(); await t.complete_first()
            original = t.runtime._command_lock
            @asynccontextmanager
            async def close_on_lock(cid, command_id):
                if command_id == 'admitted-goal':
                    t.store.maintenance_admission.change('quiescing', 'window')
                async with original(cid, command_id):
                    yield
            with mock.patch.object(t.runtime, '_command_lock', side_effect=close_on_lock):
                result = await ConversationActions(t.runtime).start_goal('conversation-1',
                    command_id='admitted-goal', objective='完成测试目标', skills=['preserved-skill'])
            self.assertEqual('accepted', result['command']['state'])
            self.assertEqual(['active'], [v['status'] for name,v in t.client.calls if name=='goal/set'])
        finally:
            await t.asyncTearDown()

    async def test_nonsteerable_maintenance_ticket_is_rejected_never_replayed_by_native_goal(self):
        from engine.codex_app_server import AppServerRPCError
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            await t.start(); await t.change()
            t.store.maintenance_admission.change('quiescing','window')
            t.client.steer_error = AppServerRPCError(-32602,'activeTurnNotSteerable')
            result = await t.runtime.send_message('conversation-1',command_id='ticket',text='本轮补充')
            self.assertEqual('rejected', result['command']['state'])
            self.assertEqual([], t.store.queued_commands('conversation-1'))
            t.client.steer_error = None
            auto = {'id':'auto-turn','status':'inProgress','items':[]}
            await t.runtime.handle_notification({'method':'turn/started','params':{'threadId':'thread-1','turn':auto}})
            await t.runtime.goals.deliver_queue('conversation-1','auto-turn')
            self.assertEqual(1, sum(name=='turn/steer' for name,_ in t.client.calls))
            self.assertEqual('rejected', t.store.command('ticket')['state'])
        finally:
            await t.asyncTearDown()

    async def test_ticket_cannot_steer_a_different_active_turn_even_if_original_turn_finished_before_lock(self):
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            await t.start(); t.store.maintenance_admission.change('quiescing','window')
            command, created = t.store.begin_command('conversation-1','ticket',kind='steer',text='本轮补充')
            self.assertEqual('turn-1', command['payload'][STEER_ONLY]); self.assertTrue(created)
            t.store.set_conversation_state('conversation-1','active',active_turn_id='different-turn')
            result = await t.runtime._steer_locked('conversation-1','ticket','本轮补充',skill_selectors=None)
            self.assertEqual('rejected',result['command']['state'])
            self.assertFalse(any(name=='turn/steer' for name,_ in t.client.calls))
        finally:
            await t.asyncTearDown()


class LoadedImplementationFingerprintTests(unittest.TestCase):
    def test_same_helper_sha_with_old_loaded_ingress_cannot_attest_current_candidate(self):
        import json
        from shared.maintenance_admission import attest
        with tempfile.TemporaryDirectory() as root:
            gate = MaintenanceAdmission(Path(root)/'conversations.sqlite3')
            gate.change('quiescing','window')
            proof = {**gate.identity(), 'implementation_sha256':'0'*64,
                'database':str(gate.database_path.resolve()),
                'state':gate.read(),'idle':True,'busy':{}}
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(proof).encode()
            with mock.patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=response
                with self.assertRaises(RuntimeError):
                    attest(gate,'window',gate.identity()['module_sha256'])


class LegacyMediaBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unarchived_this_process_image_holds_legacy_but_older_history_does_not(self):
        from shared.maintenance_bootstrap import legacy_terminal_counts
        from datetime import datetime, timezone, timedelta
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            await t.start()
            turn = copy.deepcopy(t.factory.threads['thread-1']['turns'][0])
            turn.update(status='completed',items=[{'id':'image-fixture','type':'imageGeneration',
                'status':'completed','savedPath':str(t.root/'fixture.png')}])
            # Real projection, deliberately before runtime ingestion.
            t.store.apply_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':turn}})
            now = datetime.now(timezone.utc)
            result = legacy_terminal_counts(t.store._db,now.isoformat(),(now-timedelta(minutes=10)).isoformat())
            self.assertEqual(1,result['legacy_image_archival'])
            t.store._db.execute("UPDATE items SET updated_at=? WHERE native_item_id='image-fixture'", ((now-timedelta(days=30)).isoformat(),))
            result = legacy_terminal_counts(t.store._db,now.isoformat(),(now-timedelta(minutes=10)).isoformat())
            self.assertEqual(0,result['legacy_image_archival'])
        finally:
            await t.asyncTearDown()


class ReopenedGateProofTests(unittest.TestCase):
    def test_already_open_owned_gate_can_be_verified_with_live_new_tasks_without_reclosing(self):
        import json
        from shared.maintenance_admission import attest
        with tempfile.TemporaryDirectory() as root:
            gate = MaintenanceAdmission(Path(root)/'conversations.sqlite3')
            gate.change('quiescing','window'); gate.change('open','window')
            proof = {**gate.identity(),'database':str(gate.database_path.resolve()),
                'state':gate.read(),'idle':False,'busy':{'conversations':1}}
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value=json.dumps(proof).encode()
            with mock.patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=response
                self.assertEqual(proof,attest(gate,'window',gate.identity()['module_sha256'],
                    expected_mode='open',require_idle=False))
                with self.assertRaises(RuntimeError):
                    attest(gate,'other',gate.identity()['module_sha256'],expected_mode='open',require_idle=False)
                with self.assertRaises(RuntimeError):
                    attest(gate,'window',gate.identity()['module_sha256'])
            self.assertEqual('open',gate.read()['mode'])


class ActualPushJournalDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_real_push_is_busy_without_wire_marker_or_recent_terminal_time(self):
        from datetime import datetime,timezone,timedelta
        from shared.maintenance_bootstrap import legacy_terminal_counts
        t = goal_fixture.ConversationGoalTests(); await t.asyncSetUp()
        try:
            t.store.set_completion_notification('conversation-1',True)
            await t.start(); await t.complete_first()
            tid=t.store.snapshot('conversation-1')['turns'][0]['id']
            t.store.upsert_web_push_subscription(account_id='fixture-admin', endpoint='https://push.example.invalid/fixture',
                origin='https://fixture.example.invalid',p256dh='fixture-key',auth='fixture-auth')
            deliveries=t.store.prepare_web_push_deliveries('conversation-1',tid,'fixture-admin')
            self.assertEqual(1,len(deliveries))
            now=datetime.now(timezone.utc)
            t.store._db.execute('UPDATE turns SET completed_at=?,delete_deferred_terminal=0 WHERE id=?', ((now-timedelta(days=30)).isoformat(),tid))
            merged=busy_counts(t.store._db)
            merged.update(legacy_terminal_counts(t.store._db,now.isoformat()))
            self.assertEqual(1,merged['web_push_deliveries'])
            self.assertTrue(any(merged.values()))
        finally:
            await t.asyncTearDown()

    def test_status_samples_in_event_loop_without_await_handoff(self):
        import ast,inspect
        from routes.maintenance_admission import status, local_busy_counts
        self.assertTrue(inspect.iscoroutinefunction(status))
        self.assertFalse(inspect.iscoroutinefunction(local_busy_counts))
        self.assertFalse(any(isinstance(node,ast.Await) for node in ast.walk(ast.parse(inspect.getsource(local_busy_counts)))))

class NativeCallbackDrainRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_turn_native_request_stays_busy_after_queue_handoff_until_response_sent(self):
        import json
        from types import SimpleNamespace
        from starlette.requests import Request
        from engine.codex_app_server import CodexAppServerClient
        from routes.maintenance_admission import status
        with tempfile.TemporaryDirectory() as root:
            store = seeded_store(Path(root) / 'conversations.sqlite3')
            registered = asyncio.Event(); release_handler = asyncio.Event()
            sending = asyncio.Event(); release_send = asyncio.Event()
            async def handler(method, params, **kwargs):
                registered.set(); await release_handler.wait(); return {'action':'decline'}
            async def enqueue(response):
                sending.set(); await release_send.wait()
            client = CodexAppServerClient('/tmp/unused.sock', server_request_handler=handler,
                                          notification_handler=lambda message:None)
            client._connected=True; client._connection_epoch=1
            client._enqueue = enqueue
            client.list_loaded_thread_ids=mock.AsyncMock(return_value=[])
            client._notification_task=asyncio.create_task(client._notification_worker(1))
            runtime = SimpleNamespace(_review_tasks={}, _background=set(), _notification_handlers=0, _client=client, _ensure_client=mock.AsyncMock(return_value=client))
            service = SimpleNamespace(store=store, runtime=runtime)
            store.maintenance_admission.change('quiescing','window')
            token=store.maintenance_admission.token_path.read_text()
            request=Request({'type':'http','method':'GET','path':'/internal/maintenance-admission',
                'client':('127.0.0.1',123), 'headers':[(b'x-workbench-maintenance-token',token.encode())]})
            try:
                with mock.patch('routes.maintenance_admission.conversation_service',return_value=service):
                    await client._handle_raw(json.dumps({'id':1,'method':'mcpServer/elicitation/request',
                        'params':{'turnId':None,'threadId':'fixture'}}),1)
                    await asyncio.wait_for(registered.wait(),1)
                    self.assertEqual(0,client._notifications.qsize())
                    result=json.loads((await status(request)).body)
                    self.assertFalse(result['idle']);self.assertEqual(1,result['busy']['native_request_callbacks'])
                    release_handler.set();await asyncio.wait_for(sending.wait(),1)
                    self.assertFalse(json.loads((await status(request)).body)['idle'])
                    release_send.set();await asyncio.gather(*tuple(client._callback_tasks))
                    self.assertTrue(json.loads((await status(request)).body)['idle'])
            finally:
                await client.close();store.close()

    async def test_accepted_unconfirmed_native_tree_stop_blocks_both_new_and_legacy_idle_proof(self):
        import datetime
        from shared.maintenance_bootstrap import legacy_terminal_counts
        t=fixture.ConversationRuntimeTests();await t.asyncSetUp()
        try:
            t.store.begin_command('conversation-1','stop',kind='interrupt',payload={'scope':'conversation_tree'})
            t.runtime._record_tree_stop('conversation-1','stop',{'status':'unconfirmed','active_threads':1})
            self.assertEqual('accepted',t.store.command('stop')['state'])
            self.assertIsNone(t.store.get_conversation('conversation-1')['active_turn_id'])
            counts=busy_counts(t.store._db)
            self.assertEqual(1,counts['commands'])
            counts.update(legacy_terminal_counts(t.store._db,datetime.datetime.now(datetime.timezone.utc).isoformat()))
            self.assertTrue(any(counts.values()))
            t.runtime._record_tree_stop('conversation-1','stop',{'status':'verified','root_turn_ids':[]})
            self.assertEqual(0,busy_counts(t.store._db)['commands'])
        finally:
            await t.asyncTearDown()

class NativeLoadedInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_loaded_inventory_pagination_and_bad_responses(self):
        from engine.codex_app_server import CodexAppServerClient
        client=CodexAppServerClient('/tmp/unused.sock')
        client.request=mock.AsyncMock(side_effect=[{'data':['root'],'nextCursor':'page2'}, {'data':['agent'],'nextCursor':None}])
        self.assertEqual(['agent','root'],await client.list_loaded_thread_ids())
        self.assertEqual('page2',client.request.await_args_list[1].args[1]['cursor'])
        for pages in ([{'data':['root'],'nextCursor':'again'},{'data':['root'],'nextCursor':None}],
                      [{'data':['root'],'nextCursor':'again'},{'data':['agent'],'nextCursor':'again'}],
                      [{'data':'not-an-id-list'}]):
            client.request=mock.AsyncMock(side_effect=pages)
            with self.assertRaises(AppServerProtocolError):
                await client.list_loaded_thread_ids()

    async def test_native_agent_and_goal_are_busy_without_local_turn_or_stop_command(self):
        from types import SimpleNamespace
        from shared.maintenance_native import busy_counts as native_counts
        states={'root':'idle','child':'active','unloaded':'notLoaded'}
        async def read(tid,include_turns):
            self.assertFalse(include_turns)
            return {'thread':{'id':tid,'status':{'type':states[tid]}}}
        async def goal(tid):
            self.assertNotEqual('unloaded',tid)
            return {'status':'active'} if tid=='root' else None
        client=SimpleNamespace(list_loaded_thread_ids=mock.AsyncMock(return_value=list(states)),
            read_thread=read,get_thread_goal=goal,
            list_background_terminals=mock.AsyncMock(return_value=[]))
        counts=await native_counts(client)
        self.assertEqual(1,counts['native_active_threads']);self.assertEqual(1,counts['native_active_goals'])
        states['child']='idle'; client.get_thread_goal=mock.AsyncMock(return_value=None)
        self.assertFalse(any((await native_counts(client)).values()))
        client.list_loaded_thread_ids=mock.AsyncMock(side_effect=[['root'],['root','late-child']])
        self.assertEqual(1,(await native_counts(client))['native_inventory_unstable'])
        states['root']='systemError';client.list_loaded_thread_ids=mock.AsyncMock(return_value=['root'])
        self.assertEqual(1,(await native_counts(client))['native_runtime_unknown'])

    async def test_idle_thread_with_background_terminal_is_busy_and_inventory_failure_cannot_prove_idle(self):
        from types import SimpleNamespace
        from shared.maintenance_native import busy_counts as native_counts
        client = SimpleNamespace(
            list_loaded_thread_ids=mock.AsyncMock(return_value=['idle-child']),
            read_thread=mock.AsyncMock(return_value={'thread': {'id': 'idle-child', 'status': {'type': 'idle'}}}),
            get_thread_goal=mock.AsyncMock(return_value=None),
            list_background_terminals=mock.AsyncMock(return_value=[{'processId': 'still-running'}]))
        counts = await native_counts(client)
        self.assertEqual(0, counts['native_active_threads']); self.assertEqual(0, counts['native_active_goals'])
        self.assertEqual(1, counts['native_background_terminals'])
        client.list_background_terminals.assert_awaited_once_with('idle-child')
        client.list_background_terminals = mock.AsyncMock(side_effect=AppServerProtocolError('unavailable'))
        with self.assertRaises(AppServerProtocolError):
            await native_counts(client)

    async def test_native_idle_scan_resamples_local_callbacks_after_await(self):
        import json
        from types import SimpleNamespace
        from starlette.requests import Request
        from routes.maintenance_admission import status
        with tempfile.TemporaryDirectory() as root:
            store=seeded_store(Path(root)/'conversations.sqlite3')
            store.maintenance_admission.change('quiescing','window')
            token=store.maintenance_admission.token_path.read_text()
            request=Request({'type':'http','method':'GET','path':'/internal/maintenance-admission',
                'client':('127.0.0.1',123),'headers':[(b'x-workbench-maintenance-token',token.encode())]})
            callback=asyncio.create_task(asyncio.Event().wait())
            client=SimpleNamespace(_notifications=asyncio.Queue(),_callback_tasks=set())
            runtime=SimpleNamespace(_review_tasks={},_background=set(),_notification_handlers=0,_client=client,
                                    _ensure_client=mock.AsyncMock(return_value=client))
            service=SimpleNamespace(store=store,runtime=runtime)
            async def scan(c):
                await asyncio.sleep(0)
                client._callback_tasks.add(callback)
                return {'native_active_threads':0}
            try:
                with mock.patch('routes.maintenance_admission.conversation_service',return_value=service), \
                     mock.patch('routes.maintenance_admission.native_busy_counts',side_effect=scan):
                    proof=json.loads((await status(request)).body)
                    self.assertFalse(proof['idle']);self.assertEqual(1,proof['busy']['native_request_callbacks'])
                runtime._ensure_client=mock.AsyncMock(side_effect=RuntimeError('unsupported native runtime'))
                with mock.patch('routes.maintenance_admission.conversation_service',return_value=service):
                    proof=json.loads((await status(request)).body)
                    self.assertFalse(proof['idle']);self.assertEqual(1,proof['busy']['native_inventory_unavailable'])
            finally:
                callback.cancel();await asyncio.gather(callback,return_exceptions=True);store.close()
