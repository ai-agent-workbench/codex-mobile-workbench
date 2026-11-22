"""Scoped native-title supplementation never widens local history visibility."""
import tempfile
import unittest
from pathlib import Path

from engine.codex_app_server import AppServerDisconnected
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from shared.conversation_pagination import ConversationPageCursorError


class NativeTitleRuntime:
    def __init__(self, matches=None, error=None):
        self.matches = matches
        self.error = error
        self.calls = []

    async def search_scoped_thread_titles(self, thread_ids, search_term):
        self.calls.append((list(thread_ids), search_term))
        if self.error:
            raise self.error
        return None if self.matches is None else set(self.matches).intersection(thread_ids)


class NativeTitleSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-native-title-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = ConversationStore(self.root / "state" / "conversations.sqlite3")
        self.runtime = NativeTitleRuntime()
        self.service = ConversationService(
            self.store, ConversationArtifactService(self.store, self.root / "artifacts"), self.runtime,
        )

    async def asyncTearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, conversation_id, *, owner="owner", project=None, deleting=False):
        self.store.create_conversation(
            conversation_id, title="本地 " + conversation_id, cwd=str(self.workspace),
            project_id=project, settings={}, owner_account_id=owner,
        )
        self.store.bind_thread(
            conversation_id, {"id": "thread-" + conversation_id}, server_version="fixture",
        )
        if deleting:
            self.store._db.execute(
                "UPDATE conversations SET native_delete_pending=1 WHERE id=?", (conversation_id,)
            )

    async def test_owner_project_delete_and_second_pair_check_bound_native_request(self):
        self.add("allowed", project="project-a")
        self.add("other-project", project="project-b")
        self.add("other-owner", owner="other", project="project-a")
        self.add("ownerless", owner=None, project="project-a")
        self.add("deleting", project="project-a", deleting=True)
        self.runtime.matches = {"thread-allowed", "thread-other-project", "thread-other-owner"}

        page = await self.service.native_title_search_public_page(
            "标题", account_id="owner", project_id="project-a",
        )

        self.assertEqual(self.runtime.calls, [(["thread-allowed"], "标题")])
        self.assertEqual(page["checked_count"], 1)
        self.assertEqual(page["next_cursor"], None)
        self.assertEqual(page["values"], [{
            "id": "allowed", "title": "本地 allowed", "project_id": "project-a",
            "activity_at": page["values"][0]["activity_at"], "native_title_match": True,
        }])
        self.assertNotIn("thread_id", page["values"][0])

    async def test_empty_or_ownerless_scope_makes_no_native_rpc(self):
        self.add("ownerless", owner=None)
        for account in (None, "", "owner"):
            page = await self.service.native_title_search_public_page("标题", account_id=account)
            self.assertEqual(page["values"], [])
            self.assertEqual(page["checked_count"], 0)
        self.assertEqual(self.runtime.calls, [])

    async def test_batches_are_bounded_and_cursor_query_scope_cannot_leak(self):
        for number in range(101):
            self.add("c" + str(number).zfill(3))
        self.runtime.matches = {"thread-c000", "thread-c100"}

        first = await self.service.native_title_search_public_page("标题", account_id="owner")
        self.assertEqual(len(self.runtime.calls[0][0]), 100)
        self.assertTrue(first["next_cursor"])
        self.assertEqual(len(first["values"]), 1)
        with self.assertRaises(ConversationPageCursorError):
            await self.service.native_title_search_public_page(
                "另一查询", account_id="owner", cursor=first["next_cursor"],
            )
        self.assertEqual(len(self.runtime.calls), 1)

        second = await self.service.native_title_search_public_page(
            "标题", account_id="owner", cursor=first["next_cursor"],
        )
        self.assertEqual(len(self.runtime.calls[1][0]), 1)
        self.assertEqual(
            {value["id"] for value in first["values"] + second["values"]},
            {"c000", "c100"},
        )
        self.assertIsNone(second["next_cursor"])

    async def test_unsupported_and_rpc_failure_do_not_fallback_to_broader_native_search(self):
        self.add("allowed")
        unsupported = await self.service.native_title_search_public_page("标题", account_id="owner")
        self.assertFalse(unsupported["supported"])
        self.assertEqual(unsupported["values"], [])
        self.assertEqual(self.runtime.calls, [(["thread-allowed"], "标题")])

        self.runtime.error = AppServerDisconnected("test")
        with self.assertRaises(AppServerDisconnected):
            await self.service.native_title_search_public_page("标题", account_id="owner")
        self.assertEqual(self.runtime.calls[-1], (["thread-allowed"], "标题"))


if __name__ == "__main__":
    unittest.main()
