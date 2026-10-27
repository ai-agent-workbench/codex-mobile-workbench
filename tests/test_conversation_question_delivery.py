"""Native input HTTP privacy/authentication and transcript regression gates."""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock
from starlette.requests import Request
from starlette.responses import JSONResponse
from engine.codex_app_server import AppServerProtocolError
from routes import conversation_api as api
from shared.conversation_export import export_markdown


class QuestionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, payload='{"choice":"A"}'):
        return await api.answer_conversation_question(
            Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}),
            'conversation-1', 'question-1', 'command-1', payload)

    async def test_unauthenticated_never_accesses_service(self):
        with mock.patch.object(api, '_auth', return_value=JSONResponse({}, status_code=401)), mock.patch.object(api, 'conversation_service') as service:
            self.assertEqual((await self.call()).status_code, 401)
            service.assert_not_called()

    async def test_invalid_json_is_rejected_before_service(self):
        with mock.patch.object(api, '_auth', return_value=None), mock.patch.object(api, 'conversation_service') as service:
            self.assertEqual((await self.call('{')).status_code, 400)
            service.assert_not_called()

    async def test_protocol_errors_are_sanitized_and_not_audited(self):
        service = mock.Mock()
        service.answer_question = mock.AsyncMock(side_effect=AppServerProtocolError('private-wire-secret'))
        with mock.patch.object(api, '_auth', return_value=None), mock.patch.object(api, 'conversation_service', return_value=service), mock.patch.object(api, 'audit') as audit:
            response = await self.call()
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(b'private-wire-secret', response.body)
            audit.assert_not_called()

    async def test_answer_is_not_in_audit_and_response_is_private(self):
        service = mock.Mock()
        service.answer_question = mock.AsyncMock()
        service.public_snapshot.return_value = {'questions': []}
        with mock.patch.object(api, '_auth', return_value=None), mock.patch.object(api, 'conversation_service', return_value=service), mock.patch.object(api, 'audit') as audit:
            response = await self.call('{"secret":"private-wire-secret"}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
            self.assertNotIn('private-wire-secret', str(audit.call_args))
            service.answer_question.assert_awaited_once_with('conversation-1', question_id='question-1', command_id='command-1', answers={'secret': 'private-wire-secret'})


class AsyncQuestionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, text='方案 A'):
        return await api.answer_async_conversation_question(
            Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}),
            'conversation-1', 'async-request-1', 'command-1', text,
        )

    async def test_unauthenticated_never_accesses_service(self):
        with mock.patch.object(api, '_auth', return_value=JSONResponse({}, status_code=401)), mock.patch.object(api, 'conversation_service') as service:
            self.assertEqual((await self.call()).status_code, 401)
            service.assert_not_called()

    async def test_protocol_error_is_sanitized_and_not_audited(self):
        service = mock.Mock()
        service.answer_async_question = mock.AsyncMock(
            side_effect=AppServerProtocolError('private-answer')
        )
        with mock.patch.object(api, '_auth', return_value=None), mock.patch.object(api, 'conversation_service', return_value=service), mock.patch.object(api, 'audit') as audit:
            response = await self.call('private-answer')
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(b'private-answer', response.body)
            audit.assert_not_called()

    async def test_answer_is_not_in_audit_and_response_is_private(self):
        service = mock.Mock()
        service.answer_async_question = mock.AsyncMock()
        service.public_snapshot.return_value = {'questions': []}
        with mock.patch.object(api, '_auth', return_value=None), mock.patch.object(api, 'conversation_service', return_value=service), mock.patch.object(api, 'audit') as audit:
            response = await self.call('private-answer')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
            self.assertNotIn('private-answer', str(audit.call_args))
            service.answer_async_question.assert_awaited_once_with(
                'conversation-1', request_id='async-request-1',
                command_id='command-1', answer_text='private-answer',
            )


class QuestionExportTests(unittest.TestCase):
    def render(self, state='answered', source='user'):
        return export_markdown({'conversation': {'title': '测试'}, 'turns': [{'id': 'turn-1', 'ordinal': 1, 'final_text': '最后结论'}], 'questions': [{
            'turn_id': 'turn-1', 'state': state, 'answer_source': source,
            'questions': [{'id': 'choice', 'question': '选择方案？', 'options': [{'label': 'A', 'description': '推荐'}]}, {'id': 'secret', 'question': '秘密？', 'isSecret': True}],
            'answers_masked': {'choice': 'A', 'secret': 'should-never-leak'},
        }]}, project_name='测试', exported_at=datetime.now(timezone.utc))

    def test_original_question_answer_and_final_survive_export(self):
        result = self.render()
        for value in ('选择方案？', '你的回答：A', '秘密回答已隐藏', '模型已继续', '最后结论'):
            self.assertIn(value, result)
        self.assertNotIn('should-never-leak', result)
        self.assertLess(result.index('中途提问', result.index('## 第')), result.index('最后结论'))

    def test_uncertain_delivery_never_claims_continuation(self):
        self.assertIn('送达状态无法确认', self.render('delivery_unknown'))
        self.assertNotIn('模型已继续', self.render('delivery_unknown'))
        self.assertIn('已按推荐项自动继续', self.render(source='deadline'))

    def test_system_automatic_reply_is_not_labelled_as_user(self):
        result = export_markdown({
            'conversation': {'title': '测试'},
            'turns': [{
                'id': 'turn-1', 'ordinal': 1, 'final_text': '最后结论',
                'commands': [{
                    'id': 'auto-1', 'kind': 'steer', 'origin': 'system',
                    'text': 'A', 'state': 'accepted',
                }],
            }],
        }, project_name='测试', exported_at=datetime.now(timezone.utc))
        self.assertIn('### 系统 · 自动继续', result)
