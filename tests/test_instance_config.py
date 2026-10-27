import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared import instance_config as cfg


class InstanceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='instance-config-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.patch = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.root = self.home / '中文 实例'
        self.path = self.root / 'instance.json'

    def initialize(self, **extra):
        return cfg.initialize(self.path, {'root': str(self.root), 'account': 'fixture-owner', **extra})

    def test_missing_read_is_side_effect_free(self):
        self.assertIsNone(cfg.read(self.path))
        self.assertFalse(self.root.exists())

    def test_initialize_is_private_core_only_and_repeat_keeps_data(self):
        first = self.initialize()
        marker = self.root / 'workspace/projects' / 'keep.txt'
        marker.write_text('用户资料')
        self.assertFalse(first['settings']['automations'])
        self.assertFalse(first['settings']['completion_notifications'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cfg.read(self.path), first)
        self.assertEqual(self.initialize(), first)
        self.assertEqual(marker.read_text(), '用户资料')

    def test_repeated_prepare_can_complete_missing_owned_directories(self):
        first = self.initialize()
        (self.root / 'workspace/inbox').rmdir()
        self.assertEqual(self.initialize(), first)
        self.assertEqual((self.root / 'workspace/inbox').stat().st_mode & 0o777, 0o700)

    def test_bad_inputs_create_no_partial_root(self):
        for account in ('', ' leading', 'line\nbreak', None):
            with self.subTest(account=account), self.assertRaises(cfg.InstanceError):
                self.initialize(account=account)
            self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(cfg.InstanceError, '配置文件必须'):
            cfg.initialize(self.home / 'elsewhere.json', {'root': str(self.root), 'account': 'owner'})
        self.assertFalse(self.root.exists())

    def test_existing_unmanaged_directory_or_account_is_not_adopted(self):
        self.root.mkdir(mode=0o700)
        marker = self.root / 'keep.txt'
        marker.write_text('untouched')
        with self.assertRaisesRegex(cfg.InstanceError, '已有数据'):
            self.initialize()
        self.assertEqual(marker.read_text(), 'untouched')
        marker.unlink()
        self.initialize()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(cfg.InstanceError, '管理员不同'):
            self.initialize(account='other')
        self.assertEqual(self.path.read_bytes(), original)

    def test_symlinks_and_loose_permissions_fail_without_reset(self):
        self.initialize()
        original = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaises(cfg.InstanceError):
            cfg.read(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.chmod(0o600)
        link = self.home / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(cfg.InstanceError):
            cfg.read(link / 'instance.json')
        (self.root / 'workspace/inbox').rmdir()
        (self.root / 'workspace/inbox').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(cfg.InstanceError):
            self.initialize()

    def test_runtime_layout_rejects_aliases_without_reading_login_data(self):
        value = self.initialize()
        (self.root / 'native-home').rmdir()
        (self.root / 'native-home').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(cfg.InstanceError):
            cfg.environment(value)
        (self.root / 'native-home').unlink()
        (self.root / 'native-home').mkdir(mode=0o700)
        target = self.home / 'foreign-auth.json'
        target.write_text('inert sentinel; never read'); target.chmod(0o600)
        (self.root / 'native-home/auth.json').symlink_to(target)
        with self.assertRaises(cfg.InstanceError):
            cfg.environment(value)

    def test_invalid_saved_data_is_not_silently_defaulted(self):
        first = self.initialize()
        for bad in ({**first, 'format_version': True}, {**first, 'extra': True},
                    {**first, 'port': True}):
            self.path.write_text(json.dumps(bad))
            with self.assertRaises(cfg.InstanceError):
                cfg.read(self.path)
            self.assertEqual(json.loads(self.path.read_text()), bad)

    def test_configuration_never_accepts_secret_values_in_place_of_paths(self):
        first = self.initialize()
        value = copy.deepcopy(first['settings'])
        value['notification_topic_file'] = 'topic-value-not-an-absolute-path'
        with self.assertRaises(cfg.InstanceError):
            cfg.settings(value)
        value = copy.deepcopy(first['settings'])
        value['completion_notifications'] = True
        with self.assertRaisesRegex(cfg.InstanceError, '先配置'):
            cfg.settings(value)

    def test_secure_origin_and_private_environment(self):
        value = self.initialize()
        self.assertEqual(cfg.phone_origin('https://Phone.Example:443/'), 'https://phone.example')
        for bad in ('http://phone.example', 'https://user:pass@phone.example',
                    'https://phone.example/login', 'https://phone.example?token=secret', '//phone.example'):
            with self.subTest(bad=bad), self.assertRaises(cfg.InstanceError):
                cfg.phone_origin(bad)
        env = cfg.environment(value)
        self.assertEqual(env['CODEX_WEB_AUTOMATIONS_ENABLED'], '0')
        self.assertEqual(env['CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED'], '0')
        self.assertEqual(env['CODEX_WEB_MANAGED_INSTALL'], '1')
        self.assertEqual(env['CODEX_WEB_DOT_ENABLED'], '0')
        self.assertEqual(env['CODEX_WEB_DOT_EVENTS_OUTBOUND_ENABLED'], '0')
        self.assertEqual(env['CODEX_HOME'], str(self.root / 'native-home'))
        self.assertEqual(env['HOME'], str(self.root / 'home'))
        self.assertNotIn('/home/workbench/Tools', json.dumps(env))
        self.assertLess(len(env['CODEX_APP_SERVER_SOCKET'].encode()), 108)
