"""ASGI boundary tests: no handler/body/WS side effects on rejected origins."""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI, Request, WebSocket
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from middleware.request_origin import RequestOriginMiddleware
from shared import config
from shared.request_origin import require_same_https_origin
from shared.web_push import canonical_https_origin


ORIGIN = 'https://workbench.test:18443'


def scope(path='/change', *, method='POST', origin=ORIGIN, kind='http', headers=(), scheme='http'):
    values = [(b'host', b'workbench.test'), *headers]
    if origin is not None:
        values.append((b'origin', origin.encode()))
    return {'type': kind, 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': method, 'scheme': scheme, 'path': path, 'raw_path': path.encode(),
            'root_path': '', 'query_string': b'', 'headers': values,
            'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 18080),
            'subprotocols': []}


async def call(app, request_scope, body=b'{}'):
    messages, received = [], []
    complete = asyncio.Event()
    async def receive():
        if not received:
            received.append(True)
            if request_scope['type'] == 'websocket':
                return {'type': 'websocket.connect'}
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await complete.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        messages.append(message)
        if message['type'] == 'http.response.body' and not message.get('more_body'):
            complete.set()
    await asyncio.wait_for(app(request_scope, receive, send), timeout=5)
    return messages, received


def status(messages):
    return next(m['status'] for m in messages if m['type'] == 'http.response.start')


class OriginBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patch = mock.patch.object(config, 'PASSKEY_ORIGIN', ORIGIN)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.effects = []
        self.app = FastAPI()
        self.app.add_middleware(RequestOriginMiddleware, router=self.app.router)
        @self.app.api_route('/change', methods=['POST', 'PUT', 'PATCH', 'DELETE', 'TRACE'])
        async def change(request: Request):
            self.effects.append(await request.body())
            return JSONResponse({'ok': True})
        @self.app.get('/quick')
        @self.app.get('/read')
        async def read():
            self.effects.append('read')
            return JSONResponse({'ok': True})
        @self.app.websocket('/ws')
        async def socket(ws: WebSocket):
            await ws.accept()
            self.effects.append('ws')
            await ws.close()

    async def test_unsafe_methods_reject_missing_malformed_same_site_wrong_port_before_body(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'TRACE'):
            for origin in (None, '', 'null', 'http://workbench.test:18443',
                           'https://workbench.test', 'https://workbench.test:18444',
                           'https://evil.test', ORIGIN+'/path', ORIGIN+'?x=1',
                           'https://user@workbench.test:18443', ORIGIN+' ', ORIGIN+'\\x'):
                with self.subTest(method=method, origin=origin):
                    messages, received = await call(self.app, scope(method=method, origin=origin))
                    self.assertEqual(status(messages), 403)
                    self.assertEqual(received, [])
                    self.assertFalse(self.effects)
                    self.assertIn((b'cache-control', b'no-store'), messages[0]['headers'])

    async def test_configured_origin_accepts_proxy_host_loss_and_blocks_spoofing_and_duplicates(self):
        messages, _ = await call(self.app, scope(headers=[(b'x-forwarded-host', b'evil.test')]))
        self.assertEqual(status(messages), 200)
        self.assertEqual(self.effects, [b'{}'])
        self.effects.clear()
        for origin, extra in [(ORIGIN, [(b'origin', ORIGIN.encode())]),
                              ('https://evil.test', [(b'x-forwarded-host', b'workbench.test:18443'),
                                                     (b'x-forwarded-proto', b'https')])]:
            messages, _ = await call(self.app, scope(origin=origin, headers=extra))
            self.assertEqual(status(messages), 403)
            self.assertFalse(self.effects)

    async def test_referer_fallback_only_for_forms_never_overrides_origin(self):
        for content_type in ('application/x-www-form-urlencoded', 'multipart/form-data; boundary=x'):
            messages, _ = await call(self.app, scope(origin=None, headers=[
                (b'content-type', content_type.encode()), (b'referer', (ORIGIN+'/login/legacy?return_to=x').encode())]))
            self.assertEqual(status(messages), 200)
        self.effects.clear()
        for origin, content_type, referer in [(None, 'application/json', ORIGIN+'/'),
                    ('null', 'application/x-www-form-urlencoded', ORIGIN+'/'),
                    (None, 'multipart/form-data', 'https://workbench.test:18444/form'),
                    (None, 'application/x-www-form-urlencoded', 'https://user@workbench.test:18443/form')]:
            messages, received = await call(self.app, scope(origin=origin, headers=[
                (b'content-type', content_type.encode()), (b'referer', referer.encode())]))
            self.assertEqual(status(messages), 403)
            self.assertFalse(received)
            self.assertFalse(self.effects)

    async def test_ws_rejects_before_accept_and_registration_then_accepts_correct_origin(self):
        for origin in (None, '', 'null', 'https://workbench.test:18444', 'https://evil.test'):
            messages, received = await call(self.app, scope('/ws', kind='websocket', origin=origin,
                                                          headers=[(b'referer', ORIGIN.encode())]))
            self.assertEqual(messages, [{'type': 'websocket.close', 'code': 4403}])
            self.assertFalse(received)
            self.assertFalse(self.effects)
        messages, _ = await call(self.app, scope('/ws', kind='websocket'))
        self.assertEqual(messages[0]['type'], 'websocket.accept')
        self.assertEqual(self.effects, ['ws'])

    async def test_read_navigation_unchanged_but_draft_get_requires_navigation_proof(self):
        messages, _ = await call(self.app, scope('/read', method='GET', origin=None))
        self.assertEqual(status(messages), 200)
        for site, expected in [('none', 200), ('same-origin', 200), ('same-site', 403), ('cross-site', 403), ('', 403)]:
            self.effects.clear()
            messages, _ = await call(self.app, scope('/quick', method='GET', origin=None,
                                    headers=[(b'sec-fetch-site', site.encode())]))
            self.assertEqual(status(messages), expected)
            self.assertEqual(bool(self.effects), expected == 200)
        messages, _ = await call(self.app, scope('/quick', method='GET', origin=None,
                                headers=[(b'referer', (ORIGIN+'/home').encode())]))
        self.assertEqual(status(messages), 200)

    async def test_privacy_native_form_accepts_only_full_browser_same_origin_proof(self):
        headers = [(b'content-type', b'application/x-www-form-urlencoded'),
                   (b'sec-fetch-site', b'same-origin'), (b'sec-fetch-mode', b'navigate'),
                   (b'sec-fetch-dest', b'document')]
        for origin in (None, 'null'):
            messages, _ = await call(self.app, scope(origin=origin, headers=headers), b'field=value')
            self.assertEqual(status(messages), 200)
        for destination in ('iframe', 'empty'):
            forwarded = [(k, destination.encode() if k == b'sec-fetch-dest' else v) for k,v in headers]
            messages,_=await call(self.app,scope(origin='null',headers=forwarded),b'field=value')
            self.assertEqual(status(messages),200)
        self.effects.clear()
        invalid = [
            ('sec-fetch-site', 'same-site'), ('sec-fetch-site', 'cross-site'), ('sec-fetch-site', 'none'),
            ('sec-fetch-site', ''), ('sec-fetch-mode', 'cors'), ('sec-fetch-mode', 'same-origin'),
            ('sec-fetch-dest', 'script'), ('sec-fetch-dest', ''), ('content-type', 'application/json'),
        ]
        for name, value in invalid:
            with self.subTest(name=name, value=value):
                altered=[(k, value.encode() if k == name.encode() else v) for k,v in headers]
                messages, received=await call(self.app, scope(origin='null', headers=altered))
                self.assertEqual(status(messages), 403)
                self.assertFalse(received)
                self.assertFalse(self.effects)
        for name,value in headers + [(b'origin', b'null'), (b'host', b'workbench.test')]:
            messages, received=await call(self.app, scope(origin='null', headers=[*headers,(name,value)]))
            self.assertEqual(status(messages), 403)
            self.assertFalse(received)
        for origin in ('', 'NULL', 'https://evil.test', 'https://workbench.test:18444'):
            messages,_=await call(self.app, scope(origin=origin, headers=headers))
            self.assertEqual(status(messages), 403)
        with mock.patch.object(config, 'PASSKEY_ORIGIN', 'http://workbench.test'):
            messages,_=await call(self.app, scope(origin='null', headers=headers))
            self.assertEqual(status(messages), 403)
        request_scope=scope(origin='null',headers=headers)
        request_scope['headers'][0]=(b'host',b'other.test')
        messages,_=await call(self.app,request_scope)
        self.assertEqual(status(messages),403)

    async def test_privacy_metadata_never_authenticates_an_unauthorized_request(self):
        from routes import conversations
        app=FastAPI()
        app.add_middleware(RequestOriginMiddleware,router=app.router)
        app.include_router(conversations.router)
        headers=[(b'content-type',b'application/x-www-form-urlencoded'),
                 (b'sec-fetch-site',b'same-origin'),(b'sec-fetch-mode',b'navigate'),
                 (b'sec-fetch-dest',b'document')]
        from starlette.responses import PlainTextResponse
        with mock.patch.object(conversations,'require_auth',return_value=PlainTextResponse('auth required',403)), \
                mock.patch.object(conversations,'conversation_service') as service:
            messages,_=await call(app,scope('/conversation/new',origin='null',headers=headers),b'prompt=fixture')
            self.assertEqual(status(messages),403)
            self.assertEqual(b''.join(m.get('body',b'') for m in messages),b'auth required')
            service.assert_not_called()

    async def test_retired_handler_only_exemption_does_not_cover_live_business_or_future_routes(self):
        from routes.legacy_employee import router
        @self.app.post('/operations/future')
        def future():
            self.effects.append('future')
        self.app.include_router(router)
        for path in ('/upload', '/upload/path', '/operations', '/operations/retired'):
            messages, received = await call(self.app, scope(path, origin=None))
            self.assertEqual(status(messages), 410)
            self.assertFalse(received)
        messages, received = await call(self.app, scope('/operations/future', origin=None))
        self.assertEqual(status(messages), 403)
        self.assertFalse(received)
        self.assertFalse(self.effects)

    async def test_real_app_all_current_live_post_routes_are_guarded(self):
        import app
        from routes.legacy_employee import retired_employee_portal
        # Enumerate the actual route table, not a hard-coded representative list.
        covered = 0
        def routes(router):
            for route in router.routes:
                nested = getattr(route, 'original_router', None)
                if nested is not None:
                    yield from routes(nested)
                else:
                    yield route
        for route in routes(app.app.router):
            if 'POST' not in getattr(route, 'methods', ()) or route.endpoint is retired_employee_portal:
                continue
            path = route.path
            import re
            path = re.sub(r'\{[^}]+\}', 'fixture', path)
            request_scope = scope(path, origin='https://workbench.test:18444')
            messages, _ = await call(app.app, request_scope, body=b'not even valid multipart/json')
            self.assertEqual(status(messages), 403, path)
            self.assertIn(b'origin_rejected', b''.join(m.get('body', b'') for m in messages))
            covered += 1
        self.assertGreater(covered, 40)

    async def test_correct_ws_origin_still_requires_real_endpoint_authentication(self):
        from routes import ws
        app = FastAPI()
        app.add_middleware(RequestOriginMiddleware, router=app.router)
        app.include_router(ws.router)
        with mock.patch.object(ws, 'ENABLED_FILE', SimpleNamespace(exists=lambda: True)), \
                mock.patch.object(ws, 'logged_in', return_value=False) as auth, \
                mock.patch.object(ws, 'audit'):
            messages, _ = await call(app, scope('/ws', kind='websocket', origin=None))
            self.assertEqual(messages, [{'type': 'websocket.close', 'code': 4403}])
            auth.assert_not_called()
            messages, _ = await call(app, scope('/ws', kind='websocket'))
            self.assertEqual(messages[0]['type'], 'websocket.close')
            self.assertEqual(messages[0]['code'], 4401)
            self.assertFalse(any(m['type'] == 'websocket.accept' for m in messages))
            auth.assert_called_once()

    async def test_emergency_referer_only_form_and_legacy_logout_keep_auth_contracts(self):
        from routes import auth
        from shared import login_recovery, passkey_auth
        app = FastAPI()
        app.add_middleware(RequestOriginMiddleware, router=app.router)
        app.include_router(auth.router)
        headers = [(b'content-type', b'application/x-www-form-urlencoded'),
                   (b'referer', (ORIGIN+'/login/legacy').encode())]
        with mock.patch.object(auth, 'ENABLED_FILE', SimpleNamespace(exists=lambda: True)), \
                mock.patch.object(login_recovery, 'PASSKEY_ORIGIN', ORIGIN), \
                mock.patch.object(auth, 'load_accounts', return_value={}), \
                mock.patch.object(auth, '_handle_admin_login', return_value=JSONResponse({'ok': True})) as login:
            messages, _ = await call(app, scope('/login/legacy', origin=None, headers=headers), body=b'code=123456')
            self.assertEqual(status(messages), 200)
            self.assertEqual(login.call_args.args[1], '123456')
            privacy=[(b'content-type',b'application/x-www-form-urlencoded'),
                     (b'sec-fetch-site',b'same-origin'),(b'sec-fetch-mode',b'navigate'),
                     (b'sec-fetch-dest',b'document')]
            messages,_=await call(app,scope('/login/legacy',origin='null',headers=privacy),b'code=123456')
            self.assertEqual(status(messages),200)
            login.reset_mock()
            messages, _ = await call(app, scope('/login/legacy', origin='null', headers=headers), body=b'code=123456')
            self.assertEqual(status(messages), 403)
            login.assert_not_called()
        with mock.patch.object(auth, 'logged_in', return_value='fixture-admin'), \
                mock.patch.object(passkey_auth, 'session_id_from_request', return_value=None), \
                mock.patch.object(passkey_auth, 'revoke_request_session', return_value=None) as revoke, \
                mock.patch.object(auth, 'audit'):
            messages, _ = await call(app, scope('/logout'))
            self.assertEqual(status(messages), 303)
            revoke.assert_called_once()
            revoke.reset_mock()
            messages, _ = await call(app, scope('/logout', origin=None))
            self.assertEqual(status(messages), 403)
            revoke.assert_not_called()



class CanonicalOriginTests(unittest.TestCase):
    def test_https_and_wss_fallback_exact_ports_and_config_fail_closed(self):
        with mock.patch.object(config, 'PASSKEY_ORIGIN', ''):
            for kind, scheme in [('http', 'https'), ('websocket', 'wss')]:
                request_scope = scope(kind=kind, scheme=scheme)
                request_scope['headers'][0] = (b'host', b'workbench.test:18443')
                self.assertEqual(require_same_https_origin(HTTPConnection(request_scope)), ORIGIN)
            for scheme in ('http', 'ws'):
                with self.assertRaises(ValueError):
                    require_same_https_origin(HTTPConnection(scope(scheme=scheme)))
        for invalid in ('not-an-origin', 'http://workbench.test', ORIGIN+'/path'):
            with mock.patch.object(config, 'PASSKEY_ORIGIN', invalid), self.assertRaises(ValueError):
                require_same_https_origin(HTTPConnection(scope()))

    def test_canonical_rejects_ambiguous_hosts_and_retains_ipv6_and_default_port(self):
        self.assertEqual(canonical_https_origin('https://WORKBENCH.test:443/'), 'https://workbench.test')
        self.assertEqual(canonical_https_origin('https://[::1]:18443'), 'https://[::1]:18443')
        for value in (' https://workbench.test', 'https://work bench.test', 'https://workbench.test:',
                      'https://workbench.test:0', 'https://workbench.test:65536', 'https://workbench.test\x7f',
                      'https://workbench.test\\@evil.test', 'https://workbench.test,https://evil.test'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_https_origin(value)
