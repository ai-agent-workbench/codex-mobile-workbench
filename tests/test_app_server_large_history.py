"""Long conversation recovery retains a finite, tested transport ceiling."""
import unittest
from unittest import mock

import websockets

from engine.codex_app_server import CodexAppServerClient, _MAX_WEBSOCKET_MESSAGE_BYTES
from tests.test_codex_app_server_protocol_20260822 import FakeUnixAppServer


class LargeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_receives_history_larger_than_previous_ceiling(self):
        async def responder(socket, message):
            if message.get('method') == 'fixture/large-history':
                return {'id': message['id'], 'result': {'history': 'x' * 34_241_188}}
            return await FakeUnixAppServer._default_responder(server, socket, message)

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            original = websockets.unix_connect

            def connect(*args, **kwargs):
                self.assertEqual(kwargs['max_size'], 64 * 1024 * 1024)
                self.assertEqual(kwargs['max_size'], _MAX_WEBSOCKET_MESSAGE_BYTES)
                return original(*args, **kwargs)

            try:
                with mock.patch('engine.codex_app_server.websockets.unix_connect', side_effect=connect):
                    await client.connect()
                value = await client.request('fixture/large-history', {})
                self.assertEqual(len(value['history']), 34_241_188)
                self.assertTrue(client.connected)
            finally:
                await client.close()
