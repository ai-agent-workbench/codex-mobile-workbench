import unittest
from unittest import mock

from routes import misc


class CompletionSoundSettingsTests(unittest.TestCase):
    def test_settings_home_only_links_to_dedicated_sound_page(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.settings(None).body.decode("utf-8")
        self.assertIn('href="/settings/completion-sound"', body)
        self.assertIn("完成提示音", body)
        self.assertNotIn('id="completion-sound-panel"', body)
        self.assertNotIn("选择提示音", body)

    def test_dedicated_page_exposes_device_local_controls_and_explanation(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.completion_sound_settings(None).body.decode("utf-8")
        self.assertIn('href="/settings"', body)
        self.assertIn('id="completion-sound-enabled"', body)
        self.assertIn('data-completion-tone="soft"', body)
        self.assertIn('data-completion-tone="clear"', body)
        self.assertIn('data-completion-tone="strong"', body)
        self.assertIn('id="completion-sound-volume"', body)
        self.assertIn('id="completion-sound-test"', body)
        self.assertIn('id="completion-sound-save"', body)
        self.assertIn("只提醒新的完成轮次", body)
        self.assertIn("前台为主", body)
        self.assertIn("/static/completion-sound.js", body)
        self.assertIn("/static/completion-sound-settings.js", body)

if __name__ == "__main__":
    unittest.main()
