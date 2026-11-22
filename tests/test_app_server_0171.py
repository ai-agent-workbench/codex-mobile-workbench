"""Exact App Server 0.171.0 negotiation and rollback compatibility."""
import json
import unittest

from engine.codex_app_server import (
    AppServerProtocolError,
    PROTOCOL_FIXTURE,
    CodexAppServerClient,
    PROTOCOL_FIXTURE_0_160_0,
    PROTOCOL_FIXTURE_0_171_0,
    PROTOCOL_FIXTURES,
)
from tests.test_codex_app_server_protocol_20260822 import (
    FakeUnixAppServer,
    INITIALIZE_RESULT,
    MODEL,
)


class Runtime171Tests(unittest.IsolatedAsyncioTestCase):
    def test_verified_contract_preserves_160_methods(self):
        current = json.loads(PROTOCOL_FIXTURE_0_171_0.read_text())
        previous = json.loads(PROTOCOL_FIXTURE_0_160_0.read_text())
        self.assertIn(PROTOCOL_FIXTURE_0_171_0, PROTOCOL_FIXTURES)
        self.assertEqual(PROTOCOL_FIXTURE, PROTOCOL_FIXTURE_0_171_0)
        self.assertEqual(current['cliVersion'], 'codex-cli 0.171.0')
        self.assertEqual(current['serverVersion'], '0.171.0')
        self.assertEqual(
            current['sourcePackage'],
            'OpenAI npm @openai/codex 0.171.0-linux-x64',
        )
        self.assertEqual(
            current['sourceOrigin'],
            'OpenAI official npm package with npm signature and dist SHA-512/SHA-1 verified; official GitHub kit binary digests match',
        )
        self.assertEqual(current['schemaSha256'], '6207620bfd8e98a03a36330ed5e416b551086b91202f93a3759a937065bf2ca7')
        self.assertEqual(current['experimentalSchemaSha256'], 'fe3f0cc67ec5f581743c802ee6e1369c32328d24f91faf4c6f5c697935db642c')
        self.assertEqual(current['codexSha256'], 'b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0')
        self.assertEqual(current['codeModeHostSha256'], 'f49bd4f0c0cc73cf1ec3a719009e07ce5b6e100520d023c760cc203fb37e5c6f')
        self.assertEqual(current['methods']['thread/items/read'], {
            'params': ['itemIds', 'threadId', 'turnId'],
            'responseRequired': ['data'],
            'entryRequired': ['item', 'turnId'],
        })
        for method, contract in previous['methods'].items():
            self.assertIn(method, current['methods'])
            self.assertTrue(set(contract.get('params', [])) <= set(current['methods'][method].get('params', [])), method)
            for field, value in contract.items():
                if field != 'params':
                    self.assertEqual(value, current['methods'][method][field], (method, field))
        self.assertEqual(current['initialize'], previous['initialize'])

    async def test_candidate_and_rollback_negotiate_and_unknown_171_builds_fail(self):
        for version in ('0.171.0', '0.160.0', '0.171.1', '0.171.0-alpha.1'):
            async def responder(_socket, message):
                if message.get('method') == 'initialized':
                    return None
                if message.get('method') == 'initialize':
                    result = dict(INITIALIZE_RESULT, userAgent=f'codex_app_server/{version}')
                elif message.get('method') == 'model/list':
                    result = {'data': [MODEL]}
                else:
                    raise AssertionError(message.get('method'))
                return {'id': message['id'], 'result': result}

            with self.subTest(version=version):
                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    try:
                        if version in ('0.171.0', '0.160.0'):
                            await client.connect()
                            self.assertEqual(client.server_version, version)
                            self.assertEqual(
                                (await client.list_models())['data'][0]['id'],
                                MODEL['id'],
                            )
                        else:
                            with self.assertRaises(AppServerProtocolError):
                                await client.connect()
                    finally:
                        await client.close()

    async def test_exact_item_read_needs_no_predecessor_and_keeps_tool_output(self):
        items = {
            'command': {
                'id': 'command', 'type': 'commandExecution', 'status': 'completed',
                'aggregatedOutput': '完整命令输出',
            },
            'mcp': {
                'id': 'mcp', 'type': 'mcpToolCall', 'status': 'completed',
                'result': '完整 MCP 输出',
            },
        }

        async def responder(_socket, message):
            method = message.get('method')
            if method == 'initialized':
                return None
            if method == 'initialize':
                return {'id': message['id'], 'result': dict(
                    INITIALIZE_RESULT, userAgent='codex_app_server/0.171.0'
                )}
            if method == 'thread/items/read':
                self.assertEqual(message['params']['threadId'], 'thread-history')
                self.assertEqual(message['params']['turnId'], 'turn-history')
                item_id = message['params']['itemIds'][0]
                self.assertEqual(message['params']['itemIds'], [item_id])
                return {'id': message['id'], 'result': {'data': [{
                    'turnId': 'turn-history', 'item': items[item_id],
                }]}}
            self.fail(method)

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            command = await client.read_thread_item(
                'thread-history', 'turn-history', 'command'
            )
            mcp = await client.read_thread_item(
                'thread-history', 'turn-history', 'mcp'
            )
            await client.close()

        self.assertEqual(command['aggregatedOutput'], '完整命令输出')
        self.assertEqual(mcp['result'], '完整 MCP 输出')
        methods = [request['method'] for request in server.received]
        self.assertNotIn('thread/items/list', methods)

    async def test_exact_item_read_rejects_wrong_anchor_or_deferred_output(self):
        for name, entry, expected in (
            ('wrong-turn', {
                'turnId': 'other-turn',
                'item': {'id': 'item', 'type': 'commandExecution'},
            }, 'another turn'),
            ('deferred', {
                'turnId': 'turn-history', 'outputDeferred': True,
                'item': {'id': 'item', 'type': 'commandExecution'},
            }, 'complete item output'),
            ('missing-type', {
                'turnId': 'turn-history',
                'item': {'id': 'item'},
            }, 'item type is invalid'),
        ):
            with self.subTest(name=name):
                async def responder(_socket, message):
                    if message.get('method') == 'initialized':
                        return None
                    if message.get('method') == 'initialize':
                        return {'id': message['id'], 'result': dict(
                            INITIALIZE_RESULT, userAgent='codex_app_server/0.171.0'
                        )}
                    if message.get('method') == 'thread/items/read':
                        return {'id': message['id'], 'result': {'data': [entry]}}
                    self.fail(message.get('method'))

                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    await client.connect()
                    with self.assertRaisesRegex(AppServerProtocolError, expected):
                        await client.read_thread_item(
                            'thread-history', 'turn-history', 'item'
                        )
                    await client.close()

    async def test_rollback_0160_keeps_verified_cursor_item_read(self):
        async def responder(_socket, message):
            if message.get('method') == 'initialized':
                return None
            if message.get('method') == 'initialize':
                return {'id': message['id'], 'result': dict(
                    INITIALIZE_RESULT, userAgent='codex_app_server/0.160.0'
                )}
            if message.get('method') == 'thread/items/list':
                self.assertEqual(message['params'], {
                    'threadId': 'thread-history', 'turnId': 'turn-history',
                    'limit': 1, 'sortDirection': 'asc',
                    'cursor': {'type': 'item', 'itemId': 'before'},
                })
                return {'id': message['id'], 'result': {'data': [{
                    'turnId': 'turn-history',
                    'item': {'id': 'item', 'type': 'fileChange'},
                }]}}
            self.fail(message.get('method'))

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(
                server.path, fixture_path=PROTOCOL_FIXTURE_0_160_0
            )
            await client.connect()
            item = await client.read_thread_item(
                'thread-history', 'turn-history', 'item', predecessor_id='before'
            )
            await client.close()

        self.assertEqual(item['type'], 'fileChange')
        self.assertNotIn(
            'thread/items/read', [request['method'] for request in server.received]
        )

    async def test_scoped_native_title_search_never_accepts_or_requests_wider_threads(self):
        partitions = []
        async def responder(_socket, message):
            if message.get('method') == 'initialized':
                return None
            if message.get('method') == 'initialize':
                return {'id': message['id'], 'result': dict(
                    INITIALIZE_RESULT, userAgent='codex_app_server/0.171.0'
                )}
            if message.get('method') == 'thread/list':
                archived = message['params'].get('archived')
                partitions.append(archived)
                self.assertEqual(message['params'], {
                    'archived': archived, 'includedThreadIds': ['thread-a', 'thread-b'],
                    'currentAccountOnly': True, 'useStateDbOnly': True,
                    'searchTerm': '本地查询', 'limit': 100, 'sortDirection': 'asc',
                })
                return {'id': message['id'], 'result': {'data': [{
                    'id': 'thread-a' if archived else 'thread-b', 'parentThreadId': None,
                    'name': 'never expose this native title',
                }]}}
            self.fail(message.get('method'))

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertTrue(client.supports_scoped_thread_title_search)
            matches = await client.search_scoped_thread_titles(
                ['thread-a', 'thread-b'], '本地查询'
            )
            await client.close()
        self.assertEqual(partitions, [False, True])
        self.assertEqual(matches, {'thread-a', 'thread-b'})
        self.assertNotIn('never expose this native title', str(matches))

    async def test_scoped_native_title_search_rejects_native_thread_outside_allow_list(self):
        async def responder(_socket, message):
            if message.get('method') == 'initialized':
                return None
            if message.get('method') == 'initialize':
                return {'id': message['id'], 'result': dict(
                    INITIALIZE_RESULT, userAgent='codex_app_server/0.171.0'
                )}
            if message.get('method') == 'thread/list':
                return {'id': message['id'], 'result': {'data': [{
                    'id': 'thread-other', 'parentThreadId': None,
                }]}}
            self.fail(message.get('method'))

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with self.assertRaisesRegex(AppServerProtocolError, 'outside the allowed set'):
                await client.search_scoped_thread_titles(['thread-a'], '本地查询')
            await client.close()
