import unittest
from unittest import mock

import projects
from routes import home as home_routes
from routes import misc as misc_routes


class UiThemeTests(unittest.TestCase):
    @mock.patch.object(home_routes, "require_auth", return_value=None)
    @mock.patch.object(projects, "recent", return_value=[])
    @mock.patch.object(home_routes, "conversation_service")
    def test_home_keeps_routes_and_adds_visual_hierarchy(self, service, _recent, _auth):
        service.return_value.list_public.return_value = []
        body = home_routes.home(None).body.decode("utf-8")
        self.assertIn("class='home-page'", body)
        self.assertIn("class='card home-action-card primary' href='/quick'", body)
        self.assertIn("href='/project/new'", body)
        self.assertIn("href='/projects/history'", body)
        self.assertIn("action='/projects/history/manage'", body)
        self.assertNotIn("href='/history'", body)
        self.assertNotIn("action='/history/manage'", body)
        self.assertNotIn("搜索项目或会话", body)
        self.assertNotIn("在线", body)

    def test_home_removes_redundant_copy_but_keeps_all_cards(self):
        with mock.patch.object(home_routes, "require_auth", return_value=None), \
             mock.patch.object(home_routes, "conversation_service") as service, \
             mock.patch.object(home_routes, "dot_available", return_value=True), \
             mock.patch.object(projects, "recent", return_value=[{"id": "one", "name": "真实项目名"}]):
            service.return_value.list_public.return_value = []
            body = home_routes.home(None).body.decode("utf-8")
        for copy in ["继续项目", "给 dot 留消息，继续这条对话", "所有新对话都由 App Server 直接管理", "长期工作集中保存", "像聊天一样直接发送"]:
            self.assertNotIn(copy, body)
        for value in ["真实项目名", "href='/project/one'", "新建项目", "新对话", "还没有对话"]:
            self.assertIn(value, body)
        self.assertNotIn("href='/settings/dot'", body)

    @mock.patch.object(misc_routes, "require_auth", return_value=None)
    def test_settings_exposes_only_dark_and_light_themes(self, _auth):
        body = misc_routes.settings(None).body.decode("utf-8")
        self.assertIn("<option value='dark'>深色</option>", body)
        self.assertIn("<option value='light'>浅色</option>", body)
        self.assertNotIn("夜读暖灰", body)
        self.assertNotIn("value='neutral'", body)


if __name__ == "__main__":
    unittest.main()
