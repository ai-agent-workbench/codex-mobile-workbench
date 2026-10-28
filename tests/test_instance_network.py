"""Isolated per-instance network-policy contract."""
import copy
import os
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from shared import instance_config as cfg
from shared import instance_manager as manager
from shared import instance_network as network
from shared import instance_runtime as runtime


class NetworkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='network-policy-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home)}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.root = self.home / 'instance'
        self.path = self.root / 'instance.json'
        self.config = cfg.initialize(self.path, {'root': str(self.root), 'account': 'owner'})

    def test_inherit_without_proxy_is_valid_and_forces_loopback(self):
        result = network.policy(self.config['settings'], {})
        self.assertEqual(result['HTTPS_PROXY'], '')
        self.assertIn('127.0.0.1', result['NO_PROXY'])
        self.assertIn('::1', result['no_proxy'])

    def test_inherit_preserves_wsl_proxy_without_mutating_parent(self):
        parent = {'https_proxy': 'http://inherited.invalid:8080', 'no_proxy': 'internal.example'}
        result = network.policy(self.config['settings'], parent)
        self.assertEqual(result['https_proxy'], parent['https_proxy'])
        self.assertEqual(result['HTTPS_PROXY'], parent['https_proxy'])
        self.assertIn('internal.example', result['no_proxy'])
        self.assertNotIn('NO_PROXY', parent)

    def test_uppercase_only_proxy_and_bypass_survive_lowercase_normalization(self):
        parent = {'HTTPS_PROXY': 'http://inherited.invalid:8080', 'NO_PROXY': 'internal.example'}
        result = network.policy(self.config['settings'], parent)
        self.assertEqual(result['https_proxy'], parent['HTTPS_PROXY'])
        self.assertEqual(result['HTTPS_PROXY'], parent['HTTPS_PROXY'])
        self.assertIn('internal.example', result['no_proxy'])
        self.assertEqual(result['no_proxy'], result['NO_PROXY'])
        cleared = network.policy(self.config['settings'], {**parent, 'https_proxy': '', 'no_proxy': ''})
        self.assertEqual(cleared['HTTPS_PROXY'], '')
        self.assertNotIn('internal.example', cleared['NO_PROXY'])

    def test_network_mode_invalid_types_are_controlled_errors(self):
        for mode in ([], {}, None, 1):
            with self.subTest(mode=mode), self.assertRaises(cfg.InstanceError):
                cfg.settings({**self.config['settings'], 'network_mode': mode})

    def test_pip_external_configuration_cannot_override_selected_route(self):
        parent = {'PIP_CONFIG_FILE': '/not-read/private-pip.conf', 'https_proxy': 'http://fixture.invalid:8080'}
        result = network.pip_environment(parent)
        self.assertEqual(result['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(result['https_proxy'], parent['https_proxy'])
        self.assertEqual(parent['PIP_CONFIG_FILE'], '/not-read/private-pip.conf')

    def test_direct_clears_every_proxy_key_and_keeps_bypass(self):
        settings = {**self.config['settings'], 'network_mode': 'direct'}
        parent = {key: 'http://parent.invalid:8080' for key in network.PROXY_KEYS}
        parent['NO_PROXY'] = 'inside.example'
        result = network.policy(settings, parent)
        self.assertTrue(all(result[key] == '' for key in network.PROXY_KEYS))
        self.assertIn('inside.example', result['NO_PROXY'])
        self.assertIn('127.0.0.1', result['NO_PROXY'])
        self.assertEqual(parent['HTTPS_PROXY'], 'http://parent.invalid:8080')

    def test_explicit_proxy_is_consistent_and_bad_values_do_not_echo(self):
        proxy = 'https://user%40name:secret%3Avalue@proxy.example:8443'
        settings = {**self.config['settings'], 'network_mode': 'proxy', 'network_proxy': proxy}
        result = network.policy(settings, {'NO_PROXY': 'local.example'})
        self.assertTrue(all(result[key] == proxy for key in network.PROXY_KEYS))
        self.assertIn('local.example', result['NO_PROXY'])
        self.assertIn('localhost', result['NO_PROXY'])
        for bad in ('http://proxy.example/path', 'http://proxy.example?secret=value',
                    'http://proxy.example#secret', 'http://proxy.example:99999',
                    'http://proxy\\evil', 'http://proxy.example\nsecret'):
            with self.subTest(bad=bad), self.assertRaises(cfg.InstanceError) as error:
                cfg.settings({**settings, 'network_proxy': bad})
            self.assertNotIn(bad, str(error.exception))

    def test_legacy_profile_and_configure_payload_preserve_network_choice(self):
        legacy = copy.deepcopy(self.config)
        legacy['settings'] = {key: legacy['settings'][key] for key in cfg.LEGACY_SETTINGS}
        self.path.write_text(__import__('json').dumps(legacy)); self.path.chmod(0o600)
        read = cfg.read(self.path)
        self.assertEqual(read['settings']['network_mode'], 'inherit')
        self.assertEqual(read['settings']['network_proxy'], '')
        selected = {**read, 'settings': {**read['settings'], 'network_mode': 'proxy',
                    'network_proxy': 'http://proxy.example:8080'}}
        cfg.write_json(self.path, selected)
        payload = {key: selected['settings'][key] for key in cfg.LEGACY_SETTINGS}
        payload['expected_revision'] = manager.revision(selected)
        manager.configure(self.path, payload)
        self.assertEqual(cfg.read(self.path)['settings']['network_proxy'], 'http://proxy.example:8080')
        revision = manager.revision(cfg.read(self.path))
        manager.configure(self.path, {**payload, 'expected_revision': revision,
                                      'network_mode': 'direct', 'network_proxy': None})
        self.assertEqual(cfg.read(self.path)['settings']['network_proxy'], '')

    def test_inspect_never_returns_proxy_url(self):
        config = {**self.config, 'settings': {**self.config['settings'], 'network_mode': 'proxy',
                  'network_proxy': 'http://person:password@proxy.example:8080'}}
        cfg.write_json(self.path, config)
        with mock.patch.object(manager, 'user_environment', return_value={}):
            result = manager.inspect(self.path)
        rendered = repr(result)
        self.assertEqual(result['settings']['network_proxy'], '')
        self.assertNotIn('person:password', rendered)
        self.assertEqual(result['network']['model_connection'], 'not_checked')

    def test_service_env_explicitly_clears_manager_proxy_for_direct_mode(self):
        config = {**self.config, 'settings': {**self.config['settings'], 'network_mode': 'direct'}}
        directory = self.home / 'units'; directory.mkdir(mode=0o700)
        def local_step(args, **kwargs):
            if args[:3] == ['systemctl', '--user', 'show']:
                return ('FragmentPath=' + str(directory / args[3]) + '\nDropInPaths=\nLoadState=loaded\nEnvironmentFiles='
                        + str(self.root / 'service.env') + ' (ignore_errors=no)\nWorkingDirectory='
                        + str(self.root / 'home') + '\n')
            return ''
        parent = {key: 'http://manager.invalid:8080' for key in network.PROXY_KEYS}
        with mock.patch.object(runtime, 'unit_directory', return_value=directory), \
                mock.patch.object(runtime, 'user_environment', return_value={}), \
                mock.patch.object(runtime, 'command', side_effect=local_step), \
                mock.patch.dict(os.environ, parent, clear=True):
            runtime.write_services(config, {'controller': 'controllers/' + 'a' * 16})
        contents = (self.root / 'service.env').read_text()
        for key in network.PROXY_KEYS:
            self.assertIn(key + '=\"\"\n', contents)
        self.assertEqual((self.root / 'service.env').stat().st_mode & 0o777, 0o600)

    def test_native_download_uses_policy_environment_and_no_proxy_argument(self):
        config = {**self.config, 'settings': {**self.config['settings'], 'network_mode': 'direct'}}
        with mock.patch.object(runtime, 'network_command', side_effect=cfg.InstanceError('offline')) as download:
            with self.assertRaises(cfg.InstanceError):
                runtime.install_native(config)
        args, kwargs = download.call_args
        self.assertEqual(args[0][0], 'curl')
        self.assertNotIn('--proxy', args[0])
        self.assertNotIn('--noproxy', args[0])
        self.assertEqual(kwargs['env']['HTTPS_PROXY'], '')
        self.assertEqual(kwargs['code'], 'native_download_failed')

    def test_dependency_install_passes_route_and_ignores_parent_pip_config(self):
        for mode in ('inherit', 'direct', 'proxy'):
            config = {**self.config, 'settings': {**self.config['settings'], 'network_mode': mode,
                      'network_proxy': 'http://selected.invalid:8080' if mode == 'proxy' else ''}}
            with self.subTest(mode=mode), \
                    mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://inherited.invalid:8080',
                                                 'PIP_CONFIG_FILE': '/unread/private-pip.conf'}), \
                    mock.patch.object(runtime, 'user_environment', return_value={}), \
                    mock.patch.object(runtime, 'unit_directory', return_value=self.home), \
                    mock.patch.object(runtime, 'unit_status', return_value='inactive'), \
                    mock.patch.object(runtime, 'snapshot_source', return_value=('source', 'runtime')), \
                    mock.patch.object(runtime, 'command', return_value=''), \
                    mock.patch.object(runtime, 'network_command', side_effect=cfg.InstanceError('fixture stop')) as download:
                with self.assertRaises(cfg.InstanceError):
                    runtime.install(config, self.home / 'unused-source')
            args, options = download.call_args
            self.assertIn('pip', args[0])
            self.assertNotIn('--proxy', args[0])
            self.assertEqual(options['code'], 'dependency_download_failed')
            self.assertEqual(options['env']['PIP_CONFIG_FILE'], os.devnull)
            expected = '' if mode == 'direct' else 'http://selected.invalid:8080' if mode == 'proxy' else 'http://inherited.invalid:8080'
            self.assertEqual(options['env']['https_proxy'], expected)

    def test_network_failure_has_one_route_and_sanitized_stage_error(self):
        env = runtime.instance_environment(self.config)
        with mock.patch.object(runtime, 'command', side_effect=cfg.InstanceError('http://secret@proxy')) as call:
            with self.assertRaises(cfg.InstanceError) as error:
                runtime.network_command(['curl'], env=env, timeout=1, mode='proxy',
                                        phase='原生程序下载', code='native_download_failed')
        self.assertEqual(error.exception.code, 'native_download_failed')
        self.assertIn('原生程序下载', str(error.exception))
        self.assertIn('未自动切换', str(error.exception))
        self.assertNotIn('secret', str(error.exception))
        call.assert_called_once()

    @unittest.skipUnless(shutil.which('curl') and shutil.which('openssl'), 'local HTTPS probe needs curl and openssl')
    def test_real_curl_and_isolated_pip_use_the_selected_route(self):
        source = Path(__file__).resolve().parents[1]
        probe = source / 'tests/fixtures/instance_network_transport.py'
        # The child uses only its own synthetic environment and loopback
        # servers; it cannot access a public origin or any real credentials.
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.home), 'LANG': 'C.UTF-8'}
        done = subprocess.run([sys.executable, str(probe), str(source)], env=env,
                              capture_output=True, text=True, timeout=45)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = [json.loads(line) for line in done.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(lines[-1]['cases'], 12)
        self.assertTrue(lines[-1]['passed'])
        self.assertTrue(all(item['passed'] for item in lines[:-1]))
