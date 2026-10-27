"""Regression gates for native question races, cancellation and recovery."""
import asyncio
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import CodexAppServerClient, AppServerProtocolError
from engine.conversation_questions import answer_digest
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from tests.test_conversation_questions import request


class QuestionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.tmp.name) / 'db.sqlite3')
        self.store.create_conversation('conversation-1', title='fixture', cwd='/tmp', settings={
            'model': 'fixture', 'effort': 'medium', 'permission': 'danger-full-access',
        })
        self.store.bind_thread('conversation-1', {'id': 'thread-1'}, server_version='fixture')
        self.store.begin_command('conversation-1', 'start-1', kind='start', text='fixture')
        self.store.bind_turn('conversation-1', {'id': 'turn-1', 'status': 'inProgress'}, command_id='start-1')
        self.artifacts = ConversationArtifactService(self.store, Path(self.tmp.name) / 'artifacts')
        self.runtime = ConversationRuntime(self.store, self.artifacts, lambda **kwargs: None)
        self.tasks = []

    async def asyncTearDown(self):
        await self.runtime.close()
        for task in self.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.tmp.cleanup()

    async def ask(self, params=None, *, rpc=0, connection='instance:1'):
        task = asyncio.create_task(self.runtime.handle_server_request(
            'item/tool/requestUserInput', params or request(),
            request_id=rpc, connection_id=connection,
        ))
        self.tasks.append(task)
        await asyncio.sleep(0)
        return task

    def question(self):
        return self.store.snapshot('conversation-1')['questions'][-1]

    async def test_resolved_without_answer_cancels_waiter_and_never_replies(self):
        task = await self.ask(request(blocking=True))
        await self.runtime.handle_notification({
            'method': 'serverRequest/resolved',
            'params': {'threadId': 'thread-1', 'requestId': 0},
        }, connection_id='instance:1')
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.question()['state'], 'closed')
        self.assertFalse(self.runtime._question_waiters)
        self.assertFalse(self.runtime._question_deadlines)

    async def test_turn_stop_cancels_waiter_and_deadline(self):
        task = await self.ask()
        await self.runtime.handle_notification({
            'method': 'turn/completed',
            'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'interrupted'}},
        }, connection_id='instance:1')
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.question()['state'], 'closed')
        self.assertFalse(self.runtime._question_waiters)
        self.assertFalse(self.runtime._question_deadlines)

    async def test_user_click_after_deadline_sends_recommended_not_original_answer(self):
        task = await self.ask()
        question_id = self.question()['id']
        self.store._db.execute("UPDATE question_requests SET deadline_at='2000-01-01T00:00:00+00:00' WHERE id=?", (question_id,))
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='late-click', answers={'choice': 'A'})
        outcome = await asyncio.wait_for(task, 1)
        self.assertEqual(outcome.result, {'answers': {'choice': {'answers': ['A']}}})
        self.assertEqual(self.question()['answer_source'], 'deadline')
        self.assertEqual(self.question()['answers'], {'choice': 'A'})

    async def test_duplicate_inbound_request_does_not_reply_twice(self):
        first = await self.ask(request(blocking=True))
        second = await self.ask(request(blocking=True))
        with self.assertRaises(asyncio.CancelledError):
            await second
        question_id = self.question()['id']
        self.assertIn(question_id, self.runtime._question_waiters)
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='answer', answers={'choice': 'A'})
        outcome = await first
        self.assertEqual(outcome.result, {'answers': {'choice': {'answers': ['A']}}})

    async def test_reissued_expired_native_request_gets_new_delivery_attempt(self):
        first = await self.ask(request(auto=0))
        first_outcome = await asyncio.wait_for(first, 1)
        self.assertEqual(first_outcome.result, {'answers': {'choice': {'answers': ['A']}}})
        original = self.question()
        self.store.mark_questions_connection_lost('instance:1')
        second = await self.ask(request(auto=0), rpc='fresh', connection='instance:2')
        second_outcome = await asyncio.wait_for(second, 1)
        self.assertEqual(second_outcome.result, {'answers': {'choice': {'answers': ['A']}}})
        current = self.question()
        self.assertEqual(current['id'], original['id'])
        self.assertEqual(current['deadline_at'], original['deadline_at'])
        self.assertEqual(current['attempt'], 2)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM question_commands').fetchone()[0], 2)

    async def test_other_item_same_question_is_not_mistaken_for_reissue(self):
        first = await self.ask(request(auto=0))
        await first
        original = self.question()
        self.store.mark_questions_connection_lost('instance:1')
        new_params = request(auto=0)
        new_params['itemId'] = 'different-item'
        second = await self.ask(new_params, rpc='fresh', connection='instance:2')
        await second
        values = self.store.snapshot('conversation-1')['questions']
        self.assertEqual(len(values), 2)
        self.assertNotEqual(values[1]['id'], original['id'])

    async def test_reissue_rejects_old_browser_command_and_allows_new_command(self):
        first = await self.ask(request(blocking=True))
        question_id = self.question()['id']
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='old-click', answers={'choice': 'A'})
        await first
        self.store.mark_questions_connection_lost('instance:1')
        second = await self.ask(request(blocking=True), rpc='fresh', connection='instance:2')
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_question('conversation-1', question_id=question_id,
                command_id='old-click', answers={'choice': 'A'})
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='new-click', answers={'choice': 'A'})
        self.assertEqual((await second).result, {'answers': {'choice': {'answers': ['A']}}})

    async def test_commands_and_tool_output_do_not_claim_model_continued(self):
        task = await self.ask(request(auto=0))
        outcome = await task
        await outcome.on_sent()
        await self.runtime.handle_notification({
            'method': 'serverRequest/resolved', 'params': {'threadId': 'thread-1', 'requestId': 0},
        }, connection_id='instance:1')
        await self.runtime.handle_notification({
            'method': 'item/completed', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1',
                'item': {'id': 'command-x', 'type': 'commandExecution', 'command': 'fixture', 'status': 'completed'}},
        }, connection_id='instance:1')
        self.assertEqual(self.question()['state'], 'awaiting_activity')
        await self.runtime.handle_notification({
            'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1',
                'item': {'id': 'reason-x', 'type': 'reasoning'}},
        }, connection_id='instance:1')
        self.assertEqual(self.question()['state'], 'answered')

    async def test_duplicate_resolved_notification_is_idempotent(self):
        task = await self.ask(request(auto=0))
        outcome = await task
        await outcome.on_sent()
        notification = {'method': 'serverRequest/resolved',
                        'params': {'threadId': 'thread-1', 'requestId': 0}}
        for _ in range(2):
            await self.runtime.handle_notification(notification, connection_id='instance:1')
        self.assertEqual(self.question()['state'], 'awaiting_activity')

    async def test_secret_not_in_database_snapshot_or_offline_hash(self):
        params = request(questions=[{'id': 'secret', 'header': 'S', 'question': 'Secret', 'isSecret': True}])
        task = await self.ask(params)
        question_id = self.question()['id']
        secret = 'low-entropy-fixture-secret'
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='secret-click', answers={'secret': secret})
        outcome = await task
        self.assertEqual(outcome.result['answers']['secret']['answers'], [secret])
        digest = self.question()['answers_digest']
        self.assertNotEqual(digest, answer_digest({'secret': secret}))
        service = ConversationService(self.store, self.artifacts, self.runtime)
        public = service.public_snapshot('conversation-1')
        self.assertEqual(public['questions'][0]['answers_masked']['secret'], '***')
        dump = '\n'.join(self.store._db.iterdump())
        self.assertNotIn(secret, dump)
        self.assertNotIn(secret, json.dumps(public))
        await self.runtime.answer_question('conversation-1', question_id=question_id,
            command_id='secret-click', answers={'secret': secret})
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_question('conversation-1', question_id=question_id,
                command_id='secret-click', answers={'secret': 'changed'})

    async def test_cross_conversation_answer_does_not_modify_question(self):
        task = await self.ask(request(blocking=True))
        question_id = self.question()['id']
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_question('wrong-conversation', question_id=question_id,
                command_id='cross-click', answers={'choice': 'A'})
        self.assertEqual(self.question()['state'], 'awaiting_answer')
        self.assertFalse(task.done())

    async def test_previous_release_can_open_additive_question_database(self):
        # Rollback runs the previous release against the same durable SQLite.
        root = Path(__file__).resolve().parents[1]
        old_source = (root / 'tests/fixtures/conversation_store_pre_questions.py').read_text(encoding='utf-8')
        namespace = {'__name__': 'old_store_rollback_fixture'}
        exec(compile(old_source, '<previous store>', 'exec'), namespace)
        old = namespace['ConversationStore'](self.store.path)
        try:
            self.assertEqual(old.get_conversation('conversation-1')['id'], 'conversation-1')
        finally:
            old.close()


class ServerRequestEpochTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_callback_never_registers_on_new_epoch(self):
        handler = mock.AsyncMock(return_value={'answers': {}})
        client = CodexAppServerClient('/tmp/unused-socket', server_request_handler=handler)
        client._connected = True
        client._connection_epoch = 2
        client._enqueue = mock.AsyncMock()
        await client._handle_server_request({'id': 0, 'method': 'item/tool/requestUserInput', 'params': {}}, 1)
        handler.assert_not_called()
        client._enqueue.assert_not_called()

    async def test_callback_cannot_return_old_rpc_on_new_connection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(method, params, **kwargs):
            entered.set()
            await release.wait()
            return {'answers': {}}
        client = CodexAppServerClient('/tmp/unused-socket', server_request_handler=handler)
        client._connected = True
        client._connection_epoch = 1
        client._enqueue = mock.AsyncMock()
        task = asyncio.create_task(client._handle_server_request({'id': 0, 'method': 'item/tool/requestUserInput', 'params': {}}, 1))
        await entered.wait()
        client._connection_epoch = 2
        release.set()
        await task
        client._enqueue.assert_not_called()
