import asyncio
import json
import tempfile
import unittest
from fastapi.responses import RedirectResponse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import projects
from fastapi import FastAPI
from engine.conversation_service import ConversationService
from routes import home
from routes import projects as project_routes
from shared.conversation_artifacts import ConversationArtifactService
from tests.store_fixtures import seeded_store


class FakeRuntime:
    def __getattr__(self, name):
        raise AssertionError("history status must not touch runtime: " + name)


class HistoryStatusRouteTests(unittest.TestCase):
    def test_order_probe_supports_empty_history_without_starting_a_page(self):
        service = mock.Mock()
        service.public_index_by_ids.return_value = []
        service.public_order_version.return_value = "a" * 64
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "logged_in", return_value="account-a"), \
             mock.patch.object(home, "conversation_service", return_value=service), \
             mock.patch.object(projects, "recent", return_value=[]):
            response = home.history_status(object(), ids=[], check_order=True)
        self.assertEqual(json.loads(response.body), {"html": "", "order_version": "a" * 64})
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.public_order_version.assert_called_once_with(account_id="account-a")
        service.list_public_page.assert_not_called()

    def test_project_order_probe_is_scoped_and_requires_existing_project_and_auth(self):
        service = mock.Mock()
        service.public_index_by_ids.return_value = []
        service.public_order_version.return_value = "b" * 64
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "get", return_value={"id": "project-a"}):
            response = project_routes.project_conversation_status(
                object(), "project-a", ids=[], check_order=True,
            )
        self.assertEqual(json.loads(response.body)["order_version"], "b" * 64)
        service.public_order_version.assert_called_once_with(account_id="account-a", project_id="project-a")
        service.reset_mock()
        with mock.patch.object(project_routes, "require_auth", return_value=RedirectResponse("/login", 303)), \
             mock.patch.object(project_routes, "conversation_service", return_value=service):
            response = project_routes.project_conversation_status(object(), "project-a", ids=[], check_order=True)
        self.assertEqual(response.status_code, 401)
        service.public_order_version.assert_not_called()
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "get", side_effect=ValueError("missing")):
            response = project_routes.project_conversation_status(object(), "missing", ids=[], check_order=True)
        self.assertEqual(response.status_code, 404)
        service.public_order_version.assert_not_called()

    def test_status_requires_auth_and_never_reads_service_when_denied(self):
        service = mock.Mock()
        with mock.patch.object(home, "require_auth", return_value=RedirectResponse("/login", 303)), \
             mock.patch.object(home, "conversation_service", return_value=service):
            response = home.history_status(object(), ids=["one"])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.public_index_by_ids.assert_not_called()

    def test_status_limits_invalid_values_and_no_store(self):
        service = mock.Mock()
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "conversation_service", return_value=service):
            for ids in (None, [], [""], ["bad id"], ["x" * 161], ["one"] * 101):
                response = home.history_status(object(), ids=ids)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.headers["cache-control"], "no-store")
        service.public_index_by_ids.assert_not_called()

    def test_http_route_parses_repeated_ids_and_accepts_maximum_batch(self):
        app = FastAPI()
        app.include_router(home.router)
        service = mock.Mock()
        service.public_index_by_ids.return_value = []

        async def get(path, ids):
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "root_path": "",
                "query_string": urlencode([("ids", value) for value in ids]).encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("fixture", 80),
            }, receive, send)
            start = next(message for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            return start["status"], dict(start["headers"]), json.loads(body)

        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "logged_in", return_value="account-a"), \
             mock.patch.object(home, "conversation_service", return_value=service), \
             mock.patch.object(projects, "recent", return_value=[]):
            ids = ["c-" + str(index) for index in range(100)]
            status, headers, body = asyncio.run(get("/projects/history/status", ids))
            self.assertEqual(status, 200)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual(body, {"html": ""})
            service.public_index_by_ids.assert_called_once_with(ids, account_id="account-a")
            self.assertEqual(asyncio.run(get("/history/status", []))[0], 400)

    def test_status_renders_only_cards_and_uses_authenticated_unread_projection(self):
        service = mock.Mock()
        now = datetime.now(timezone.utc).isoformat()
        service.public_index_by_ids.return_value = [{
            "id": "one", "title": "完成但未读", "state": "idle", "unread": True,
            "project_id": "project-a", "pinned": False,
            "last_terminal_at": now, "activity_at": now, "revision": 8,
            # This must never appear in the HTML response even if a malformed
            # test double supplies persistence-only fields.
            "cwd": "/private/workspace", "settings": {"permission": "secret"},
        }]
        with mock.patch.object(home, "require_auth", return_value=None), \
             mock.patch.object(home, "logged_in", return_value="account-a"), \
             mock.patch.object(home, "conversation_service", return_value=service), \
             mock.patch.object(projects, "recent", return_value=[{
                 "id": "project-a", "name": "项目 A",
             }]):
            response = home.history_status(object(), ids=["one", "missing"])
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.public_index_by_ids.assert_called_once_with(
            ["one", "missing"], account_id="account-a",
        )
        self.assertEqual(set(payload), {"html"})
        self.assertIn("完成未读", payload["html"])
        self.assertIn("data-conversation-revision='8'", payload["html"])
        self.assertNotIn("/private/workspace", payload["html"])
        self.assertNotIn("secret", payload["html"])

    def test_project_status_rejects_invalid_ids_and_scopes_cards_to_project(self):
        service = mock.Mock()
        service.public_index_by_ids.return_value = [{
            "id": "inside", "title": "项目内", "state": "idle", "pinned": False,
        }]
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "get", return_value={"id": "project-a"}):
            response = project_routes.project_conversation_status(
                object(), "project-a", ids=["inside", "outside"],
            )
            invalid = project_routes.project_conversation_status(
                object(), "project-a", ids=["bad id"],
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        service.public_index_by_ids.assert_called_once_with(
            ["inside", "outside"], account_id="account-a", project_id="project-a",
        )
        self.assertIn("conversation-card-status", json.loads(response.body)["html"])
        self.assertEqual(invalid.status_code, 400)

    def test_project_status_auth_project_and_http_query_contract(self):
        denied = mock.Mock()
        with mock.patch.object(project_routes, "require_auth", return_value=RedirectResponse("/login", 303)), \
             mock.patch.object(project_routes, "conversation_service", return_value=denied):
            response = project_routes.project_conversation_status(object(), "project-a", ids=["one"])
        self.assertEqual(response.status_code, 401)
        denied.public_index_by_ids.assert_not_called()

        missing = mock.Mock()
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "conversation_service", return_value=missing), \
             mock.patch.object(projects, "get", side_effect=ValueError("missing")):
            response = project_routes.project_conversation_status(object(), "missing", ids=["one"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.body)["code"], "project_missing")
        missing.public_index_by_ids.assert_not_called()

        app = FastAPI()
        app.include_router(project_routes.router)
        service = mock.Mock()
        service.public_index_by_ids.return_value = []

        async def get(ids):
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": "/project/project-a/conversations/status",
                "raw_path": b"/project/project-a/conversations/status", "root_path": "",
                "query_string": urlencode([("ids", value) for value in ids]).encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("fixture", 80),
            }, receive, send)
            start = next(message for message in messages if message["type"] == "http.response.start")
            return start["status"]

        ids = ["card-" + str(index) for index in range(99)] + ["card-0"]
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.object(projects, "get", return_value={"id": "project-a"}):
            self.assertEqual(asyncio.run(get(ids)), 200)
        service.public_index_by_ids.assert_called_once_with(
            ids, account_id="account-a", project_id="project-a",
        )


class HistoryStatusServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-history-live-", dir=Path.home())
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(root / "state" / "conversations.sqlite3")
        self.service = ConversationService(
            self.store, ConversationArtifactService(self.store, root / "artifacts"), FakeRuntime(),
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def create_bound(self, conversation_id, project_id=None):
        self.store.create_conversation(
            conversation_id, title="公开卡片", cwd=str(self.workspace), settings={}, project_id=project_id,
        )
        self.store.bind_thread(
            conversation_id, {"id": "thread-" + conversation_id}, server_version="fixture",
        )

    def test_batch_is_current_read_only_filters_drafts_and_unread_is_per_account(self):
        self.create_bound("visible")
        self.store.create_conversation("draft", title="草稿", cwd=str(self.workspace), settings={})
        now = datetime.now(timezone.utc).isoformat()
        self.store._db.execute(
            "UPDATE conversations SET state = 'idle', terminal_generation = 1, "
            "last_terminal_at = ?, activity_at = ?, revision = 9 WHERE id = 'visible'",
            (now, now),
        )

        values = self.service.public_index_by_ids(
            ["missing", "visible", "draft", "visible"], account_id="account-a",
        )
        self.assertEqual([value["id"] for value in values], ["visible"])
        self.assertTrue(values[0]["unread"])
        self.assertEqual(values[0]["revision"], 9)
        self.assertNotIn("cwd", values[0])
        self.assertNotIn("owner_account_id", values[0])

        self.store.mark_read("visible", "account-a")
        self.assertFalse(self.service.public_index_by_ids(["visible"], account_id="account-a")[0]["unread"])
        self.assertTrue(self.service.public_index_by_ids(["visible"], account_id="account-b")[0]["unread"])

    def test_batch_calls_only_list_query_not_snapshot_read_or_runtime(self):
        store = mock.Mock()
        store.list_conversations_by_ids.return_value = []
        service = ConversationService(store, mock.Mock(), FakeRuntime())
        self.assertEqual(service.public_index_by_ids(["one", "one"], account_id="account-a"), [])
        store.list_conversations_by_ids.assert_called_once_with(["one"], account_id="account-a")
        store.snapshot.assert_not_called()
        store.mark_read.assert_not_called()

    def test_batch_rechecks_project_scope(self):
        self.create_bound("inside", "project-a")
        self.create_bound("outside", "project-b")
        values = self.service.public_index_by_ids(
            ["inside", "outside"], account_id="account-a", project_id="project-a",
        )
        self.assertEqual([value["id"] for value in values], ["inside"])


if __name__ == "__main__":
    unittest.main()
