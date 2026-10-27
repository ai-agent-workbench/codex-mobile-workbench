"""Origin checks retain the public port across the real gateway's Host rewrite."""
import unittest
from unittest import mock

from starlette.requests import Request

from routes import settings_center, web_push
from shared import config


def request(origin, *, host='workbench.test', scheme='https', extra=()):
    async def receive():
        return {'type': 'http.request', 'body': b'{}', 'more_body': False}
    return Request({
        'type': 'http', 'method': 'POST', 'scheme': scheme,
        'path': '/settings/api/config', 'root_path': '', 'query_string': b'',
        'server': ('127.0.0.1', 18080),
        'headers': [(b'host', host.encode()), (b'origin', origin.encode()),
                    (b'content-type', b'application/json'), *extra],
    }, receive)


class RequestOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_stripped_host_port_accepts_configured_origin(self):
        # The browser uses a non-default external port; nginx forwards $host.
        with mock.patch.object(config, 'PASSKEY_ORIGIN', 'https://workbench.test:18443'), \
                mock.patch.object(config, 'PASSKEY_ENABLED', False):
            self.assertEqual(await settings_center.body(request('https://workbench.test:18443')), {})
            self.assertEqual(web_push._change_origin(request('https://workbench.test:18443')),
                             'https://workbench.test:18443')

    async def test_configured_origin_rejects_wrong_port_and_spoofed_forwarding(self):
        with mock.patch.object(config, 'PASSKEY_ORIGIN', 'https://workbench.test:18443'):
            for origin in ('', 'null', 'https://workbench.test', 'https://workbench.test:18444',
                           'http://workbench.test:18443', 'https://evil.test:18443',
                           'https://workbench.test:18443/path', 'https://workbench.test:18443?x=1',
                           'https://user@workbench.test:18443'):
                with self.subTest(origin=origin):
                    headers = [(b'x-forwarded-host', origin.encode()), (b'x-forwarded-proto', b'https')]
                    with self.assertRaises(ValueError):
                        await settings_center.body(request(origin, extra=headers))
                    with self.assertRaises(ValueError):
                        web_push._change_origin(request(origin, extra=headers))

    async def test_direct_https_fallback_only_when_configuration_absent(self):
        with mock.patch.object(config, 'PASSKEY_ORIGIN', ''):
            self.assertEqual(await settings_center.body(request('https://workbench.test:18443', host='workbench.test:18443')), {})
            self.assertEqual(web_push._change_origin(request('https://workbench.test:18443', host='workbench.test:18443')), 'https://workbench.test:18443')
            for origin, scheme in [('https://workbench.test:18443', 'https'), ('http://workbench.test', 'http')]:
                with self.assertRaises(ValueError):
                    await settings_center.body(request(origin, scheme=scheme))
                with self.assertRaises(ValueError):
                    web_push._change_origin(request(origin, scheme=scheme))
        for invalid in ('http://workbench.test', 'https://workbench.test/path', 'not-an-origin'):
            with mock.patch.object(config, 'PASSKEY_ORIGIN', invalid):
                with self.assertRaises(ValueError):
                    await settings_center.body(request('https://workbench.test'))
                with self.assertRaises(ValueError):
                    web_push._change_origin(request('https://workbench.test'))
