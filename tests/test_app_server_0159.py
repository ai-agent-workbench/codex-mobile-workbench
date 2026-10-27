"""Exact App Server 0.159.2 negotiation and GPT-6.1 Sol catalog gates."""

import json
import unittest

from engine.codex_app_server import (
    AppServerProtocolError,
    CodexAppServerClient,
    PROTOCOL_FIXTURE_0_156_0,
    PROTOCOL_FIXTURE_0_159_2,
    PROTOCOL_FIXTURES,
)
from tests.test_codex_app_server_protocol_20260822 import (
    FakeUnixAppServer,
    INITIALIZE_RESULT,
    MODEL,
)


class Runtime159Tests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_hashes_and_previous_contract(self):
        value = json.loads(PROTOCOL_FIXTURE_0_159_2.read_text())
        previous = json.loads(PROTOCOL_FIXTURE_0_156_0.read_text())
        self.assertIn(PROTOCOL_FIXTURE_0_159_2, PROTOCOL_FIXTURES)
        self.assertEqual(value['serverVersion'], '0.159.2')
        self.assertEqual(value['schemaSha256'], '8683f6fd9fbb994fa2bfb704ab5b02c9890238fa507a4c0ff33ff45884cd0a2e')
        self.assertEqual(value['experimentalSchemaSha256'], '7243ba241962af92ca60581f1a81808ebda4212a800f8b205f54703bcfd508c5')
        self.assertEqual(value['codexSha256'], '1748767b230ebfc3d4ab7e4e254920d0c0ad9691fd8c11f190e7d44511a4a92e')
        self.assertEqual(value['codeModeHostSha256'], '5b2c075ac2380fa04d76d7313fbc044d29c8d0a0d0b9138415acd4610211ca03')
        expected_methods = dict(previous['methods'])
        expected_methods.update({
            'thread/name/set': {
                'params': ['name', 'threadId'],
                'responseEquals': {},
            },
            'thread/resume': {
                'params': ['config', 'excludeTurns', 'threadId'],
            },
            'thread/items/list': {
                'params': ['cursor', 'limit', 'sortDirection', 'threadId', 'turnId'],
                'responseRequired': ['data'],
                'entryRequired': ['item', 'turnId'],
                'paginationField': 'nextCursor',
            },
            'config/read': {
                'params': ['cwd', 'includeLayers'],
                'responseRequired': ['config', 'origins'],
            },
            'review/start': {
                'params': ['delivery', 'target', 'threadId'],
                'responseRequired': ['reviewThreadId', 'turn'],
                'targetTypes': ['uncommittedChanges', 'baseBranch', 'commit', 'custom'],
            },
        })
        self.assertEqual(value['methods'], expected_methods)
        self.assertEqual(value['initialize'], previous['initialize'])
        self.assertTrue(value['metadataResume'])
        self.assertTrue(value['itemPagination'])

    async def test_exact_159_and_156_accepted_unknown_patch_rejected(self):
        for version in ('0.159.2', '0.156.0', '0.159.1', '0.159.3'):
            async def responder(_socket, message):
                if message.get('method') == 'initialize':
                    return {'id': message['id'], 'result': dict(INITIALIZE_RESULT, userAgent=f'codex_app_server/{version}')}
                if message.get('method') == 'model/list':
                    model = dict(MODEL, id='gpt-6.1-sol', model='gpt-6.1-sol',
                                 displayName='GPT-6.1 Sol', defaultReasoningEffort='medium',
                                 supportedReasoningEfforts=[
                                     {'reasoningEffort': effort, 'description': effort}
                                     for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
                                 ])
                    return {'id': message['id'], 'result': {'data': [model]}}
            with self.subTest(version=version):
                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    try:
                        if version in ('0.159.2', '0.156.0'):
                            await client.connect()
                            self.assertEqual(client.server_version, version)
                            selected = await client.resolve_model_selection('gpt-6.1-sol', 'high')
                            self.assertEqual(selected['model'], 'gpt-6.1-sol')
                            self.assertEqual(selected['reasoning_effort'], 'high')
                            with self.assertRaises(AppServerProtocolError):
                                await client.resolve_model_selection('gpt-6.1-sol', 'none')
                        else:
                            with self.assertRaises(AppServerProtocolError):
                                await client.connect()
                    finally:
                        await client.close()


if __name__ == '__main__':
    unittest.main()
