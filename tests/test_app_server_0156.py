"""Exact 0.156 negotiation and protected Unix rendezvous regression gates."""
import asyncio
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import websockets

from engine.codex_app_server import (
    AppServerError, AppServerProtocolError, CodexAppServerClient,
    PROTOCOL_FIXTURE_0_154_0, PROTOCOL_FIXTURE_0_156_0,
    PROTOCOL_FIXTURES,
)
from tests.test_codex_app_server_protocol_20260822 import (
    FakeUnixAppServer, INITIALIZE_RESULT, MODEL,
)


class Runtime156Tests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_hashes_and_previous_contract(self):
        self.assertIn(PROTOCOL_FIXTURE_0_156_0, PROTOCOL_FIXTURES)
        value = json.loads(PROTOCOL_FIXTURE_0_156_0.read_text())
        previous = json.loads(PROTOCOL_FIXTURE_0_154_0.read_text())
        self.assertEqual(value['serverVersion'], '0.156.0')
        self.assertEqual(value['schemaSha256'], 'eb1ba91bd0fab656523092f6ed7de3ea7aef278921a650f14dc871ae7dcfaf84')
        self.assertEqual(value['experimentalSchemaSha256'], '655adafa0ccea3d84f30bcbdc74e201fa14511c51e08d0cd024a0280daa8bc60')
        self.assertEqual(value['codexSha256'], '78a11f06e0a2dda42d13fba1d50dc62e8cbdb2d5f69789722f4d4d99b5cdbe30')
        self.assertEqual(value['codeModeHostSha256'], 'a5c727845f8418acfe5a3d0ff05ad892d76545e51834da817cf77d6d83bdeb18')
        self.assertEqual(value['methods'], previous['methods'])
        self.assertEqual(value['initialize'], previous['initialize'])

    async def test_exact_156_and_154_accepted_unknown_patch_rejected(self):
        for version in ('0.156.0', '0.154.0', '0.156.1', '0.156.0.99'):
            async def responder(_socket, message):
                if message.get('method') == 'initialize':
                    return {'id': message['id'], 'result': dict(INITIALIZE_RESULT, userAgent=f'codex_app_server/{version}')}
                if message.get('method') == 'model/list':
                    return {'id': message['id'], 'result': {'data': [dict(MODEL, model='gpt-6-sol')]}}
            with self.subTest(version=version):
                async with FakeUnixAppServer(responder) as server:
                    client = CodexAppServerClient(server.path)
                    try:
                        if version in ('0.156.0', '0.154.0'):
                            await client.connect()
                            self.assertEqual(client.server_version, version)
                            self.assertEqual((await client.list_models())['data'][0]['model'], 'gpt-6-sol')
                        else:
                            with self.assertRaises(AppServerProtocolError):
                                await client.connect()
                    finally:
                        await client.close()

    async def test_connect_uses_checked_physical_socket_even_if_alias_changes(self):
        directory = Path(f'/tmp/codex-daemon-{os.geteuid()}')
        directory.mkdir(mode=0o700, exist_ok=True)
        # Do not alter the shared daemon directory or any pre-existing sockets.
        info = directory.lstat()
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        with tempfile.TemporaryDirectory(prefix='test-c156-', dir='/tmp') as root:
            alias = Path(root) / 'app.sock'
            physical = directory / hashlib.sha256(os.fsencode(alias)).hexdigest()
            fake = FakeUnixAppServer()
            server = await websockets.unix_serve(fake._handler, str(physical))
            physical.chmod(0o600)
            alias.symlink_to(physical)
            original = websockets.unix_connect
            client = CodexAppServerClient(alias)
            def connect(*args, **kwargs):
                self.assertEqual(kwargs['path'], str(physical))
                alias.unlink()
                alias.symlink_to(Path(root) / 'untrusted')
                return original(*args, **kwargs)
            try:
                with mock.patch('engine.codex_app_server.websockets.unix_connect', side_effect=connect):
                    await client.connect()
                self.assertEqual((await client.list_models())['data'], [MODEL])
            finally:
                await client.close()
                server.close()
                await server.wait_closed()
                physical.unlink(missing_ok=True)
                fake.temporary.cleanup()

    def test_alias_security_rejects_unsafe_objects(self):
        uid = os.geteuid()
        alias = Path('/private-c156-test/app.sock')
        parent = alias.parent
        directory = Path(f'/tmp/codex-daemon-{uid}')
        physical = directory / hashlib.sha256(os.fsencode(alias)).hexdigest()
        def entry(mode, owner=uid):
            return mock.Mock(st_mode=mode, st_uid=owner)
        base = {
            alias: entry(stat.S_IFLNK | 0o777),
            parent: entry(stat.S_IFDIR | 0o700),
            directory: entry(stat.S_IFDIR | 0o700),
            physical: entry(stat.S_IFSOCK | 0o600),
        }
        cases = [
            ('alias owner', alias, entry(stat.S_IFLNK | 0o777, uid + 1)),
            ('parent permissions', parent, entry(stat.S_IFDIR | 0o755)),
            ('parent owner', parent, entry(stat.S_IFDIR | 0o700, uid + 1)),
            ('daemon permissions', directory, entry(stat.S_IFDIR | 0o750)),
            ('daemon owner', directory, entry(stat.S_IFDIR | 0o700, uid + 1)),
            ('daemon symlink', directory, entry(stat.S_IFLNK | 0o700)),
            ('target symlink', physical, entry(stat.S_IFLNK | 0o600)),
            ('target file', physical, entry(stat.S_IFREG | 0o600)),
            ('target permissions', physical, entry(stat.S_IFSOCK | 0o660)),
            ('target owner', physical, entry(stat.S_IFSOCK | 0o600, uid + 1)),
        ]
        for name, path, value in cases:
            entries = dict(base, **{})
            entries[path] = value
            with self.subTest(name=name), mock.patch.object(Path, 'lstat', lambda p: entries[p]), mock.patch.object(Path, 'resolve', return_value=parent), mock.patch('os.readlink', return_value=str(physical)):
                with self.assertRaises(AppServerError):
                    CodexAppServerClient(alias)._check_socket_security()
        with mock.patch.object(Path, 'lstat', lambda p: base[p]), mock.patch.object(Path, 'resolve', return_value=parent), mock.patch('os.readlink', return_value='/tmp/arbitrary.sock'):
            with self.assertRaisesRegex(AppServerError, 'target mismatch'):
                CodexAppServerClient(alias)._check_socket_security()
