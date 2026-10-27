import asyncio
import json
import unittest
from fastapi.responses import RedirectResponse
from datetime import datetime, timedelta
from unittest import mock

import projects
from routes import home as home_routes
from routes import projects as project_routes
from shared.conversation_views import conversation_cards


class ConversationViewTests(unittest.TestCase):
    def test_history_uses_real_project_names_and_date_groups(self):
        now = datetime.now().astimezone()
        values = [
            {
                "id": "pinned-project", "title": "置顶项目任务", "state": "idle",
                "project_id": "project-one", "updated_at": now.isoformat(),
                "last_terminal_at": now.isoformat(),
                "pinned": True,
            },
            {
                "id": "today-project", "title": "今天项目任务", "state": "idle",
                "project_id": "project-one", "updated_at": now.isoformat(),
                "last_terminal_at": now.isoformat(),
                "pinned": False,
            },
            {
                "id": "yesterday-temp", "title": "临时任务", "state": "active",
                "project_id": None,
                "updated_at": (now - timedelta(days=1)).isoformat(),
                "activity_at": (now - timedelta(days=1)).isoformat(),
                "pinned": False,
            },
            {
                "id": "older", "title": "更早任务", "state": "idle",
                "project_id": "missing",
                "updated_at": (now - timedelta(days=4)).isoformat(),
                "last_terminal_at": (now - timedelta(days=4)).isoformat(),
                "pinned": False,
            },
        ]
        body = conversation_cards(
            values, "history-form", show_project=True,
            project_names={"project-one": "Codex Web 工作台维护"},
            group_dates=True,
        )
        self.assertIn("Codex Web 工作台维护 · 已就绪", body)
        self.assertIn("临时对话 · 执行中", body)
        self.assertIn("项目对话 · 已就绪", body)
        self.assertEqual(body.count("conversation-date-group"), 4)
        self.assertLess(body.index("置顶"), body.index("今天"))
        self.assertLess(body.index("今天"), body.index("昨天"))
        self.assertLess(body.index("昨天"), body.index("更早"))

    def test_project_page_cards_use_shared_grouped_status_layout_without_project_name(self):
        body = conversation_cards([{
            "id": "one", "title": "项目内对话", "state": "idle",
            "project_id": "project-one", "updated_at": None,
            "last_terminal_at": None, "pinned": False,
        }], "project-form", group_dates=True, visual_status=True)
        self.assertNotIn("项目对话 ·", body)
        self.assertIn("conversation-date-group", body)
        self.assertIn("conversation-card-status", body)

    def test_history_marks_failed_terminal_turn_as_failure_not_completion(self):
        body = conversation_cards([{
            "id": "failed-one", "title": "失败任务", "state": "idle",
            "last_terminal_status": "failed", "unread": True,
        }], "history-form", visual_status=True)
        self.assertIn("conversation-index-card failed", body)
        self.assertIn(">执行失败</span>", body)
        self.assertNotIn("完成未读", body)

    def test_priority_groups_distinguish_successful_unread_from_stopped(self):
        now = datetime.now().astimezone().isoformat()
        rows = [
            {"id": "pin", "pinned": True, "unread": True, "last_terminal_status": "completed"},
            {"id": "result", "unread": True, "last_terminal_status": "completed"},
            {"id": "stop", "unread": True, "last_terminal_status": "interrupted"},
            {"id": "read", "unread": False, "last_terminal_status": "completed"},
        ]
        for row in rows:
            row.update(state="idle", title=row["id"], activity_at=now, last_terminal_at=now)
        body = conversation_cards(rows, "test", visual_status=True, group_dates=True)
        self.assertEqual(body.count("<h2>置顶</h2>"), 1)
        self.assertEqual(body.count("<h2>已完成未读</h2>"), 1)
        self.assertEqual(body.count("<h2>今天</h2>"), 1)
        self.assertLess(body.index("<h2>置顶</h2>"), body.index("<h2>已完成未读</h2>"))
        self.assertLess(body.index("<h2>已完成未读</h2>"), body.index("<h2>今天</h2>"))
        self.assertIn("conversation-index-card stopped'", body)
        self.assertNotIn("stopped unread", body)

    def test_visual_status_distinguishes_completed_from_idle_and_keeps_terminal_priorities(self):
        body = conversation_cards([
            {"id": "ready", "title": "空闲", "state": "idle", "pinned": False},
            {"id": "done", "title": "完成", "state": "idle", "pinned": False,
             "last_terminal_status": "completed", "last_terminal_turn_id": "turn-done"},
            {"id": "unread", "title": "未读完成", "state": "idle", "pinned": False,
             "unread": True, "last_terminal_at": datetime.now().astimezone().isoformat()},
            {"id": "failed", "title": "失败", "state": "idle", "pinned": False,
             "last_terminal_status": "failed", "last_terminal_at": datetime.now().astimezone().isoformat()},
            {"id": "stopped", "title": "停止", "state": "idle", "pinned": False,
             "last_terminal_status": "interrupted", "last_terminal_at": datetime.now().astimezone().isoformat()},
        ], "history-form", visual_status=True)
        self.assertIn(">已就绪</span>", body)
        self.assertIn(">已完成</span>", body)
        self.assertIn(">完成未读</span>", body)
        self.assertIn(">执行失败</span>", body)
        self.assertIn(">已停止</span>", body)

    def test_history_route_resolves_project_name_instead_of_generic_label(self):
        now = datetime.now().astimezone().isoformat()
        values = [{
            "id": "one", "title": "部署评审", "state": "idle",
            "project_id": "local-model", "updated_at": now,
            "last_terminal_at": now, "pinned": False,
        }]
        with mock.patch.object(home_routes, "require_auth", return_value=None), \
             mock.patch.object(home_routes, "conversation_service") as service, \
             mock.patch.object(projects, "recent", return_value=[{
                 "id": "local-model", "name": "部署本地模型",
             }]):
            service.return_value.list_public_page.return_value = {"values": values, "next_cursor": None}
            body = home_routes.history(None).body.decode("utf-8")
        terminal_label = datetime.fromisoformat(now).astimezone().strftime("%m-%d %H:%M")
        self.assertIn("部署本地模型 · " + terminal_label, body)
        self.assertNotIn("部署本地模型 · 已就绪", body)
        self.assertNotIn("项目对话 · 已就绪", body)
        self.assertIn("已完成", body)
        self.assertIn("conversation-date-group", body)
        self.assertIn("conversation-state-rail", body)
        self.assertIn("conversation-card-status", body)
        self.assertIn("complete", body)
        self.assertIn("action='/projects/history/manage'", body)
        self.assertNotIn("action='/history/manage'", body)

    def test_history_manage_redirects_to_public_gateway_route(self):
        service = mock.Mock()
        service.scoped_public_ids.return_value = ["one"]
        request = mock.Mock()
        with mock.patch.object(home_routes, "require_auth", return_value=None), \
             mock.patch.object(home_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(home_routes, "conversation_service", return_value=service), \
             mock.patch.object(home_routes, "audit"):
            response = asyncio.run(home_routes.history_manage(
                request, ids=["one"], action="unpin",
            ))
        service.set_pinned.assert_called_once_with(["one"], False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/projects/history")

    def test_history_fragment_is_private_and_uses_authenticated_cursor_scope(self):
        service = mock.Mock()
        service.list_public_page.return_value = {"values": [], "next_cursor": "next"}
        request = mock.Mock()
        with mock.patch.object(home_routes, "require_auth", return_value=None), \
             mock.patch.object(home_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(home_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "recent", return_value=[]):
            response = home_routes.history_page(request, "cursor")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(json.loads(response.body)["next_cursor"], "next")
        service.list_public_page.assert_called_once_with(account_id="account-a", cursor="cursor")

        denied = mock.Mock()
        with mock.patch.object(home_routes, "require_auth", return_value=RedirectResponse("/login", 303)), \
             mock.patch.object(home_routes, "conversation_service", return_value=denied):
            response = home_routes.history_page(request, "cursor")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        denied.list_public_page.assert_not_called()

    def test_project_fragment_is_private_and_binds_project_scope(self):
        service = mock.Mock()
        service.list_public_page.return_value = {"values": [{
            "id": "one", "title": "项目对话", "state": "active", "pinned": False,
        }], "next_cursor": None}
        request = mock.Mock()
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "get", return_value={"id": "project-one"}):
            response = project_routes.project_conversation_page(request, "project-one", "cursor")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.list_public_page.assert_called_once_with(
            project_id="project-one", account_id="account-a", cursor="cursor",
        )
        payload = json.loads(response.body)
        self.assertIn("conversation-date-group", payload["html"])
        self.assertIn("conversation-card-status", payload["html"])

    def test_history_manage_uses_service_scope_for_pin_and_delete(self):
        request = mock.Mock()
        service = mock.Mock()
        service.scoped_public_ids.return_value = ["past-500"]
        service.delete_many = mock.AsyncMock()
        with mock.patch.object(home_routes, "require_auth", return_value=None), \
             mock.patch.object(home_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(home_routes, "conversation_service", return_value=service), \
             mock.patch.object(home_routes, "audit"):
            asyncio.run(home_routes.history_manage(request, ids=["past-500"], action="pin"))
            asyncio.run(home_routes.history_manage(request, ids=["past-500"], action="delete"))
        self.assertEqual(service.scoped_public_ids.call_count, 2)
        service.set_pinned.assert_called_once_with(["past-500"], True)
        service.delete_many.assert_awaited_once_with(["past-500"])

    def test_idle_card_uses_terminal_time_and_server_unread(self):
        terminal = datetime.now().astimezone() - timedelta(hours=3)
        projection = terminal + timedelta(hours=2)
        body = conversation_cards([{
            "id": "one", "title": "完成时间核对", "state": "idle",
            "project_id": None, "pinned": False, "unread": True,
            "last_terminal_at": terminal.isoformat(),
            "activity_at": terminal.isoformat(),
            "updated_at": projection.isoformat(),
        }], "history-form", group_dates=True, visual_status=True)
        terminal_label = terminal.strftime("%m-%d %H:%M")
        projection_label = projection.strftime("%m-%d %H:%M")
        self.assertIn(terminal_label, body)
        self.assertNotIn("完成 " + terminal_label, body)
        self.assertNotIn(projection_label, body)
        self.assertIn("完成未读", body)
        self.assertIn("conversation-index-card complete unread", body)


if __name__ == "__main__":
    unittest.main()
