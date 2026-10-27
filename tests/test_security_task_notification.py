import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import security


class TaskNotificationSenderTests(unittest.TestCase):
    def setUp(self):
        available = mock.patch('shared.security.task_notification_available', return_value=True)
        available.start()
        self.addCleanup(available.stop)

    @mock.patch("shared.security.subprocess.run")
    def test_uses_approved_script_with_inherited_environment(self, run):
        run.return_value = mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"https_proxy": "fixture-proxy"}):
            sent, detail = security.notify_task_completion(
                "标题", "正文", tags="warning"
            )
        self.assertEqual((sent, detail), (True, "sent"))
        args, kwargs = run.call_args
        self.assertEqual(
            args[0], ["bash", str(security.TASK_NOTIFICATION_COMMAND), "标题", "正文", "default"],
        )
        self.assertFalse(kwargs["check"])
        self.assertGreaterEqual(kwargs["timeout"], 15)
        self.assertEqual(kwargs["env"]["NTFY_MARKDOWN"], "1")
        self.assertEqual(kwargs["env"]["NTFY_TAGS"], "warning")
        self.assertEqual(kwargs["env"]["https_proxy"], "fixture-proxy")
        self.assertIn("NTFY_TOPIC_FILE", kwargs["env"])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    @mock.patch("shared.security.subprocess.run", side_effect=subprocess.TimeoutExpired("sender", 20))
    def test_timeout_has_stable_non_sensitive_detail(self, _run):
        self.assertEqual(
            security.notify_task_completion("标题", "正文"),
            (False, "TimeoutExpired"),
        )

    @mock.patch("shared.security.subprocess.run")
    def test_nonzero_exit_does_not_preserve_sender_output(self, run):
        run.return_value = mock.Mock(returncode=2, stdout="secret", stderr="secret")
        self.assertEqual(
            security.notify_task_completion("标题", "正文"),
            (False, "exit_2"),
        )

    @mock.patch("shared.security.subprocess.run")
    def test_missing_optional_sender_does_not_start_a_process(self, run):
        with mock.patch('shared.security.task_notification_available', return_value=False):
            self.assertEqual(security.notify_task_completion('标题', '正文'),
                             (False, 'task_sender_not_configured'))
        run.assert_not_called()

    @mock.patch("shared.security.subprocess.run")
    def test_configured_sender_is_used_without_falling_back_to_alerts(self, run):
        run.return_value = mock.Mock(returncode=0)
        with (mock.patch.object(security, 'TASK_NOTIFICATION_COMMAND', Path('/fixture/task-sender.sh')),
              mock.patch.object(security, 'TASK_NTFY_TOPIC_FILE', Path('/fixture/private-topic'))):
            self.assertEqual(security.notify_task_completion('标题', '正文'), (True, 'sent'))
        self.assertEqual(run.call_args.args[0][1], '/fixture/task-sender.sh')
        self.assertEqual(run.call_args.kwargs['env']['NTFY_TOPIC_FILE'], '/fixture/private-topic')


class TaskNotificationPrerequisitesTests(unittest.TestCase):
    def test_files_are_checked_without_reading_topic_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            sender, topic = Path(temporary) / 'sender.sh', Path(temporary) / 'topic'
            sender.touch()
            with (mock.patch.object(security, 'TASK_NOTIFICATION_COMMAND', sender),
                  mock.patch.object(security, 'TASK_NTFY_TOPIC_FILE', topic),
                  mock.patch.object(Path, 'read_text', side_effect=AssertionError('must not read topic'))):
                self.assertFalse(security.task_notification_available())
                topic.touch()
                self.assertTrue(security.task_notification_available())
                sender.unlink()
                self.assertFalse(security.task_notification_available())
