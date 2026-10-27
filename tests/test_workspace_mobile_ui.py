import unittest
from datetime import datetime
from unittest import mock

import projects
from routes import home, misc


class WorkspaceMobileUiTests(unittest.TestCase):
    def test_history_has_new_topbar_active_nav_and_empty_state(self):
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "conversation_service") as service, \
             mock.patch.object(projects, "recent", return_value=[]):
            service.return_value.list_public_page.return_value = {"values": [], "next_cursor": None}
            body = home.history(None).body.decode("utf-8")
        self.assertIn("class='history-page-head'", body)
        self.assertIn("class='history-new-conversation' href='/quick'", body)
        self.assertIn("还没有历史对话", body)
        self.assertIn("href='/projects/history' aria-current='page'", body)
        self.assertIn("action='/projects/history/manage'", body)

    def test_history_keeps_card_management_markup_and_real_status(self):
        now = datetime.now().astimezone().isoformat()
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "conversation_service") as service, \
             mock.patch.object(projects, "recent", return_value=[]):
            service.return_value.list_public_page.return_value = {"values": [{
                "id": "one", "title": "很长的历史对话标题", "state": "active",
                "project_id": None, "pinned": False, "activity_at": now,
            }], "next_cursor": None}
            body = home.history(None).body.decode("utf-8")
        self.assertIn("manage-card conversation-index-card running", body)
        self.assertIn("conversation-state-rail", body)
        self.assertIn("name='ids'", body)
        self.assertIn("执行中", body)
        self.assertNotIn("<small>临时对话 · 执行中", body)

    def test_history_visual_card_keeps_status_only_in_chip(self):
        now = datetime.now().astimezone()
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "conversation_service") as service, \
             mock.patch.object(projects, "recent", return_value=[{
                 "id": "project-one", "name": "Codex Web 工作台维护",
             }]):
            service.return_value.list_public_page.return_value = {"values": [{
                "id": "one", "title": "完成的对话", "state": "idle",
                "project_id": "project-one", "pinned": False,
                "last_terminal_at": now.isoformat(), "activity_at": now.isoformat(),
            }], "next_cursor": None}
            body = home.history(None).body.decode("utf-8")
        time_label = now.strftime("%m-%d %H:%M")
        self.assertIn("<small>Codex Web 工作台维护 · " + time_label + "</small>", body)
        self.assertIn("<i aria-hidden='true'></i>已完成</span>", body)
        self.assertNotIn("完成 " + time_label, body)

    def test_settings_compact_center_keeps_themes_and_real_quota_hooks(self):
        with mock.patch.object(misc, "require_auth", return_value=None):
            body = misc.settings(None).body.decode("utf-8")
        self.assertIn('class="settings-page settings-center"', body)
        self.assertIn("<option value='dark'>深色</option>", body)
        self.assertIn("<option value='light'>浅色</option>", body)
        self.assertIn("跟随系统", body)
        self.assertIn("id='codex-quota-card'", body)
        self.assertIn("data-endpoint='/settings/codex-quota'", body)
        self.assertIn("id='codex-weekly-value'", body)
        self.assertIn("id='codex-quota-refresh'", body)
        self.assertIn("href='/settings' aria-current='page'", body)

    def test_notifications_hide_admin_toggle_but_keep_rules(self):
        topic_file = mock.Mock()
        topic_file.read_text.return_value = ""
        request = object()
        with mock.patch.object(misc, "require_auth", return_value=None), \
             mock.patch.object(misc, "logged_in", return_value="person"), \
             mock.patch.object(misc, "load_accounts", return_value={"person": {"role": "user"}}), \
             mock.patch.object(misc, "NTFY_TOPIC_FILE", topic_file):
            user = misc.notifications(request).body.decode("utf-8")
        with mock.patch.object(misc, "require_auth", return_value=None), \
             mock.patch.object(misc, "logged_in", return_value="person"), \
             mock.patch.object(misc, "load_accounts", return_value={"person": {"role": "admin"}}), \
             mock.patch.object(misc, "NTFY_TOPIC_FILE", topic_file):
            admin = misc.notifications(request).body.decode("utf-8")
        self.assertNotIn("id=\"web-push-toggle\"", user)
        self.assertIn("任务完成", user)
        self.assertIn("安全事件", user)
        self.assertIn("id=\"web-push-toggle\"", admin)
        self.assertIn("不等于保证送达", admin)
        self.assertNotIn("测试通知", admin)

if __name__ == "__main__":
    unittest.main()
