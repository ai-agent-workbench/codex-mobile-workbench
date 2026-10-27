import asyncio
import copy
import json
import hashlib
import unittest
from unittest import mock
from urllib.parse import urlencode

from engine.codex_app_server import AppServerDisconnected, AppServerProtocolError, AppServerRPCError
from engine.conversation_actions import ConversationActions
from engine.conversation_service import ConversationService
from engine.conversation_plan import plan_confirmation_candidate
import test_conversation_runtime as runtime_fixture
import test_conversation_goals as goal_fixture


async def finish_plan(fixture, text="修改功能并通过测试"):
    thread = fixture.factory.threads['thread-1']
    turn = thread['turns'][-1]
    item = {'id': 'approved-plan', 'type': 'plan', 'text': text}
    turn.update(status='completed', items=[item])
    thread['status'] = {'type': 'idle'}
    await fixture.runtime.handle_notification({'method': 'item/completed', 'params': {
        'threadId': 'thread-1', 'turnId': turn['id'], 'item': copy.deepcopy(item)}})
    await fixture.runtime.handle_notification({'method': 'turn/completed', 'params': {
        'threadId': 'thread-1', 'turn': copy.deepcopy(turn)}})
    projected = fixture.store.snapshot('conversation-1')['turns'][-1]
    plan = next(i for i in projected['items'] if i['kind'] == 'plan')
    return {'turn_id': projected['id'], 'item_id': plan['native_item_id'], 'plan_digest': hashlib.sha256(plan['text'].encode()).hexdigest()}


async def finish_reply(fixture):
    thread = fixture.factory.threads['thread-1']
    turn = thread['turns'][-1]
    item = {'id': 'reply-' + turn['id'], 'type': 'agentMessage', 'text': '已收到', 'phase': 'final_answer'}
    turn.update(status='completed', items=[item])
    thread['status'] = {'type': 'idle'}
    await fixture.runtime.handle_notification({'method': 'turn/completed', 'params': {
        'threadId': 'thread-1', 'turn': copy.deepcopy(turn)}})


class PlanActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = runtime_fixture.ConversationRuntimeTests()
        await self.t.asyncSetUp()
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        self.binding = await finish_plan(self.t)
        self.actions = ConversationActions(self.t.runtime)

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def confirm(self, command='confirm-plan', **updates):
        return await self.actions.implement_plan('conversation-1', command_id=command,
            skills=['selected-skill'], **{**self.binding, **updates})

    async def test_approval_switches_mode_starts_once_and_keeps_permissions(self):
        result = await self.confirm()
        self.assertEqual('accepted', result['command']['state'])
        calls = [v for n, v in self.t.factory.clients[0].calls if n == 'turn/start']
        self.assertEqual(2, len(calls))
        opts = calls[-1]['options']
        self.assertEqual('default', opts['collaborationMode']['mode'])
        self.assertEqual('danger-full-access', opts['sandbox'])
        self.assertIsNone(opts['collaborationMode']['settings']['developer_instructions'])
        self.assertIn('按刚刚确认的计划', calls[-1]['text'])
        self.assertIn('修改功能并通过测试', calls[-1]['leading_context'])
        self.assertIn(self.binding['plan_digest'], calls[-1]['leading_context'])
        repeated = await self.confirm()
        self.assertEqual(result['command']['result_turn_id'], repeated['command']['result_turn_id'])
        self.assertEqual(2, len([n for n, _ in self.t.factory.clients[0].calls if n == 'turn/start']))

    async def test_reviewed_completed_supplements_start_once_and_are_native_context(self):
        await self.t.runtime.send_message('conversation-1', command_id='supplement-one', text='保留这项后续要求')
        await self.t.runtime.send_message('conversation-1', command_id='supplement-two', text='还有第二项要求')
        await finish_reply(self.t)
        candidate = plan_confirmation_candidate(self.t.store.snapshot('conversation-1'))
        self.assertEqual('review', candidate['state'])
        reviewed = {**self.binding, 'context_digest': candidate['context_digest']}
        result = await self.actions.implement_plan('conversation-1', command_id='review-confirm', **reviewed)
        self.assertEqual('accepted', result['command']['state'])
        calls = [value for name, value in self.t.factory.clients[0].calls if name == 'turn/start']
        self.assertEqual(3, len(calls))
        self.assertIn('保留这项后续要求', calls[-1]['leading_context'])
        self.assertLess(calls[-1]['leading_context'].index('保留这项后续要求'), calls[-1]['leading_context'].index('还有第二项要求'))
        self.assertIn('后续用户补充要求仍有效', calls[-1]['leading_context'])
        self.assertNotIn('不用其他计划替换', calls[-1]['leading_context'])
        repeated = await self.actions.implement_plan('conversation-1', command_id='review-confirm', **reviewed)
        self.assertEqual(result['command']['result_turn_id'], repeated['command']['result_turn_id'])
        await finish_reply(self.t)
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        self.assertEqual('plan_already_confirmed', plan_confirmation_candidate(self.t.store.snapshot('conversation-1'))['reason'])
        duplicate = await self.actions.implement_plan('conversation-1', command_id='new-reviewed-id', **reviewed)
        self.assertEqual('rejected', duplicate['command']['state'])
        self.assertEqual(3, len([v for n, v in self.t.factory.clients[0].calls if n == 'turn/start']))

    async def test_new_supplement_during_review_preflight_rejects_without_delivery(self):
        await self.t.runtime.send_message('conversation-1', command_id='supplement', text='后续要求')
        await finish_reply(self.t)
        candidate = plan_confirmation_candidate(self.t.store.snapshot('conversation-1'))
        client = self.t.factory.clients[0]
        original = client.resolve_skill_inputs
        async def new_input(*args, **kwargs):
            self.t.store.begin_command('conversation-1', 'late-supplement', kind='steer', text='新要求还没核对')
            return await original(*args, **kwargs)
        client.resolve_skill_inputs = new_input
        result = await self.actions.implement_plan('conversation-1', command_id='race-review',
            context_digest=candidate['context_digest'], skills=['selected-skill'], **self.binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(2, len([v for n, v in client.calls if n == 'turn/start']))

    async def test_review_supplement_attachment_is_private_and_missing_version_restores_mode(self):
        await self.t.runtime.send_message('conversation-1', command_id='supplement-file', text='按这个附件补充')
        await finish_reply(self.t)
        file = self.t.root / 'frozen-evidence.txt'
        file.write_text('补充要求', encoding='utf-8')
        info = file.stat()
        ref = {'id': 'a' * 36, 'name': file.name, 'size': info.st_size, 'mime_type': 'text/plain',
               'preview_kind': 'text', 'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
               'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]}
        payload = self.t.store.command('supplement-file')['payload']
        payload['attachments'] = [ref]
        self.t.store._db.execute('UPDATE commands SET payload_json=? WHERE id=?', (json.dumps(payload), 'supplement-file'))
        service = ConversationService(self.t.store, self.t.artifacts, self.t.runtime)
        candidate = service.public_snapshot('conversation-1')['plan_confirmation']
        self.assertEqual('review', candidate['state'])
        self.assertNotIn(str(file), json.dumps(candidate))
        self.assertEqual(file.name, candidate['supplements'][0]['attachments'][0]['name'])
        file.unlink()
        result = await self.actions.implement_plan('conversation-1', command_id='missing-review-file',
            context_digest=candidate['context_digest'], **self.binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(2, len([v for n, v in self.t.factory.clients[0].calls if n == 'turn/start']))

    async def test_wrong_revision_and_foreign_item_cannot_switch_mode(self):
        for change in ({'plan_digest': '0' * 64}, {'item_id': 'other-plan'}, {'turn_id': 'other-turn'}):
            result = await self.confirm(command='bad-' + str(len(self.t.store.snapshot('conversation-1')['commands'])), **change)
            self.assertEqual('rejected', result['command']['state'])
            self.assertEqual('plan_confirmation_stale', result['command']['reason'])
            self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])

    async def test_newer_turn_invalidates_old_plan(self):
        await self.t.runtime.send_message('conversation-1', command_id='refine', text='先修改计划')
        result = await self.confirm()
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])

    async def test_native_plan_content_change_invalidates_displayed_fingerprint(self):
        self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '另一份尚未确认的计划'
        result = await self.confirm()
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])

    async def test_lost_turn_response_is_unknown_and_never_replayed(self):
        client = self.t.factory.clients[0]
        client.start_turn_error = AppServerDisconnected('lost response')
        result = await self.confirm()
        self.assertEqual('delivery_unknown', result['command']['state'])
        count = len([n for n, _ in client.calls if n == 'turn/start'])
        repeated = await self.confirm()
        self.assertEqual('delivery_unknown', repeated['command']['state'])
        self.assertEqual(count, len([n for n, _ in client.calls if n == 'turn/start']))

    async def test_reused_command_with_different_plan_is_not_accepted(self):
        await self.confirm()
        with self.assertRaises(AppServerProtocolError):
            await self.confirm(plan_digest='0' * 64)

    async def test_plan_changed_during_preflight_or_skill_resolution_is_not_sent(self):
        client = self.t.factory.clients[0]
        original_read = client.read_thread
        count = 0
        async def race_read(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '新计划，不允许用旧确认实施'
            return await original_read(*args, **kwargs)
        client.read_thread = race_read
        result = await self.confirm(command='late-preflight-change')
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in client.calls if n == 'turn/start']))
        client.read_thread = original_read
        self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '修改功能并通过测试'
        original_skills = client.resolve_skill_inputs
        async def race_skills(*args, **kwargs):
            self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '技能读取期间更新的计划'
            return await original_skills(*args, **kwargs)
        client.resolve_skill_inputs = race_skills
        result = await self.confirm(command='late-skills-change')
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in client.calls if n == 'turn/start']))

    async def test_restart_settles_already_rejected_idle_owner_without_goal(self):
        self.t.store.begin_command('conversation-1', 'restart-rejected', kind='steer', text='实施',
            payload={'plan_confirmation': self.binding})
        self.t.store.update_command('restart-rejected', 'rejected', reason='cancelled_before_delivery')
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'default',
            '_plan_approval_transition': {'command_id': 'restart-rejected', 'previous_mode': 'plan'}})
        await self.t.runtime.startup_recover()
        settings = self.t.store.get_conversation('conversation-1')['settings']
        self.assertEqual('plan', settings['collaboration_mode'])
        self.assertIsNone(settings['_plan_approval_transition'])
        self.assertEqual('rejected', self.t.store.command('restart-rejected')['state'])
        self.assertEqual(1, len([n for client in self.t.factory.clients for n, _ in client.calls if n == 'turn/start']))

    async def test_compensation_receipt_owner_and_local_mode_are_atomic(self):
        store = self.t.store
        store.begin_command('conversation-1', 'atomic-restore', kind='steer', text='实施',
            payload={'plan_confirmation': self.binding})
        store.update_command('atomic-restore', 'delivery_unknown', reason='plan_mode_restoring')
        store.update_settings('conversation-1', {'collaboration_mode': 'default',
            '_plan_approval_transition': {'command_id': 'atomic-restore', 'previous_mode': 'plan'}})
        original_update = store.update_settings
        def fail_after_owner_write(cid, updates):
            result = original_update(cid, updates)
            if updates.get('_plan_approval_transition', 'absent') is None:
                raise OSError('simulated crash before atomic commit')
            return result
        with mock.patch.object(store, 'update_settings', side_effect=fail_after_owner_write):
            with self.assertRaises(OSError):
                self.t.runtime._finish_plan_compensation('conversation-1', 'atomic-restore',
                    'cancelled_before_delivery', {'collaboration_mode': 'plan'})
        self.assertEqual('delivery_unknown', store.command('atomic-restore')['state'])
        settings = store.get_conversation('conversation-1')['settings']
        self.assertEqual('default', settings['collaboration_mode'])
        self.assertEqual('atomic-restore', settings['_plan_approval_transition']['command_id'])
        self.t.runtime._finish_plan_compensation('conversation-1', 'atomic-restore',
            'cancelled_before_delivery', {'collaboration_mode': 'plan'})
        self.assertEqual('rejected', store.command('atomic-restore')['state'])
        settings = store.get_conversation('conversation-1')['settings']
        self.assertEqual('plan', settings['collaboration_mode'])
        self.assertIsNone(settings['_plan_approval_transition'])


class GoalActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = goal_fixture.ConversationGoalTests()
        await self.t.asyncSetUp()
        self.actions = ConversationActions(self.t.runtime)

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def start_goal(self, **updates):
        return await self.actions.start_goal('conversation-1',
            **{'command_id': 'start-goal', 'objective': '实现功能并通过测试',
               'skills': ['preserved-skill'], **updates})

    async def test_review_with_paused_native_goal_keeps_pause_and_unknown_is_not_replayed(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change()
        await self.t.runtime.send_message('conversation-1', command_id='goal-supplement', text='补充要求不要丢')
        await finish_reply(self.t)
        await self.t.change('pause', 'pause-before-review')
        candidate = self.t.service.public_snapshot('conversation-1')['plan_confirmation']
        self.assertEqual('review', candidate['state'])
        self.t.client.start_turn_error = AppServerDisconnected('lost review response')
        reviewed = {**binding, 'context_digest': candidate['context_digest']}
        result = await self.actions.implement_plan('conversation-1', command_id='unknown-reviewed-goal', **reviewed)
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.assertEqual('paused', self.t.runtime.goals.store.public('conversation-1')['goal']['status'])
        calls = len(self.t.client.calls)
        repeated = await self.actions.implement_plan('conversation-1', command_id='unknown-reviewed-goal', **reviewed)
        self.assertEqual('delivery_unknown', repeated['command']['state'])
        self.assertEqual(calls, len(self.t.client.calls), '同一未知请求只能核对，不能重投')

    async def test_review_after_user_steer_in_native_automatic_turn(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change()
        automatic = {'id': 'automatic-with-user', 'status': 'inProgress', 'items': []}
        self.t.factory.threads['thread-1']['turns'].append(automatic)
        self.t.factory.threads['thread-1']['status'] = {'type': 'active'}
        await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {
            'threadId': 'thread-1', 'turn': copy.deepcopy(automatic)}})
        delivered = await self.t.runtime.send_message('conversation-1', command_id='during-auto', text='这个补充必须保留')
        self.assertEqual('accepted', delivered['command']['state'])
        await finish_reply(self.t)
        candidate = self.t.service.public_snapshot('conversation-1')['plan_confirmation']
        self.assertEqual('review', candidate['state'])
        self.assertEqual(['during-auto'], [item['id'] for item in candidate['supplements']])
        result = await self.actions.implement_plan('conversation-1', command_id='review-auto-supplement',
            context_digest=candidate['context_digest'], **binding)
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('default', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        calls = [value for name, value in self.t.client.calls if name == 'turn/start']
        self.assertEqual(2, len(calls), '自动轮不被补造，补充不被重发成新轮')
        self.assertIn('这个补充必须保留', calls[-1]['leading_context'])

    async def staged_plan_restart(self, reason, state='idle'):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        self.t.store.begin_command('conversation-1', 'restart-plan', kind='steer', text='实施',
            payload={'plan_confirmation': binding})
        self.t.store.update_command('restart-plan', 'submitted', reason=reason)
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'default',
            '_plan_approval_transition': {'command_id': 'restart-plan', 'previous_mode': 'plan'}})
        self.t.store.set_conversation_state('conversation-1', state, active_turn_id=None)
        await self.t.runtime.startup_recover()

    async def test_restart_restores_acknowledged_unsent_approval_before_next_message(self):
        await self.staged_plan_restart('plan_settings_synced', state='starting_turn')
        self.assertEqual('rejected', self.t.store.command('restart-plan')['state'])
        settings = self.t.store.get_conversation('conversation-1')['settings']
        self.assertEqual('plan', settings['collaboration_mode'])
        self.assertIsNone(settings['_plan_approval_transition'])
        self.assertEqual('plan', [v for n, v in self.t.client.calls if n == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))
        await self.t.runtime.send_message('conversation-1', command_id='restart-refine', text='继续修改计划')
        self.assertEqual('plan', [v for n, v in self.t.client.calls if n == 'turn/start'][-1]['options']['collaborationMode']['mode'])

    async def test_restart_preserves_unacknowledged_settings_as_unknown_even_when_idle(self):
        await self.staged_plan_restart('native_rpc_sending')
        self.assertEqual('delivery_unknown', self.t.store.command('restart-plan')['state'])
        self.assertEqual('needs_user', self.t.store.get_conversation('conversation-1')['state'])
        self.assertIsNotNone(self.t.store.get_conversation('conversation-1')['settings']['_plan_approval_transition'])
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))

    async def test_settings_ack_followed_by_flush_failure_compensates_unsent_approval(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        client = self.t.client
        original_sync, original_flush = self.t.runtime.goals.sync_settings, client.flush_notifications
        fail_after_ack = False
        async def mark_ack(*args, **kwargs):
            nonlocal fail_after_ack
            result = await original_sync(*args, **kwargs)
            if kwargs.get('settings', {}).get('collaboration_mode') == 'default':
                fail_after_ack = True
            return result
        async def lose_flush(*args, **kwargs):
            nonlocal fail_after_ack
            if fail_after_ack:
                fail_after_ack = False
                raise AppServerDisconnected('notifications lost after settings ACK')
            return await original_flush(*args, **kwargs)
        self.t.runtime.goals.sync_settings = mark_ack
        client.flush_notifications = lose_flush
        result = await self.actions.implement_plan('conversation-1', command_id='ack-then-read-fails', **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan_preflight_failed', result['command']['reason'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual('plan', [v for n, v in client.calls if n == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in client.calls if n == 'turn/start']))

    async def test_rejected_requests_cannot_compensate_across_pending_delete(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        store = self.t.store
        store.begin_command('conversation-1', 'delete-barrier-owner', kind='steer', text='实施',
            payload={'plan_confirmation': binding})
        store.update_command('delete-barrier-owner', 'rejected', reason='not_delivered_before_restart')
        store.update_settings('conversation-1', {'collaboration_mode': 'default',
            '_plan_approval_transition': {'command_id': 'delete-barrier-owner', 'previous_mode': 'plan'}})
        store.set_conversation_state('conversation-1', 'needs_user', native_delete_pending=True)
        calls = len(self.t.client.calls)
        sent = await self.t.runtime.send_message('conversation-1', command_id='delete-barrier-send', text='修改计划')
        settings = await self.t.runtime.update_settings('conversation-1', command_id='delete-barrier-settings',
            updates={'collaboration_mode': 'plan'})
        self.assertEqual('rejected', sent['command']['state'])
        self.assertEqual('rejected', settings['command']['state'])
        self.assertEqual(calls, len(self.t.client.calls), '删除门槛下被拒绝的请求不得发出补偿设置RPC')
        self.assertIsNotNone(store.get_conversation('conversation-1')['settings']['_plan_approval_transition'])
        store.set_conversation_state('conversation-1', 'idle', native_delete_pending=False)
        await self.t.runtime.reconcile('conversation-1')
        self.assertIsNone(store.get_conversation('conversation-1')['settings']['_plan_approval_transition'])
        self.assertEqual('plan', store.get_conversation('conversation-1')['settings']['collaboration_mode'])

    async def test_new_goal_uses_objective_as_only_first_prompt_without_budget(self):
        result = await self.start_goal()
        self.assertEqual('accepted', result['command']['state'])
        calls = self.t.client.calls
        starts = [v for n, v in calls if n == 'turn/start']
        self.assertEqual(1, len(starts))
        self.assertEqual('实现功能并通过测试', starts[0]['text'])
        self.assertEqual('preserved-skill', starts[0]['skill_inputs'][0]['name'])
        goal = [v for n, v in calls if n == 'goal/set'][-1]
        self.assertNotIn('tokenBudget', goal)
        self.assertEqual('active', goal['status'])
        await self.start_goal()
        self.assertEqual(1, len([n for n, _ in calls if n == 'turn/start']))
        self.assertEqual(1, len([n for n, _ in calls if n == 'goal/set']))

    async def test_first_message_success_is_not_goal_activation_success(self):
        client = await self.t.runtime._ensure_client()
        original = client.set_thread_goal
        for index, error in enumerate((AppServerRPCError(-32600, 'activation rejected'),
                                       AppServerDisconnected('activation response lost'))):
            with self.subTest(error=type(error).__name__):
                cid, command_id = 'partial-goal-' + str(index), 'partial-start-' + str(index)
                self.t.store.create_conversation(cid, title='新对话', cwd=str(self.t.root),
                    settings={'model': 'fixture', 'effort': 'medium', 'permission': 'read-only'})
                async def fail(*args, **kwargs):
                    client.calls.append(('goal/set', kwargs))
                    raise error
                client.set_thread_goal = fail
                result = await self.actions.start_goal(cid, command_id=command_id, objective='保留失败的目标')
                self.assertEqual('accepted', self.t.store.command(command_id)['state'])
                expected = 'rejected' if isinstance(error, AppServerRPCError) else 'delivery_unknown'
                self.assertEqual(expected, result['command']['state'])
                self.assertEqual('accepted', result['command']['message_state'])
                self.assertTrue(self.t.runtime.goals.store.entry(cid)['draft']['activation_unconfirmed'])
                count = len(client.calls)
                repeated = await self.actions.start_goal(cid, command_id=command_id, objective='保留失败的目标')
                self.assertEqual(expected, repeated['command']['state'])
                self.assertEqual(count, len(client.calls), '不重发首轮或激活请求')
                from routes.conversation_api import conversation_action_receipt
                with mock.patch('routes.conversation_api.conversation_service', return_value=self.t.service), mock.patch('routes.conversation_api._auth', return_value=None):
                    response = await conversation_action_receipt(None, cid, command_id)
                self.assertEqual(expected, json.loads(response.body)['command']['state'])
                client.set_thread_goal = original
                starts = len([name for name, _ in client.calls if name == 'turn/start'])
                version = str(self.t.runtime.goals.store.entry(cid)['version'])
                recovered = await self.actions.start_goal(cid, command_id='explicit-resume-' + str(index),
                    objective='保留失败的目标', goal_version=version)
                self.assertEqual('accepted', recovered['command']['state'])
                self.assertEqual(starts, len([name for name, _ in client.calls if name == 'turn/start']),
                    '明确启用失败草稿只设置原生Goal，不再重发已成功首轮')
        client.set_thread_goal = original

    async def test_stale_first_goal_draft_is_rejected_before_thread_creation(self):
        self.t.store.update_conversation_title('conversation-1', '新对话')
        self.t.runtime.goals.save_draft('conversation-1', '其他已保存目标')
        result = await self.t.service.start_goal('conversation-1', command_id='stale-service-start',
            objective='错误的新目标')
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('goal_start_stale', result['command']['reason'])
        self.assertFalse(self.t.factory.clients)
        self.assertEqual('新对话', self.t.store.get_conversation('conversation-1')['title'])

    async def test_existing_idle_goal_is_native_only_no_second_manual_turn(self):
        await self.t.start()
        await self.t.complete_first()
        self.t.client.emit_auto = True
        result = await self.start_goal()
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('auto-turn', self.t.store.get_conversation('conversation-1')['active_turn_id'])
        await self.start_goal()
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'goal/set']))

    async def test_existing_idle_goal_without_native_wakeup_seeds_once_and_normalizes_standard(self):
        await self.t.start()
        await self.t.complete_first()
        original = self.t.client.update_goal_thread_settings
        async def acknowledge_default(thread_id, **settings):
            await original(thread_id, **settings)
            await self.t.runtime.handle_notification({'method': 'thread/settings/updated', 'params': {
                'threadId': thread_id, 'threadSettings': {'serviceTier': 'default'}}})
        self.t.client.update_goal_thread_settings = acknowledge_default
        result = await self.start_goal()
        self.assertEqual('accepted', result['command']['state'])
        self.assertIsNone(self.t.store.get_conversation('conversation-1')['settings']['service_tier'])
        starts = [v for n, v in self.t.client.calls if n == 'turn/start']
        self.assertEqual(2, len(starts), '空闲已有Thread明确开始Goal需要且仅需要一个启动轮')
        self.assertEqual('实现功能并通过测试', starts[-1]['text'])
        self.assertNotIn('serviceTier', starts[-1]['options'])
        await self.start_goal()
        self.assertEqual(2, len([n for n, _ in self.t.client.calls if n == 'turn/start']))
        await self.t.runtime.handle_notification({'method': 'thread/settings/updated', 'params': {
            'threadId': 'thread-1', 'threadSettings': {'serviceTier': 'priority'}}})
        self.assertEqual('priority', self.t.store.get_conversation('conversation-1')['settings']['service_tier'], '不能把实际 Fast 档误清除')

    async def test_goal_seed_notification_needs_matching_message_identity(self):
        await self.t.start()
        await self.t.complete_first()
        client = self.t.client
        async def unrelated_automatic(thread_id, text, **values):
            client.calls.append(('turn/start', {'text': text}))
            turn = {'id': 'racing-auto', 'status': 'inProgress', 'items': []}
            self.t.factory.threads[thread_id]['turns'].append(copy.deepcopy(turn))
            self.t.factory.threads[thread_id]['status'] = {'type': 'active'}
            await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': thread_id, 'turn': turn}})
            raise AppServerDisconnected('seed response lost; automatic turn is unrelated')
        client.start_turn = unrelated_automatic
        result = await self.start_goal()
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.assertIsNone(self.t.store.command('start-goal')['result_turn_id'])
        commands = self.t.store.snapshot('conversation-1')['commands']
        owner = [value for value in commands if value.get('result_turn_id') == 'racing-auto']
        self.assertEqual(['goal_continue'], [value['kind'] for value in owner], '自动轮不冒充用户seed已送达')
        count = len(client.calls)
        await self.start_goal()
        self.assertEqual(count, len(client.calls))

    async def test_goal_seed_exact_identity_is_positive_delivery_evidence(self):
        await self.t.start()
        await self.t.complete_first()
        client = self.t.client
        original = client.start_turn
        client.notify_turn_started_before_response = True
        async def lost_after_exact(*args, **kwargs):
            await original(*args, **kwargs)
            raise AppServerDisconnected('response lost after own user message notification')
        client.start_turn = lost_after_exact
        result = await self.start_goal()
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('turn-2', result['command']['result_turn_id'])

    async def test_late_seed_read_failure_settles_receipt_without_replaying_goal_or_input(self):
        await self.t.start()
        await self.t.complete_first()
        original = self.t.runtime._validate_goal_seed
        async def failed(*args, **kwargs):
            raise AppServerDisconnected('late Goal validation read failed')
        self.t.runtime._validate_goal_seed = failed
        result = await self.start_goal()
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('preflight_reconcile_failed', result['command']['reason'])
        self.assertEqual('needs_user', self.t.store.get_conversation('conversation-1')['state'])
        self.assertEqual('active', self.t.factory.goals['thread-1']['status'], 'Goal配置已经成功，但本轮输入没有发送')
        self.assertEqual(1, len([name for name, _ in self.t.client.calls if name == 'turn/start']))
        self.t.runtime._validate_goal_seed = original
        count = len(self.t.client.calls)
        await self.start_goal()
        self.assertEqual(count, len(self.t.client.calls))

    async def test_cancel_unsent_plan_approval_compensates_local_and_native_mode(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        reached = asyncio.Event()
        async def waiting(*args, **kwargs):
            reached.set()
            await asyncio.Event().wait()
        with mock.patch.object(self.t.runtime, '_preflight_start_locked', new=waiting):
            task = asyncio.create_task(self.actions.implement_plan('conversation-1', command_id='cancel-approval', **binding))
            await reached.wait()
            self.assertEqual('default', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual('rejected', self.t.store.command('cancel-approval')['state'])
        settings = self.t.store.get_conversation('conversation-1')['settings']
        self.assertEqual('plan', settings['collaboration_mode'])
        self.assertIsNone(settings['_plan_approval_transition'])
        self.assertEqual('plan', [value for name, value in self.t.client.calls if name == 'goal/settings'][-1]['collaboration_mode'])
        count = len(self.t.client.calls)
        await self.actions.implement_plan('conversation-1', command_id='cancel-approval', **binding)
        self.assertEqual(count, len(self.t.client.calls))

    async def test_second_cancellation_during_mode_restoration_stays_unknown(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        ready, restoring = asyncio.Event(), asyncio.Event()
        original = self.t.client.update_goal_thread_settings
        async def wait_preflight(*args, **kwargs):
            ready.set(); await asyncio.Event().wait()
        async def wait_restore(*args, **kwargs):
            if kwargs['collaboration_mode'] == 'plan':
                restoring.set(); await asyncio.Event().wait()
            return await original(*args, **kwargs)
        self.t.client.update_goal_thread_settings = wait_restore
        with mock.patch.object(self.t.runtime, '_preflight_start_locked', new=wait_preflight):
            task = asyncio.create_task(self.actions.implement_plan('conversation-1', command_id='double-cancel-approval', **binding))
            await ready.wait(); task.cancel(); await restoring.wait()
            self.assertEqual('delivery_unknown', self.t.store.command('double-cancel-approval')['state'])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual('plan_mode_restore_unknown', self.t.store.command('double-cancel-approval')['reason'])
        self.assertEqual('needs_user', self.t.store.get_conversation('conversation-1')['state'])
        count = len(self.t.client.calls)
        await self.actions.implement_plan('conversation-1', command_id='double-cancel-approval', **binding)
        self.assertEqual(count, len(self.t.client.calls))

    async def test_last_start_validation_failure_is_unsent_and_restores_plan(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start(); binding = await finish_plan(self.t); await self.t.change(status='paused')
        original = self.t.runtime._validate_plan_delivery
        async def fail_late(*args, **kwargs):
            if self.t.store.get_conversation('conversation-1')['state'] == 'starting_turn':
                raise AppServerDisconnected('final read before start failed')
            return await original(*args, **kwargs)
        self.t.runtime._validate_plan_delivery = fail_late
        result = await self.actions.implement_plan('conversation-1', command_id='unsent-start-failed', **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('preflight_reconcile_failed', result['command']['reason'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual('plan', [value for name, value in self.t.client.calls if name == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual(1, len([name for name, _ in self.t.client.calls if name == 'turn/start']))

    async def test_last_queued_steer_validation_failure_restores_plan_without_delivery(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start(); binding = await finish_plan(self.t)
        self.t.client.emit_auto = True; await self.t.change()
        await self.actions.implement_plan('conversation-1', command_id='unsent-steer-failed', **binding)
        self.t.factory.threads['thread-1']['turns'][-1]['status'] = 'completed'
        new = {'id': 'new-default-auto', 'status': 'inProgress', 'items': []}
        self.t.factory.threads['thread-1']['turns'].append(new)
        original = self.t.runtime._validate_plan_delivery
        async def fail_late(*args, **kwargs):
            if self.t.store.command('unsent-steer-failed')['state'] == 'queued':
                raise AppServerDisconnected('final read before steer failed')
            return await original(*args, **kwargs)
        self.t.runtime._validate_plan_delivery = fail_late
        await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': copy.deepcopy(new)}})
        await self.t.runtime.goals.deliver_queue('conversation-1', new['id'])
        self.assertEqual('rejected', self.t.store.command('unsent-steer-failed')['state'])
        self.assertEqual('preflight_reconcile_failed', self.t.store.command('unsent-steer-failed')['reason'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertFalse(any(name == 'turn/steer' for name, _ in self.t.client.calls))

    async def tree_stop_restores_queued_approval(self, deferred):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start(); binding = await finish_plan(self.t)
        self.t.client.emit_auto = True; await self.t.change()
        await self.actions.implement_plan('conversation-1', command_id='stopped-approval', **binding)
        self.t.client.supports_conversation_tree_stop = True
        async def stop(cid, client, *, mutate, **kwargs):
            if mutate:
                await client.set_thread_goal('thread-1', status='paused')
                if deferred: return {'status': 'unconfirmed', 'errors': 1}
            for turn in self.t.factory.threads['thread-1']['turns']: turn['status'] = 'completed'
            self.t.factory.threads['thread-1']['status'] = {'type': 'idle'}
            return {'status': 'verified', 'errors': 0}
        async def project(cid, client):
            self.t.runtime._reconcile_native(cid, await client.read_thread('thread-1'))
        with mock.patch.object(self.t.runtime, '_run_tree_stop', new=stop), mock.patch.object(self.t.runtime, '_project_stopped_root', new=project):
            await self.t.runtime.interrupt('conversation-1', command_id='stop-queue', scope='conversation_tree')
            if deferred:
                self.assertTrue(self.t.store.pending_tree_stop('conversation-1'))
                self.assertIsNotNone(self.t.store.get_conversation('conversation-1')['settings']['_plan_approval_transition'])
                await self.t.runtime.reconcile('conversation-1', origin='manual')
        self.assertEqual('completed', self.t.store.command('stop-queue')['state'])
        self.assertEqual('rejected', self.t.store.command('stopped-approval')['state'])
        settings = self.t.store.get_conversation('conversation-1')['settings']
        self.assertEqual('plan', settings['collaboration_mode'])
        self.assertIsNone(settings['_plan_approval_transition'])
        self.assertEqual('plan', [value for name, value in self.t.client.calls if name == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual('paused', self.t.factory.goals['thread-1']['status'])
        self.assertFalse(any(name == 'turn/steer' for name, _ in self.t.client.calls))

    async def test_verified_tree_stop_compensates_queued_approval(self):
        await self.tree_stop_restores_queued_approval(False)

    async def test_unknown_tree_stop_defers_compensation_until_read_only_verification(self):
        await self.tree_stop_restores_queued_approval(True)

    async def test_idle_active_goal_confirmation_seeds_one_explicit_user_turn(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change()
        result = await self.actions.implement_plan('conversation-1', command_id='approve-with-goal', **binding)
        self.assertEqual('accepted', result['command']['state'])
        settings = [v for n, v in self.t.client.calls if n == 'goal/settings'][-1]
        self.assertEqual('default', settings['collaboration_mode'])
        self.assertEqual('active', self.t.factory.goals['thread-1']['status'])
        self.assertEqual(2, len([n for n, _ in self.t.client.calls if n == 'turn/start']))
        await self.actions.implement_plan('conversation-1', command_id='approve-with-goal', **binding)
        self.assertEqual(2, len([n for n, _ in self.t.client.calls if n == 'turn/start']),'确认就是一条用户请求，不建立工作台续跑循环')

    async def test_paused_goal_is_not_resumed_by_plan_confirmation(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        result = await self.actions.implement_plan('conversation-1', command_id='approve-paused', **binding)
        self.assertEqual('accepted', result['command']['state'])
        self.assertEqual('paused', self.t.factory.goals['thread-1']['status'])
        self.assertEqual(2, len([n for n, _ in self.t.client.calls if n == 'turn/start']))

    async def test_automatic_turn_does_not_hide_plan_and_confirmation_waits_for_default_boundary(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        self.t.client.emit_auto = True
        await self.t.change()
        for tid in (binding['turn_id'], 'auto-turn'):
            await self.t.runtime.handle_notification({'method': 'turn/plan/updated', 'params': {
                'threadId': 'thread-1', 'turnId': tid, 'plan': [{'step': '核对已确认计划', 'status': 'inProgress'}]}})
        self.assertEqual('auto-turn', self.t.store.get_conversation('conversation-1')['active_turn_id'])
        result = await self.actions.implement_plan('conversation-1', command_id='active-auto-approval', **binding)
        self.assertEqual('queued', result['command']['state'])
        await self.t.runtime.goals.deliver_queue('conversation-1', 'auto-turn')
        self.assertFalse(any(n == 'turn/steer' for n, _ in self.t.client.calls), '旧 Plan 轮不消费实施确认')
        old = self.t.factory.threads['thread-1']['turns'][-1]
        old.update(status='completed')
        await self.t.runtime.handle_notification({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': copy.deepcopy(old)}})
        new = {'id': 'default-auto-turn', 'status': 'inProgress', 'items': []}
        self.t.factory.threads['thread-1']['turns'].append(new)
        self.t.factory.threads['thread-1']['status'] = {'type': 'active'}
        await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': copy.deepcopy(new)}})
        await self.t.runtime.goals.deliver_queue('conversation-1', new['id'])
        self.assertEqual('accepted', self.t.store.command('active-auto-approval')['state'])
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))
        steer = next(v for n, v in self.t.client.calls if n == 'turn/steer')
        self.assertIn('修改功能并通过测试', steer['leading_context'])

    async def test_new_plan_during_queued_goal_confirmation_is_rejected_at_delivery(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        self.t.client.emit_auto = True
        await self.t.change()
        result = await self.actions.implement_plan('conversation-1', command_id='queued-approval', **binding)
        self.assertEqual('queued', result['command']['state'])
        self.t.factory.threads['thread-1']['turns'][-1]['status'] = 'completed'
        new = {'id': 'auto-with-new-plan', 'status': 'inProgress', 'items': [{'id': 'new-plan', 'type': 'plan', 'text': '不同的新计划'}]}
        self.t.factory.threads['thread-1']['turns'].append(new)
        await self.t.runtime.handle_notification({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': copy.deepcopy(new)}})
        await self.t.runtime.goals.deliver_queue('conversation-1', new['id'])
        self.assertEqual('rejected', self.t.store.command('queued-approval')['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual('plan', [v for n, v in self.t.client.calls if n == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual('active', self.t.factory.goals['thread-1']['status'])
        self.assertFalse(any(n == 'turn/steer' for n, _ in self.t.client.calls))

    async def test_plan_change_while_syncing_goal_settings_rejects_and_restores_plan(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        original = self.t.client.update_goal_thread_settings
        async def race_settings(*args, **kwargs):
            await original(*args, **kwargs)
            self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '同步设置期间的另一版计划'
        self.t.client.update_goal_thread_settings = race_settings
        result = await self.actions.implement_plan('conversation-1', command_id='sync-race', **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual('plan', [v for n, v in self.t.client.calls if n == 'goal/settings'][-1]['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))

    async def test_late_goal_skill_race_restores_mode_and_failed_restoration_is_unknown(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change(status='paused')
        client = self.t.client
        original_skills, original_settings = client.resolve_skill_inputs, client.update_goal_thread_settings
        async def race_skills(*args, **kwargs):
            self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '解析技能期间更新的另一版计划'
            return await original_skills(*args, **kwargs)
        client.resolve_skill_inputs = race_skills
        result = await self.actions.implement_plan('conversation-1', command_id='late-goal-skills', skills=['late-skill'], **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual('plan', [v for n, v in client.calls if n == 'goal/settings'][-1]['collaboration_mode'])
        self.t.factory.threads['thread-1']['turns'][0]['items'][0]['text'] = '修改功能并通过测试'
        async def lose_restore(*args, **kwargs):
            await original_settings(*args, **kwargs)
            if kwargs['collaboration_mode'] == 'plan':
                raise AppServerDisconnected('restoration receipt lost')
        client.update_goal_thread_settings = lose_restore
        result = await self.actions.implement_plan('conversation-1', command_id='late-goal-restore-lost', skills=['late-skill'], **binding)
        self.assertEqual('delivery_unknown', result['command']['state'])
        self.assertEqual('plan_mode_restore_unknown', result['command']['reason'])
        self.assertEqual('needs_user', self.t.store.get_conversation('conversation-1')['state'])
        count = len(client.calls)
        await self.actions.implement_plan('conversation-1', command_id='late-goal-restore-lost', skills=['late-skill'], **binding)
        self.assertEqual(count, len(client.calls), '恢复结果未知也不重发实施确认')
        self.assertEqual(1, len([n for n, _ in client.calls if n == 'turn/start']))

    async def test_legacy_queued_refinement_prevents_confirmation_of_old_plan(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change()
        # A previous Web version could leave this unsent user refinement
        # queued in an idle Goal gap. Approving the displayed older plan
        # must not bypass that durable input, even before recovery drains it.
        self.t.store.begin_command('conversation-1', 'revise-before-approval', kind='steer',
            text='先修改计划，不要实施')
        self.t.store.update_command('revise-before-approval', 'queued', reason='native_goal_next_turn')
        result = await self.actions.implement_plan('conversation-1', command_id='stale-queued-approve', **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan_confirmation_stale', result['command']['reason'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(1, len([n for n, _ in self.t.client.calls if n == 'turn/start']))

    async def test_idle_goal_refinement_is_delivered_without_approving_old_plan(self):
        self.t.store.update_settings('conversation-1', {'collaboration_mode': 'plan'})
        await self.t.start()
        binding = await finish_plan(self.t)
        await self.t.change()
        result = await self.t.runtime.send_message('conversation-1', command_id='revise-before-approval',
            text='先修改计划，不要实施')
        self.assertEqual('accepted', result['command']['state'])
        starts = [v for n, v in self.t.client.calls if n == 'turn/start']
        self.assertEqual(2, len(starts))
        self.assertEqual('revise-before-approval', starts[-1]['client_user_message_id'])
        self.assertEqual('plan', starts[-1]['options']['collaborationMode']['mode'])
        result = await self.actions.implement_plan('conversation-1', command_id='stale-plan-approve', **binding)
        self.assertEqual('rejected', result['command']['state'])
        self.assertEqual('plan', self.t.store.get_conversation('conversation-1')['settings']['collaboration_mode'])
        self.assertEqual(2, len([n for n, _ in self.t.client.calls if n == 'turn/start']))

    async def test_goal_and_plan_http_auth_origin_and_read_only_receipts(self):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from middleware.request_origin import RequestOriginMiddleware
        from routes import conversation_api
        from shared import config
        from test_request_origin_middleware import call, scope, status, ORIGIN
        app = FastAPI()
        app.include_router(conversation_api.router, prefix='/workbench')
        app.add_middleware(RequestOriginMiddleware, router=app.router)
        headers = [(b'content-type', b'application/x-www-form-urlencoded')]
        def payload(messages):
            return json.loads(b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
        with mock.patch.object(config, 'PASSKEY_ORIGIN', ORIGIN), mock.patch.object(conversation_api, 'conversation_service', return_value=self.t.service), mock.patch.object(conversation_api, '_auth', return_value=None) as auth, mock.patch.object(conversation_api, 'audit'):
            path = '/workbench/conversation/conversation-1/goal/start'
            body = urlencode({'command_id': 'http-goal-start', 'objective': '手机目标', 'goal_version': ''}).encode()
            messages, _ = await call(app, scope(path, headers=headers), body)
            self.assertEqual(200, status(messages))
            self.assertEqual('accepted', payload(messages)['command']['state'])
            messages, _ = await call(app, scope('/workbench/conversation/conversation-1/actions/http-goal-start', method='GET'), b'')
            self.assertEqual('accepted', payload(messages)['command']['state'])
            messages, _ = await call(app, scope('/workbench/conversation/foreign/actions/http-goal-start', method='GET'), b'')
            self.assertEqual(404, status(messages))
            count = auth.call_count
            messages, received = await call(app, scope(path, headers=headers, origin='https://evil.test'), body)
            self.assertEqual(403, status(messages))
            self.assertEqual([], received)
            self.assertEqual(count, auth.call_count)
            auth.return_value = JSONResponse({'error': 'authentication_required'}, 401)
            messages, _ = await call(app, scope('/workbench/conversation/conversation-1/plan/implement', headers=headers), urlencode({'command_id': 'unauth-plan', 'turn_id': 'turn-1', 'item_id': 'plan', 'plan_digest': '0' * 64}).encode())
            self.assertEqual(401, status(messages))
            self.assertFalse(any(c['id'] == 'unauth-plan' for c in self.t.store.snapshot('conversation-1')['commands']))
