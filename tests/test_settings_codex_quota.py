import json
import unittest
from unittest import mock

from fastapi.responses import Response

from engine.codex_app_server import AppServerError
from routes import misc


class SettingsCodexQuotaTests(unittest.IsolatedAsyncioTestCase):
    def test_quota_card_is_after_existing_cards_and_before_advanced_info(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.settings(None).body.decode("utf-8")
        quota_index = body.index("id='codex-quota-card'")
        self.assertGreater(quota_index, body.index("sc-home-group"))
        self.assertLess(quota_index, body.index("class='settings-advanced sc-details'"))
        self.assertIn("周额度", body)
        self.assertIn("id='codex-weekly-bar'", body)
        self.assertIn("id='codex-quota-refresh'", body)
        self.assertIn("aria-label='刷新额度'", body)
        self.assertNotIn("<b>刷新</b>", body)
        self.assertIn("settings-codex-quota.js?v=", body)
        self.assertNotIn("5 小时额度", body)

    async def test_authenticated_quota_api_returns_only_projected_private_data(self):
        projected = {
            "remaining_percent": 72,
            "resets_at": 1788748344,
            "plan_type": "pro",
        }
        with mock.patch.object(misc, "require_auth", return_value=None), \
             mock.patch.object(
                 misc, "_read_codex_weekly_quota",
                 new=mock.AsyncMock(return_value=projected),
             ):
            response = await misc.codex_quota(None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), projected)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_quota_api_requires_auth_and_hides_backend_errors(self):
        with mock.patch.object(
            misc, "require_auth", return_value=Response(status_code=403)
        ), mock.patch.object(
            misc, "_read_codex_weekly_quota", new=mock.AsyncMock()
        ) as read:
            denied = await misc.codex_quota(None)
        self.assertEqual(denied.status_code, 403)
        read.assert_not_awaited()

        with mock.patch.object(misc, "require_auth", return_value=None), \
             mock.patch.object(
                 misc, "_read_codex_weekly_quota",
                 new=mock.AsyncMock(side_effect=AppServerError("secret detail")),
             ):
            failed = await misc.codex_quota(None)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("secret detail", failed.body.decode("utf-8"))

if __name__ == "__main__":
    unittest.main()
