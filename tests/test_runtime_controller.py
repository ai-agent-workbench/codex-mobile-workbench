import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from shared import runtime_controller as controller


class RuntimeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.releases = self.state / 'releases'
        self.releases.mkdir(parents=True)
        self.old = self.releases / '20261008-080000' / 'runtime'
        self.new = self.releases / '20261008-090000' / 'runtime'
        for bundle in (self.old, self.new):
            (bundle / 'app').mkdir(parents=True)

    def test_commands_and_environment_use_once_resolved_bundle_not_developer_or_alias(self):
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/evil', 'PYTHONHOME': '/evil'}):
            env = controller.child_environment(self.new, self.state)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertEqual(env['CODEX_WEB_PYTHON'], str(self.new / '.venv/bin/python'))
        for role in controller.ROLES:
            argv = controller.command_for(self.new, role)
            self.assertNotIn('runtime-current', ' '.join(argv))
            self.assertIn(str(self.new / 'app'), ' '.join(argv))
            self.assertNotIn('/home/workbench/codex-web-app', ' '.join(argv))
        self.assertEqual(controller.command_for(self.new, 'web')[1:4], ['-B', '-I', '-c'])

    def test_startup_import_failure_is_compensated_by_controller_not_app_hook(self):
        old_child = mock.Mock()
        old_child.wait.return_value = 0
        old_child.poll.return_value = 0
        calls = [self.new, self.new, self.old, self.old]
        with mock.patch.object(controller, 'selected_runtime', side_effect=calls), \
             mock.patch.object(controller, '_pending', side_effect=[(self.new.parent.name, self.old.parent.name), None]), \
             mock.patch.object(controller, '_read_meta', return_value={'previous': self.old.parent.name}), \
             mock.patch.object(controller, '_status', side_effect=['pending', 'healthy']), \
             mock.patch.object(controller.subprocess, 'Popen', side_effect=[OSError('exec'), old_child]), \
             mock.patch.object(controller, 'compensate') as compensate, \
             mock.patch.object(controller.signal, 'signal'), \
             mock.patch.object(controller, '_log'):
            self.assertEqual(controller.run_consumer(self.state, 'web'), 0)
        compensate.assert_called_once_with(self.releases, self.new, self.state)

    def test_failed_smoke_stops_all_candidate_writers_before_compensation(self):
        child = mock.Mock()
        child.poll.side_effect = lambda: None if not child.terminate.called else 0
        child.wait.return_value = 0
        events = []
        child.terminate.side_effect = lambda: events.append('stop')
        with mock.patch.object(controller, 'selected_runtime', side_effect=[self.new, self.new, RuntimeError('end')]), \
             mock.patch.object(controller, '_pending', return_value=(self.new.parent.name, self.old.parent.name)), \
             mock.patch.object(controller, '_read_meta', return_value={'previous': self.old.parent.name}), \
             mock.patch.object(controller, '_status', return_value='pending'), \
             mock.patch.object(controller.subprocess, 'Popen', return_value=child), \
             mock.patch.object(controller, 'wait_ready', return_value=True), \
             mock.patch.object(controller, 'smoke', return_value=False), \
             mock.patch.object(controller, 'compensate', side_effect=lambda *a: events.append('compensate')), \
             mock.patch.object(controller.signal, 'signal'), mock.patch.object(controller, '_log'):
            with self.assertRaisesRegex(RuntimeError, 'end'):
                controller.run_consumer(self.state, 'web')
        self.assertEqual(events, ['stop', 'compensate'])

    def test_healthy_acceptance_requires_live_child_and_passed_smoke(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.return_value = 0
        with mock.patch.object(controller, 'selected_runtime', return_value=self.new), \
             mock.patch.object(controller, '_pending', return_value=(self.new.parent.name, self.old.parent.name)), \
             mock.patch.object(controller, '_read_meta', return_value={'previous': self.old.parent.name}), \
             mock.patch.object(controller, '_status', return_value='pending'), \
             mock.patch.object(controller.subprocess, 'Popen', return_value=child), \
             mock.patch.object(controller, 'wait_ready', return_value=True), \
             mock.patch.object(controller, 'smoke', return_value=True), \
             mock.patch.object(controller, 'accept_runtime') as accept, \
             mock.patch.object(controller.signal, 'signal'), mock.patch.object(controller, '_log'):
            self.assertEqual(controller.run_consumer(self.state, 'web'), 0)
        accept.assert_called_once_with(self.releases, self.new.parent.name)

    def test_unknown_consumer_role_is_rejected(self):
        for role in ("security-monitor", "healthcheck", "public-ip-monitor", "unknown"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(controller.RuntimeReleaseError, "invalid runtime consumer"):
                    controller.command_for(self.new, role)

    def test_smoke_uses_bundle_python_and_resolved_cwd_without_persisting_output(self):
        result = subprocess.CompletedProcess([], 1, b'secret-cookie', b'secret-env')
        with mock.patch.object(controller.subprocess, 'run', return_value=result) as run:
            self.assertFalse(controller.smoke(self.new, self.state, 18299))
        self.assertEqual(run.call_args.kwargs['cwd'], self.new / 'app')
        self.assertEqual(run.call_args.kwargs['env']['CODEX_WEB_PYTHON'], str(self.new / '.venv/bin/python'))
        self.assertIn('http://127.0.0.1:18299', run.call_args.args[0])
