"""Empty-state and configured-path behavior, with no production credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import account_migration
from routes import auth
from tests.test_settings_center import request


class AccountBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.accounts = self.root / 'accounts.json'
        for name, value in {
            'STATE': self.root, 'ACCOUNTS_FILE': self.accounts,
            'SECRET_FILE': self.root / 'totp_secret', 'ENABLED_FILE': self.root / 'totp_enabled',
            'ACCOUNT': 'fixture-owner', 'MANAGED_INSTALL': False,
        }.items():
            patcher = mock.patch.object(account_migration, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_blank_state_does_not_create_disabled_fixed_identity(self):
        account_migration.migrate_accounts()
        self.assertFalse(self.accounts.exists())

    def test_real_legacy_identity_migrates_configured_account_without_overwrite(self):
        (self.root / 'totp_secret').write_text('fixture-secret')
        (self.root / 'totp_enabled').touch()
        account_migration.migrate_accounts()
        value = json.loads(self.accounts.read_text())
        self.assertEqual(value, {'fixture-owner': {'role': 'admin', 'totp': 'fixture-secret', 'enabled': True}})
        self.assertEqual(self.accounts.stat().st_mode & 0o777, 0o600)
        self.accounts.write_text('{"existing":{"enabled":false}}')
        original = self.accounts.read_bytes()
        account_migration.migrate_accounts()
        self.assertEqual(self.accounts.read_bytes(), original)

    def test_verified_legacy_first_setup_completes_account_creation(self):
        (self.root / 'totp_secret').write_text('fixture-secret')
        with (mock.patch.object(auth, 'ENABLED_FILE', self.root / 'totp_enabled'),
              mock.patch.object(auth, 'MANAGED_INSTALL', False),
              mock.patch.object(auth, 'totp', return_value=mock.Mock(verify=lambda *a, **k: True)),
              mock.patch.object(auth, 'audit')):
            self.assertEqual(auth.finish_setup(request(), '123456').status_code, 303)
        self.assertTrue(json.loads(self.accounts.read_text())['fixture-owner']['enabled'])

    def test_managed_install_does_not_allow_anonymous_web_setup(self):
        with (mock.patch.object(auth, 'MANAGED_INSTALL', True),
              mock.patch.object(auth, 'totp', side_effect=AssertionError('must not create or read secret'))):
            self.assertEqual(auth.setup().status_code, 403)
            self.assertEqual(auth.finish_setup(request(), '123456').status_code, 403)
        self.assertFalse(self.accounts.exists())


class PortableCoreStartupTests(unittest.TestCase):
    def test_clean_home_starts_actual_core_with_configured_paths_and_no_network_or_credentials(self):
        with tempfile.TemporaryDirectory(prefix='portable-core-') as temporary:
            root = Path(temporary)
            home, state = root / 'empty-home', root / 'state'
            home.mkdir()
            projects = root / 'projects'
            inbox = root / 'inbox'
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                   'CODEX_WEB_STATE_DIR': str(state), 'CODEX_WEB_PROJECTS_DIR': str(projects),
                   'CODEX_WEB_INBOX_DIR': str(inbox), 'CODEX_WEB_CONVERSATION_DIR': str(inbox / 'conversations'),
                   'CODEX_WEB_FILE_ROOTS': str(root), 'CODEX_WEB_FILE_GUARDED_PATHS': str(state),
                   'CODEX_WEB_MANAGED_INSTALL': '1', 'CODEX_WEB_ADMIN_ACCOUNT': 'new-owner',
                   'CODEX_WEB_AUTOMATIONS_ENABLED': '0', 'CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED': '0',
                   'CODEX_APP_SERVER_SOCKET': str(root / 'absent-native.sock')}
            code = r'''
import sys, asyncio, json, socket
from unittest import mock
def audit(event, args):
    if event == 'open' and isinstance(args[0], str):
        if args[0].startswith(('/home/workbench/.codex/', '/home/workbench/.local/share/codex-web/', '/home/workbench/Tools/', '/mnt/c/Users/')):
            raise AssertionError('core startup tried a personal dependency')
sys.addaudithook(audit)
with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('core startup must not connect')):
    import app, projects
    from routes import files, auth, misc
    from shared import config
    async def run():
        async with app.app.router.lifespan_context(app.app):
            from engine.conversation_service import conversation_service
            service = conversation_service()
            assert service._scheduler is None
            assert auth.setup().status_code == 403
            assert not config.ACCOUNTS_FILE.exists()
            assert not config.SECRET_FILE.exists()
            item = projects.create('合成项目')
            assert projects.get(item['id'])['name'] == '合成项目'
            with mock.patch.object(files, 'FILE_ROOTS', (config.PROJECTS_DIR.resolve(), config.INBOX_DIR.resolve())):
                links = files._file_shortcuts()
                assert '/home/workbench/Projects' not in links
                assert 'Windows' not in links
                assert '项目' in links and '收件箱' in links
            print(json.dumps({'core_started': True, 'project_created': True, 'scheduler_started': False,
                'network_attempted': False, 'personal_data_read': False, 'account': config.ACCOUNT}))
    asyncio.run(run())
'''
            result = subprocess.run([sys.executable, '-c', code], env=env, text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            data = json.loads(result.stdout)
            self.assertEqual(data['account'], 'new-owner')
            self.assertTrue(data['core_started'])
            self.assertTrue((projects / '合成项目' / '.codex-project.json').is_file())
