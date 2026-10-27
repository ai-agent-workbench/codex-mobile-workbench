"""0.159.2 bounded full-history hydration through item pages."""

import asyncio
import unittest
from unittest import mock

from engine.codex_app_server import (
    AppServerDisconnected,
    AppServerProtocolError,
    CodexAppServerClient,
    PROTOCOL_FIXTURE_0_156_0,
)
from tests.test_codex_app_server_protocol_20260822 import (
    FakeUnixAppServer,
    INITIALIZE_RESULT,
)


class PagedHistory159Tests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _initialize(version='0.159.2'):
        return dict(
            INITIALIZE_RESULT,
            userAgent=f'codex_app_server/{version}',
        )

    @staticmethod
    def _turn(turn_id):
        return {
            'id': turn_id,
            'status': 'completed',
            'items': [],
            'itemsView': 'notLoaded',
        }

    @staticmethod
    def _item(item_id, *, client_id=None):
        item = {'id': item_id, 'type': 'agentMessage', 'text': item_id}
        if client_id is not None:
            item = {
                'id': item_id,
                'type': 'userMessage',
                'clientId': client_id,
                'content': [{'type': 'input_text', 'text': 'original client ID'}],
            }
        return item

    async def test_resume_is_metadata_only_and_read_reassembles_all_item_pages(self):
        turn_one_items = [
            self._item(f'item-{number}', client_id='browser-message-7')
            if number == 0 else self._item(f'item-{number}')
            for number in range(11)
        ]
        # Item IDs are scoped to a turn, so the same ID is valid here too.
        turn_two_items = [self._item('item-0'), self._item('item-12')]

        async def responder(_socket, message):
            method = message.get('method')
            if method == 'initialize':
                return {'id': message['id'], 'result': self._initialize()}
            if method == 'thread/resume':
                self.assertTrue(message['params']['excludeTurns'])
                return {
                    'id': message['id'],
                    'result': {'thread': {'id': 'thread-history', 'cwd': '/tmp'}},
                }
            if method == 'thread/read':
                self.assertEqual(
                    message['params'],
                    {'threadId': 'thread-history', 'includeTurns': False},
                )
                return {
                    'id': message['id'],
                    'result': {'thread': {'id': 'thread-history', 'turns': []}},
                }
            if method == 'thread/turns/list':
                self.assertEqual(message['params'], {
                    'threadId': 'thread-history',
                    'limit': 100,
                    'sortDirection': 'asc',
                    'itemsView': 'notLoaded',
                })
                return {
                    'id': message['id'],
                    'result': {'data': [self._turn('turn-one'), self._turn('turn-two')]},
                }
            if method == 'thread/items/list':
                params = message['params']
                self.assertEqual(params['threadId'], 'thread-history')
                self.assertEqual(params['limit'], 10)
                self.assertEqual(params['sortDirection'], 'asc')
                if params['turnId'] == 'turn-one':
                    if 'cursor' not in params:
                        data, next_cursor = turn_one_items[:10], 'turn-one-page-2'
                    else:
                        self.assertEqual(params['cursor'], 'turn-one-page-2')
                        data, next_cursor = turn_one_items[10:], None
                else:
                    self.assertEqual(params['turnId'], 'turn-two')
                    self.assertNotIn('cursor', params)
                    data, next_cursor = turn_two_items, None
                return {
                    'id': message['id'],
                    'result': {
                        'data': [
                            {'turnId': params['turnId'], 'item': item} for item in data
                        ],
                        'nextCursor': next_cursor,
                    },
                }
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            resumed = await client.resume_thread('thread-history')
            hydrated = await client.read_thread('thread-history')
            await client.close()

        self.assertNotIn('turns', resumed['thread'])
        self.assertEqual(
            [[item['id'] for item in turn['items']]
             for turn in hydrated['thread']['turns']],
            [[f'item-{number}' for number in range(11)], ['item-0', 'item-12']],
        )
        self.assertEqual(hydrated['thread']['turns'][0]['itemsView'], 'full')
        self.assertEqual(
            hydrated['thread']['turns'][0]['items'][0]['clientId'],
            'browser-message-7',
        )
        methods = [message.get('method') for message in server.received]
        self.assertEqual(methods, [
            'initialize', 'initialized', 'thread/resume', 'thread/read',
            'thread/turns/list', 'thread/items/list', 'thread/items/list',
            'thread/items/list', 'thread/read', 'thread/turns/list',
        ])
        self.assertFalse({'turn/start', 'turn/steer'} & set(methods))

    async def test_malformed_item_pages_fail_closed(self):
        cases = {
            'missing': (
                [{'turnId': 'turn-one'}],
                None,
                'missing schema fields: item',
            ),
            'null-item': (
                [{'turnId': 'turn-one', 'item': None}],
                None,
                'entry item must be an object',
            ),
            'missing-item-type': (
                [{'turnId': 'turn-one', 'item': {'id': 'item-one'}}],
                None,
                'item type is invalid',
            ),
            'cross-turn': (
                [{'turnId': 'turn-other', 'item': self._item('item-one')}],
                None,
                'another turn',
            ),
        }
        for name, (data, next_cursor, message) in cases.items():
            with self.subTest(name=name):
                async def responder(_socket, request):
                    method = request.get('method')
                    if method == 'initialize':
                        return {'id': request['id'], 'result': self._initialize()}
                    if method == 'thread/read':
                        return {
                            'id': request['id'],
                            'result': {'thread': {'id': 'thread-history', 'turns': []}},
                        }
                    if method == 'thread/turns/list':
                        return {
                            'id': request['id'],
                            'result': {'data': [self._turn('turn-one')]},
                        }
                    if method == 'thread/items/list':
                        return {
                            'id': request['id'],
                            'result': {'data': data, 'nextCursor': next_cursor},
                        }
                    return None

                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    await client.connect()
                    with self.assertRaisesRegex(AppServerProtocolError, message):
                        await client.read_thread('thread-history')
                    await client.close()
                self.assertNotIn('turn/start', [x.get('method') for x in server.received])
                self.assertNotIn('turn/steer', [x.get('method') for x in server.received])

    async def test_repeated_item_and_cursor_fail_closed(self):
        for name, pages, message in (
            ('item', [
                ([self._item('item-one')], 'page-two'),
                ([self._item('item-one')], None),
            ], 'repeated an item'),
            ('cursor', [
                ([], 'page-two'),
                ([], 'page-two'),
            ], 'repeated a cursor'),
        ):
            with self.subTest(name=name):
                async def responder(_socket, request):
                    method = request.get('method')
                    if method == 'initialize':
                        return {'id': request['id'], 'result': self._initialize()}
                    if method == 'thread/read':
                        return {
                            'id': request['id'],
                            'result': {'thread': {'id': 'thread-history', 'turns': []}},
                        }
                    if method == 'thread/turns/list':
                        return {
                            'id': request['id'],
                            'result': {'data': [self._turn('turn-one')]},
                        }
                    if method == 'thread/items/list':
                        index = 1 if request['params'].get('cursor') else 0
                        data, next_cursor = pages[index]
                        return {
                            'id': request['id'],
                            'result': {
                                'data': [
                                    {'turnId': 'turn-one', 'item': item}
                                    for item in data
                                ],
                                'nextCursor': next_cursor,
                            },
                        }
                    return None

                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    await client.connect()
                    with self.assertRaisesRegex(AppServerProtocolError, message):
                        await client.read_thread('thread-history')
                    await client.close()

    async def test_read_thread_item_uses_verified_anchor_and_notification_barrier(self):
        handled = []

        async def notification_handler(message):
            handled.append(message["method"])

        async def responder(_socket, message):
            method = message.get('method')
            if method == 'initialize':
                return {'id': message['id'], 'result': self._initialize()}
            if method == 'thread/items/list':
                self.assertEqual(message['params'], {
                    'threadId': 'thread-history', 'turnId': 'turn-one',
                    'limit': 1, 'sortDirection': 'asc',
                    'cursor': {'type': 'item', 'itemId': 'item-before'},
                })
                return {'id': message['id'], 'result': {'data': [{
                    'turnId': 'turn-one', 'item': self._item('item-target'),
                }]}}
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(
                server.path, notification_handler=notification_handler
            )
            await client.connect()
            await client._handle_raw(
                '{"method":"item/started","params":{}}', client.connection_epoch
            )
            await client._handle_raw(
                '{"method":"item/completed","params":{}}', client.connection_epoch
            )
            await client.flush_notifications()
            item = await client.read_thread_item(
                'thread-history', 'turn-one', 'item-target',
                predecessor_id='item-before',
            )
            await client.close()

        self.assertEqual(handled, ['item/started', 'item/completed'])
        self.assertEqual(item['id'], 'item-target')

    async def test_notification_barrier_fails_when_close_stops_busy_worker(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_message):
            entered.set()
            await release.wait()

        async def responder(_socket, message):
            if message.get('method') == 'initialize':
                return {'id': message['id'], 'result': self._initialize()}
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path, notification_handler=handler)
            await client.connect()
            await client._handle_raw(
                '{"method":"item/started","params":{}}', client.connection_epoch
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            barrier = asyncio.create_task(client.flush_notifications())
            await asyncio.sleep(0)
            await client.close()
            with self.assertRaises(AppServerDisconnected):
                await barrier
            release.set()

    async def test_full_queue_barrier_fails_promptly_on_close_or_disconnect(self):
        for action in ('close', 'disconnect'):
            with self.subTest(action=action):
                client = CodexAppServerClient('/unused', request_timeout=5)
                client._connected = True
                client._connection_epoch = 1
                client._notifications = asyncio.Queue(maxsize=1)
                client._notifications.put_nowait((1, {'method': 'blocked'}))
                barrier = asyncio.create_task(client.flush_notifications())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(barrier.done())
                if action == 'close':
                    await client.close()
                else:
                    client._mark_disconnected(None, connection_epoch=1, source='test')
                with self.assertRaises(AppServerDisconnected):
                    await asyncio.wait_for(barrier, timeout=0.5)
                self.assertFalse(client._notification_barriers)
                # No pending put survives to enqueue a stale success barrier.
                self.assertEqual(client._notifications.qsize(), 1)
                await client.close()

    async def test_read_thread_item_uses_old_fixture_full_history_fallback(self):
        async def responder(_socket, message):
            method = message.get('method')
            if method == 'initialize':
                return {'id': message['id'], 'result': self._initialize('0.156.0')}
            if method == 'thread/read':
                return {'id': message['id'], 'result': {
                    'thread': {'id': 'thread-old', 'turns': []},
                }}
            if method == 'thread/turns/list':
                return {'id': message['id'], 'result': {'data': [{
                    'id': 'turn-old', 'status': 'completed', 'itemsView': 'full',
                    'items': [self._item('item-old')],
                }]}}
            if method == 'thread/items/list':
                self.fail('old fixture must use full-history fallback')
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(
                server.path, fixture_path=PROTOCOL_FIXTURE_0_156_0
            )
            await client.connect()
            item = await client.read_thread_item('thread-old', 'turn-old', 'item-old')
            await client.close()

        self.assertEqual(item['id'], 'item-old')

    async def test_more_than_1000_items_are_reassembled_across_pages(self):
        item_page_calls = 0

        async def responder(_socket, request):
            nonlocal item_page_calls
            method = request.get('method')
            if method == 'initialize':
                return {'id': request['id'], 'result': self._initialize()}
            if method == 'thread/read':
                return {
                    'id': request['id'],
                    'result': {'thread': {'id': 'thread-long', 'turns': []}},
                }
            if method == 'thread/turns/list':
                return {
                    'id': request['id'],
                    'result': {'data': [self._turn('turn-long')]},
                }
            if method == 'thread/items/list':
                item_page_calls += 1
                start = int(request['params'].get('cursor', '0'))
                end = min(start + 10, 1001)
                return {
                    'id': request['id'],
                    'result': {
                        'data': [{
                            'turnId': 'turn-long', 'item': self._item(f'item-{number}'),
                        } for number in range(start, end)],
                        'nextCursor': str(end) if end < 1001 else None,
                    },
                }
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            value = await client.read_thread('thread-long')
            await client.close()

        self.assertEqual(item_page_calls, 101)
        self.assertEqual(len(value['thread']['turns'][0]['items']), 1001)
        self.assertEqual(value['thread']['turns'][0]['items'][-1]['id'], 'item-1000')

    async def test_item_page_bound_is_independent_and_fail_closed(self):
        item_page_calls = 0

        async def responder(_socket, request):
            nonlocal item_page_calls
            method = request.get('method')
            if method == 'initialize':
                return {'id': request['id'], 'result': self._initialize()}
            if method == 'thread/read':
                return {
                    'id': request['id'],
                    'result': {'thread': {'id': 'thread-history', 'turns': []}},
                }
            if method == 'thread/turns/list':
                return {
                    'id': request['id'],
                    'result': {'data': [self._turn('turn-one')]},
                }
            if method == 'thread/items/list':
                item_page_calls += 1
                return {
                    'id': request['id'],
                    'result': {'data': [], 'nextCursor': f'page-{item_page_calls}'},
                }
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with mock.patch('engine.codex_app_server._MAX_ITEM_PAGES', 2):
                with self.assertRaisesRegex(
                    AppServerProtocolError, 'thread/items/list pagination exceeded limit'
                ):
                    await client.read_thread('thread-history')
            await client.close()

        self.assertEqual(item_page_calls, 2)

    async def test_one_concurrent_change_rehydrates_then_continued_churn_fails_closed(self):
        for name, statuses, expected in (
            ('settles', ['inProgress', 'completed', 'completed'], None),
            ('keeps-changing', ['inProgress', 'completed', 'failed'],
             'thread changed while paginating history'),
        ):
            with self.subTest(name=name):
                turn_list_calls = 0
                item_list_calls = 0

                async def responder(_socket, request):
                    nonlocal turn_list_calls, item_list_calls
                    method = request.get('method')
                    if method == 'initialize':
                        return {'id': request['id'], 'result': self._initialize()}
                    if method == 'thread/read':
                        return {
                            'id': request['id'],
                            'result': {'thread': {
                                'id': 'thread-history', 'status': {'type': 'idle'},
                                'turns': [],
                            }},
                        }
                    if method == 'thread/turns/list':
                        status = statuses[turn_list_calls]
                        turn_list_calls += 1
                        turn = self._turn('turn-one')
                        turn['status'] = status
                        return {'id': request['id'], 'result': {'data': [turn]}}
                    if method == 'thread/items/list':
                        item_list_calls += 1
                        return {'id': request['id'], 'result': {'data': [{
                            'turnId': 'turn-one',
                            'item': self._item(f'item-{item_list_calls}'),
                        }]}}
                    return None

                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    await client.connect()
                    if expected:
                        with self.assertRaisesRegex(AppServerProtocolError, expected):
                            await client.read_thread('thread-history')
                    else:
                        value = await client.read_thread('thread-history')
                    await client.close()

                self.assertEqual(turn_list_calls, 3)
                if expected:
                    self.assertEqual(item_list_calls, 2)
                else:
                    self.assertEqual(item_list_calls, 2)
                    self.assertEqual(value['thread']['turns'][0]['status'], 'completed')
                    self.assertEqual(value['thread']['turns'][0]['items'][0]['id'], 'item-2')

    async def test_older_fixture_keeps_full_turn_pages_and_resume_parameters(self):
        async def responder(_socket, message):
            method = message.get('method')
            if method == 'initialize':
                return {'id': message['id'], 'result': self._initialize('0.156.0')}
            if method == 'thread/resume':
                self.assertNotIn('excludeTurns', message['params'])
                return {
                    'id': message['id'],
                    'result': {'thread': {'id': 'thread-old', 'cwd': '/tmp'}},
                }
            if method == 'thread/read':
                return {
                    'id': message['id'],
                    'result': {'thread': {'id': 'thread-old', 'turns': []}},
                }
            if method == 'thread/turns/list':
                self.assertEqual(message['params']['itemsView'], 'full')
                return {
                    'id': message['id'],
                    'result': {'data': [{
                        'id': 'turn-old',
                        'status': 'completed',
                        'items': [self._item('item-old')],
                        'itemsView': 'full',
                    }]},
                }
            if method == 'thread/items/list':
                self.fail('older fixture must not use thread/items/list')
            return None

        async with FakeUnixAppServer(responder) as server:
            client = CodexAppServerClient(
                server.path, fixture_path=PROTOCOL_FIXTURE_0_156_0
            )
            await client.connect()
            await client.resume_thread('thread-old')
            value = await client.read_thread('thread-old')
            await client.close()

        self.assertEqual(value['thread']['turns'][0]['items'][0]['id'], 'item-old')


if __name__ == '__main__':
    unittest.main()
