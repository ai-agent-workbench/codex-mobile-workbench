import unittest
from unittest import mock

from routes import misc


class TtsSettingsTests(unittest.TestCase):
    def test_settings_home_links_to_dedicated_tts_page(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.settings(None).body.decode("utf-8")
        self.assertIn('href="/settings/tts"', body)
        self.assertIn("回复朗读", body)
        self.assertIn("回复朗读（TTS）", body)
        self.assertNotIn('id="tts-voice"', body)

    def test_tts_page_explains_system_default_and_device_local_storage(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.tts_settings(None).body.decode("utf-8")
        self.assertIn('href="/settings"', body)
        self.assertIn('id="tts-voice"', body)
        self.assertIn("跟随 Android 系统默认（推荐）", body)
        self.assertIn("网页无法读取引擎包名", body)
        self.assertIn("保存到这台设备", body)
        self.assertIn("彻底关闭 Chrome", body)
        self.assertIn("/static/tts.js", body)
        self.assertIn("/static/tts-settings.js", body)

if __name__ == "__main__":
    unittest.main()
