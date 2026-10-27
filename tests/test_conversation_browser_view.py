"""Bounded browser history remains compatible with the complete projection."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from tests.store_fixtures import seeded_store


def seed_history(store, count=65, *, items_per_turn=1):
    store.create_conversation("history-fixture", title="隔离历史", cwd="/tmp", settings={
        "model": "fixture", "effort": "medium", "permission": "danger-full-access"})
    store.bind_thread("history-fixture", {"id": "thread-fixture"}, server_version="fixture")
    with store._lock:
        store._db.execute("BEGIN")
        for ordinal in range(1, count + 1):
            turn_id = f"turn-{ordinal}"
            stamp = f"2026-10-01T00:{ordinal // 60:02}:{ordinal % 60:02}Z"
            text = f"第 {ordinal} 轮完整答复。" + "正文不能截断。" * 128
            store._db.execute("INSERT INTO turns(id,conversation_id,ordinal,status,started_at,completed_at,final_text) VALUES(?,?,?,?,?,?,?)", (turn_id, "history-fixture", ordinal, "completed", stamp, stamp, text))
            store._db.execute("INSERT INTO commands(id,conversation_id,kind,state,result_turn_id,text,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (f"command-{ordinal}", "history-fixture", "start", "completed", turn_id, f"第 {ordinal} 轮问题", "{}", stamp, stamp))
            for item_index in range(items_per_turn):
                final = item_index == items_per_turn - 1
                store._db.execute("INSERT INTO items(conversation_id,turn_id,native_item_id,kind,status,phase,text,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("history-fixture", turn_id, f"item-{ordinal}-{item_index}", "agent_message" if final else "command_execution", "completed", "final_answer" if final else None, text if final else "内部工具内容不公开", "notification", stamp, stamp))
        store._db.execute("UPDATE conversations SET state='idle',active_turn_id=NULL,last_terminal_turn_id=?,revision=revision+1 WHERE id='history-fixture'", (f"turn-{count}",))
        store._db.execute("COMMIT")


class BrowserViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = seeded_store(self.path / "db.sqlite3")
        self.service = ConversationService(self.store, ConversationArtifactService(self.store, self.path / "artifacts"), SimpleNamespace())
        seed_history(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_recent_window_and_all_pages_equal_full_projection(self):
        full = self.service.public_snapshot("history-fixture")
        pages = []
        cursor = ""
        with mock.patch.object(self.store, "snapshot", side_effect=AssertionError("full reader forbidden")):
            while True:
                page = self.service.public_browser_view("history-fixture", before=cursor)
                self.assertLessEqual(len(page["turns"]), 20)
                pages[0:0] = page["turns"]
                cursor = page["history"]["cursor"]
                if not cursor:
                    break
        self.assertEqual(pages, full["turns"])
        self.assertEqual(len({turn["id"] for turn in pages}), 65)
        self.assertNotIn("history", full)

    def test_large_projection_does_not_decode_unloaded_items_or_commands(self):
        baseline_size = len(json.dumps(self.service.public_browser_view("history-fixture")))
        self.store.close()
        self.store = seeded_store(self.path / "large.sqlite3")
        self.service.store = self.store
        seed_history(self.store, 500, items_per_turn=13)
        with mock.patch.object(self.store, "_decode_item", wraps=self.store._decode_item) as decode:
            page = self.service.public_browser_view("history-fixture")
        self.assertEqual(decode.call_count, 260)
        self.assertEqual(len(page["turns"]), 20)
        self.assertNotIn("内部工具内容", json.dumps(page, ensure_ascii=False))
        self.assertNotIn('"cwd"', json.dumps(page))
        self.assertLess(len(json.dumps(page)), baseline_size * 1.05)

    def test_tail_delta_does_not_invalidate_an_old_page_but_old_revision_does(self):
        recent = self.service.public_browser_view("history-fixture")
        old = self.service.public_browser_view("history-fixture", before=recent["history"]["cursor"])
        token = old["history"]["token"]
        self.store._db.execute("UPDATE items SET text=text||' tail',revision=revision+1 WHERE turn_id='turn-65'")
        unchanged = self.service.public_browser_view("history-fixture", pages=[token])
        self.assertEqual(unchanged["history"]["replacements"], [])
        self.store._db.execute("UPDATE items SET text=text||' corrected',revision=revision+1 WHERE turn_id='turn-30'")
        changed = self.service.public_browser_view("history-fixture", pages=[token])
        replacements = changed["history"]["replacements"]
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0]["previous"], token)
        turn = next(turn for turn in replacements[0]["snapshot"]["turns"] if turn["id"] == "turn-30")
        self.assertIn("corrected", turn["items"][0]["text"])

    def test_reconcile_and_restart_invalidate_fallback_only_history(self):
        old = self.service.public_browser_view("history-fixture", before=self.service.public_browser_view("history-fixture")["history"]["cursor"])
        token = old["history"]["token"]
        self.store.reconcile_thread("history-fixture", {"thread": {"id": "thread-fixture", "status": {"type": "idle"}, "turns": [{"id": f"turn-{i}", "status": "completed", "items": []} for i in range(1, 66)]}})
        self.assertEqual(len(self.service.public_browser_view("history-fixture", pages=[token])["history"]["replacements"]), 1)
        current = self.service.public_browser_view("history-fixture")["history"]["token"]
        self.store.close()
        self.store = ConversationStore(self.path / "db.sqlite3")
        self.service.store = self.store
        self.assertNotEqual(self.service.public_browser_view("history-fixture")["history"]["token"], current)

    def test_old_active_turn_and_pending_receipts_are_not_hidden(self):
        self.store._db.execute("UPDATE conversations SET active_turn_id='turn-1',state='active' WHERE id='history-fixture'")
        self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='turn-1'")
        self.store._db.execute("UPDATE commands SET state='delivery_unknown' WHERE id='command-2'")
        page = self.service.public_browser_view("history-fixture")
        self.assertEqual(len(page["turns"]), 21)
        self.assertIn("turn-1", [turn["id"] for turn in page["turns"]])
        self.assertIn("command-2", [command["id"] for command in page["messages"]])

    def test_changed_cursor_boundary_is_explicit_error_not_wrong_page(self):
        cursor = self.service.public_browser_view("history-fixture")["history"]["cursor"]
        self.store._db.execute("UPDATE turns SET ordinal=999 WHERE id='turn-46'")
        with self.assertRaisesRegex(ValueError, "boundary changed"):
            self.service.public_browser_view("history-fixture", before=cursor)
        with self.assertRaises(ValueError):
            self.service.public_browser_view("history-fixture", before="malformed!")

    def test_plan_candidate_before_window_matches_full_public_contract(self):
        self.store._db.execute("UPDATE items SET kind='plan',native_item_id='old-plan',text='完整计划' WHERE turn_id='turn-3'")
        full = self.service.public_snapshot("history-fixture")
        page = self.service.public_browser_view("history-fixture")
        self.assertEqual(page["plan_confirmation"], full["plan_confirmation"])
        self.assertEqual(page["plan_confirmation"]["turn_id"], "turn-3")
        self.assertEqual(len(page["turns"]), 20)
        self.assertEqual(len(page["plan_confirmation"]["supplements"]), 62)

    def test_empty_conversation_is_a_valid_page(self):
        self.store.create_conversation("empty-history", title="空历史", cwd="/tmp", settings={"model": "fixture"})
        page = self.service.public_browser_view("empty-history")
        self.assertEqual(page["turns"], [])
        self.assertIsNone(page["history"]["cursor"])


class BrowserViewRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_command_outside_window_has_read_only_id_receipt(self):
        from routes import conversation_api
        fixture = BrowserViewTests()
        fixture.setUp()
        try:
            # No native client/runtime methods exist: this route must only read.
            fixture.service.runtime.store = fixture.store
            page = fixture.service.public_browser_view("history-fixture")
            self.assertNotIn("command-1", json.dumps(page))
            with mock.patch.object(conversation_api, "_auth", return_value=None), mock.patch.object(
                    conversation_api, "conversation_service", return_value=fixture.service):
                response = await conversation_api.conversation_action_receipt(None, "history-fixture", "command-1")
                foreign = await conversation_api.conversation_action_receipt(None, "foreign", "command-1")
            data = json.loads(response.body)
            self.assertEqual((data["command"]["id"], data["command"]["state"]), ("command-1", "completed"))
            self.assertEqual(len(data["snapshot"]["turns"]), 65)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(foreign.status_code, 404)
        finally:
            fixture.tearDown()

    async def test_view_uses_existing_auth_private_headers_and_read_rate_bucket(self):
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from routes import conversation_api
        from middleware.rate_limit import RateLimitMiddleware, RateLimiter
        request = Request({"type": "http", "method": "GET", "path": "/conversation/fixture/view",
            "headers": [], "client": ("127.0.0.1", 1234), "scheme": "https", "server": ("example.test", 443)})
        rejected = JSONResponse({"error": "auth"}, status_code=401)
        service = mock.Mock()
        service.public_browser_view.return_value = {"history": {"mode": "paged"}}
        with mock.patch.object(conversation_api, "_auth", return_value=rejected), mock.patch.object(conversation_api, "conversation_service", return_value=service):
            self.assertIs(await conversation_api.conversation_browser_view(request, "fixture", text_previews=False), rejected)
            service.public_browser_view.assert_not_called()
        with mock.patch.object(conversation_api, "_auth", return_value=None), mock.patch.object(conversation_api, "conversation_service", return_value=service):
            response = await conversation_api.conversation_browser_view(request, "fixture", before="cursor", pages="1:20:version,21:40:version", text_previews=False)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            service.public_browser_view.assert_called_once_with("fixture", before="cursor", pages=["1:20:version", "21:40:version"], include_text_previews=False)
        limiter = RateLimitMiddleware(lambda *args: None)
        async def next_response(_request):
            return JSONResponse({"ok": True})
        with mock.patch.object(RateLimitMiddleware, "_api", RateLimiter(1)):
            self.assertEqual((await limiter.dispatch(request, next_response)).status_code, 200)
            self.assertEqual((await limiter.dispatch(request, next_response)).status_code, 429)
