"""Exact App Server 0.160.0 negotiation and GPT-6.1 Sol catalog gates."""

import json
import unittest

from engine.codex_app_server import (
    AppServerProtocolError,
    CodexAppServerClient,
    PROTOCOL_FIXTURE,
    PROTOCOL_FIXTURE_0_156_0,
    PROTOCOL_FIXTURE_0_159_2,
    PROTOCOL_FIXTURE_0_160_0,
    PROTOCOL_FIXTURES,
)
from tests.test_codex_app_server_protocol_20260822 import (
    FakeUnixAppServer,
    INITIALIZE_RESULT,
    MODEL,
)


class Runtime160Tests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_is_default_and_preserves_159_contract(self):
        self.assertEqual(PROTOCOL_FIXTURE, PROTOCOL_FIXTURE_0_160_0)
        self.assertIn(PROTOCOL_FIXTURE_0_160_0, PROTOCOL_FIXTURES)
        value = json.loads(PROTOCOL_FIXTURE_0_160_0.read_text())
        previous = json.loads(PROTOCOL_FIXTURE_0_159_2.read_text())
        self.assertEqual(value['cliVersion'], 'codex-cli 0.160.0')
        self.assertEqual(value['serverVersion'], '0.160.0')
        self.assertEqual(value['sourcePackage'], 'OpenAI npm @openai/codex 0.160.0-linux-x64')
        self.assertEqual(value['sourceOrigin'], 'OpenAI official npm package via existing Clash proxy, npm signature and dist SHA-512 verified')
        self.assertEqual(value['schemaSha256'], '8683f6fd9fbb994fa2bfb704ab5b02c9890238fa507a4c0ff33ff45884cd0a2e')
        self.assertEqual(value['experimentalSchemaSha256'], '7243ba241962af92ca60581f1a81808ebda4212a800f8b205f54703bcfd508c5')
        self.assertEqual(value['codexSha256'], '12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad')
        self.assertEqual(value['codeModeHostSha256'], '37cab1584302611e9936902219640ab5e7a79fcfccd2504c6e85ea8cb97d0e10')
        self.assertEqual({key: value['methods'][key] for key in previous['methods']}, previous['methods'])
        self.assertEqual(set(value['methods']) - set(previous['methods']), {
            'thread/list', 'thread/loaded/list', 'thread/backgroundTerminals/list', 'thread/backgroundTerminals/clean'})
        self.assertTrue(value['conversationTreeStop'])
        self.assertEqual(value['initialize'], previous['initialize'])
        self.assertEqual(value['metadataResume'], previous['metadataResume'])
        self.assertEqual(value['itemPagination'], previous['itemPagination'])
        self.assertEqual(
            value['requestUserInputDefaultModeConfig'],
            previous['requestUserInputDefaultModeConfig'],
        )

    async def test_exact_160_with_sol_and_registered_fallbacks_only(self):
        versions = (
            '0.160.0', '0.159.2', '0.156.0',
            '0.160.1', '0.161.0', '0.160.0-alpha.1',
        )
        accepted = {'0.160.0', '0.159.2', '0.156.0'}
        for version in versions:
            async def responder(_socket, message):
                if message.get('method') == 'initialize':
                    return {
                        'id': message['id'],
                        'result': dict(
                            INITIALIZE_RESULT,
                            userAgent=f'codex_app_server/{version}',
                        ),
                    }
                if message.get('method') == 'model/list':
                    model = dict(
                        MODEL,
                        id='gpt-6.1-sol',
                        model='gpt-6.1-sol',
                        displayName='GPT-6.1 Sol',
                        defaultReasoningEffort='medium',
                        supportedReasoningEfforts=[
                            {'reasoningEffort': effort, 'description': effort}
                            for effort in (
                                'low', 'medium', 'high', 'xhigh', 'max', 'ultra'
                            )
                        ],
                    )
                    return {'id': message['id'], 'result': {'data': [model]}}

            with self.subTest(version=version):
                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    try:
                        if version in accepted:
                            await client.connect()
                            self.assertEqual(client.server_version, version)
                            selected = await client.resolve_model_selection(
                                'gpt-6.1-sol', 'high'
                            )
                            self.assertEqual(selected['model'], 'gpt-6.1-sol')
                            self.assertEqual(selected['reasoning_effort'], 'high')
                        else:
                            with self.assertRaises(AppServerProtocolError):
                                await client.connect()
                    finally:
                        await client.close()


if __name__ == '__main__':
    unittest.main()
