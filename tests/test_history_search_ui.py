import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from engine.conversation_search import normalize_search_query, safe_search_snippet
from engine.conversation_store import ConversationStore
from routes import home, projects as project_routes
from shared.history_search import highlighted_text, search_cards
from shared.return_to import safe_conversation_return_to
from tests.history_search_fixtures import search_fixture


class HistorySearchUITests(unittest.TestCase):
    def setUp(self):
        self.fixture = search_fixture()
        self.store, self.service = self.fixture.__enter__()

    def tearDown(self):
        self.fixture.__exit__(None, None, None)

    def payload(self, query, *, project=None, cursor=None):
        response = (home.history_search(object(), q=query, cursor=cursor) if project is None
                    else project_routes.project_conversation_search(object(), project, q=query, cursor=cursor))
        return response, json.loads(response.body)

    def test_global_and_project_search_return_only_safe_scoped_html(self):
        response, data = self.payload("提问专属")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(set(data), {"html", "next_cursor"})
        self.assertIn("?command=command-", data["html"])
        self.assertIn("<mark>提问专属</mark>", data["html"])
        response, data = self.payload("outside", project="fixture")
        self.assertEqual(data, {"html": "", "next_cursor": None})
        self.assertEqual(self.payload("outside")[0].status_code, 200)
        self.assertIn("其他项目", self.payload("outside")[1]["html"])
        self.assertEqual(self.payload("共同关键词", project="missing")[0].status_code, 404)
        self.assertNotIn(str(self.store.path), self.payload("提问专属")[1]["html"])

    def test_final_search_targets_exact_item_and_title_search_targets_conversation(self):
        _, final = self.payload("回答专属 000")
        self.assertIn("?turn=turn-search-000&amp;item=item-search-000", final["html"])
        _, title = self.payload("示例 000")
        self.assertIn("href='/conversation/search-000'", title["html"])
        self.assertNotIn("?turn=", title["html"])

    def test_preserved_fallback_hit_and_timeline_ignore_superseded_body(self):
        _,result=self.payload("缓存命中专用短语")
        self.assertIn("?turn=turn-search-fallback-proof&amp;item=fallback",result["html"])
        raw=self.store.snapshot("search-fallback-proof")["turns"][0]
        self.assertEqual(len(raw["items"]),2)
        self.assertFalse(next(item for item in raw["items"] if item["kind"]=="agent_message")["is_current_source"])
        turn=self.service.public_snapshot("search-fallback-proof")["turns"][0]
        # The corrected native plan is now visible, but the superseded
        # answer must stay hidden and must not replace the trusted fallback.
        self.assertEqual([item["kind"] for item in turn["items"]],["plan"])
        self.assertEqual(turn["items"][0]["native_item_id"],"proof-B")
        self.assertTrue(turn["items"][0]["is_current_source"])
        self.assertEqual(turn["items"][0]["text"],"只是计划")
        self.assertIn("缓存命中专用短语",turn["final_text"])

    def test_search_targets_the_visible_notification_not_hidden_thread_read_copy(self):
        _,result=self.payload("副本精确定位关键词")
        self.assertIn("?turn=turn-search-duplicate-final&amp;item=msg_final",result["html"])
        turn=self.service.public_snapshot("search-duplicate-final")["turns"][0]
        self.assertEqual([item["native_item_id"] for item in turn["items"]],["msg_final"])

    def test_unauthenticated_search_does_not_touch_service(self):
        for module, function, args in (
            (home, home.history_search, (object(),)),
            (project_routes, project_routes.project_conversation_search, (object(), "fixture")),
        ):
            with mock.patch.object(module, "require_auth", return_value=RedirectResponse("/login", 303)), \
                 mock.patch.object(module, "conversation_service") as service:
                response = function(*args, q="private")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
                service.assert_not_called()

    def test_validation_and_cursor_query_scope_isolation(self):
        for query in ("", " ", "x" * 121, "bad\x00query", " ".join(["词"] * 9)):
            for project in (None, "fixture"):
                response, data = self.payload(query, project=project)
                self.assertEqual(response.status_code, 400, query)
                self.assertEqual(data["code"], "invalid_query")
        _, first = self.payload("共同关键词", project="fixture")
        self.assertTrue(first["next_cursor"])
        self.assertEqual(self.payload("提问专属", project="fixture", cursor=first["next_cursor"])[0].status_code, 409)
        self.assertEqual(self.payload("共同关键词", cursor=first["next_cursor"])[0].status_code, 409)

    def test_feff_is_literal_and_python_whitespace_is_not(self):
        literal = "\ufeff字面字符检索\ufeff"
        response, data = self.payload(literal)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["html"].count("class='card history-search-card'"), 1)
        self.assertIn("/conversation/search-feff", data["html"])
        self.assertNotIn("/conversation/search-no-feff", data["html"])
        self.assertEqual(normalize_search_query("\u0085" + literal + "\u001f").text, literal)
        for query, count in ((literal, 1), ("\u0085字面字符检索\u001f", 2)):
            self.assertEqual(self.payload(query)[1]["html"].count("class='card history-search-card'"), count)
            self.assertEqual(home.history(None, q=query).body.decode().count("class='card history-search-card'"), count)
            project = asyncio.run(project_routes.project_view(object(), "fixture", q=query))
            self.assertEqual(project.body.decode().count("class='card history-search-card'"), count)

    def test_ssr_controls_and_next_page_use_the_actual_normalized_query(self):
        query = " " * 120 + "共同关键词" + "\u0085" * 120
        for response in (home.history(None, q=query),
                         asyncio.run(project_routes.project_view(object(), "fixture", q=query))):
            self.assertEqual(response.status_code, 200)
            body = response.body.decode()
            self.assertEqual(body.count("class='card history-search-card'"), 20)
            self.assertIn("data-search-query='共同关键词'", body)
            self.assertIn("value='共同关键词' data-history-search-input", body)
            self.assertIn(urlencode({"q": "共同关键词"}) + "&amp;search_cursor=", body)

    def test_compact_global_header_has_native_collapsed_search_only(self):
        body = home.history(None).body.decode()
        self.assertIn("<header class='history-page-head'><h1>历史对话</h1>", body)
        self.assertNotIn("最近任务与执行状态", body)
        self.assertIn("data-search-disclosure><summary", body)
        self.assertIn("aria-label='搜索对话'", body)
        self.assertIn("placeholder='搜索对话'", body)
        self.assertIn("data-search-clear>取消</a>", body)
        self.assertIn("class='history-new-conversation' href='/quick'", body)
        self.assertNotIn("data-history-browse hidden", body)

    def test_project_and_global_search_use_accessible_in_field_icon_submission(self):
        for response in (home.history(None),
                         asyncio.run(project_routes.project_view(object(), "fixture"))):
            body = response.body.decode()
            self.assertIn("class='history-search-field search-field'", body)
            self.assertIn("type='submit' aria-label='提交搜索' title='搜索'><svg", body)
            self.assertIn("class='search-icon'", body)
            self.assertIn("aria-hidden='true'><circle", body)
            self.assertNotIn("<button type='submit'>搜索</button>", body)
        self.assertIn('<button class="send-button">开始</button>', body)
        self.assertNotIn("data-search-disclosure open", body)
        active = home.history(None, q="回答专属 000").body.decode()
        self.assertIn("data-search-disclosure open", active)
        self.assertIn("data-history-browse hidden", active)
        self.assertEqual(active.count("data-search-clear"), 1)
        project = asyncio.run(project_routes.project_view(object(), "fixture")).body.decode()
        self.assertNotIn("data-search-disclosure", project)
        self.assertIn("placeholder='搜索标题、提问和回答'", project)

    def test_server_rendered_search_and_clear_keep_project_composer(self):
        response = home.history(None, q="回答专属 000")
        body = response.body.decode()
        self.assertIn("data-history-browse hidden", body)
        self.assertIn("item=item-search-000", body)
        self.assertIn("data-search-clear", body)
        project = asyncio.run(project_routes.project_view(object(), "fixture", q="提问专属 000"))
        body = project.body.decode()
        self.assertIn('id="project-send"', body)
        self.assertIn("command=command-search-000", body)
        self.assertIn("/project/fixture/conversations/search", body)
        self.assertEqual(home.history(None, q="x" * 121).status_code, 400)
        self.assertNotIn("data-history-browse hidden", home.history(None, q="  ").body.decode())

    def test_http_routes_parse_q_and_cursor_without_native_calls(self):
        app = FastAPI()
        app.include_router(home.router)
        app.include_router(project_routes.router)

        async def get(path, query):
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "root_path": "", "query_string": urlencode(query).encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("fixture", 80),
            }, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
            return status, json.loads(body)

        for path in ("/history/search", "/projects/history/search", "/project/fixture/conversations/search"):
            status, data = asyncio.run(get(path, {"q": "回答专属 000"}))
            self.assertEqual(status, 200)
            self.assertIn("item-search-000", data["html"])

    def test_snippet_and_title_are_escaped_before_highlighting(self):
        self.store.update_conversation_title("search-000", "<script>needle</script>")
        _, data = self.payload("needle")
        self.assertNotIn("<script>needle", data["html"])
        self.assertIn("&lt;script&gt;<mark>needle</mark>&lt;/script&gt;", data["html"])
        self.assertEqual(highlighted_text("<img>", [[0, 5]]), "<mark>&lt;img&gt;</mark>")
        self.assertEqual(highlighted_text("<img>", [[-1, 3], [1, 999], [True, 3]]), "&lt;img&gt;")

    def test_exact_search_targets_survive_strict_login_recovery(self):
        for target in ("/conversation/search-000?command=command-search-000",
                       "/conversation/search-000?turn=turn-search-000&item=item-search-000",
                       "/conversation/a.b:c?turn=x.y:z&item=native:item"):
            self.assertEqual(safe_conversation_return_to(target), target)
        for target in ("//evil.test/", "/conversation/a?item=b", "/conversation/a?command=b&item=c",
                       "/conversation/a?turn=b&item=c&next=//evil.test", "/conversation/a?command=%61",
                       "/conversation/a?command=b#c", "/conversation/a?turn=b&item=c\\d"):
            self.assertIsNone(safe_conversation_return_to(target))

    def test_fts_only_loss_is_rebuilt_from_surviving_documents(self):
        path = self.store.path
        self.store.close()
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE conversation_search_fts")
        self.store = ConversationStore(path)
        # Update fixture-owned store reference for its final close.
        self.service.store = self.store
        self.assertEqual(self.store.search_conversation_ids("回答专属 000"), ["search-000"])
        self.store.close()

    def test_unicode_highlight_offsets_preserve_original_character_positions(self):
        text = "İABC中文 needle"
        snippet, ranges = safe_search_snippet(text, normalize_search_query("needle"))
        self.assertEqual(snippet[ranges[0][0]:ranges[0][1]], "needle")

    def test_remaining_page_rechecks_deleted_edited_and_reassigned_conversations(self):
        first = self.service.search_public_page("共同关键词", account_id="fixture-admin", project_id="fixture")
        ids = self.store.search_conversation_ids("共同关键词", project_id="fixture")
        remaining = [cid for cid in ids if cid not in {value["id"] for value in first["values"]}]
        self.store.delete_conversation(remaining[0])
        self.store._db.execute("UPDATE conversations SET project_id='outside' WHERE id=?", (remaining[1],))
        self.store._db.execute("UPDATE commands SET text='changed' WHERE conversation_id=?", (remaining[2],))
        self.store._db.execute("UPDATE items SET text='changed' WHERE conversation_id=?", (remaining[2],))
        self.store._db.execute("UPDATE turns SET final_text='changed' WHERE conversation_id=?", (remaining[2],))
        second = self.service.search_public_page("共同关键词", account_id="fixture-admin", project_id="fixture", cursor=first["next_cursor"])
        shown = {value["id"] for value in second["values"]}
        self.assertFalse(shown.intersection(remaining[:3]))
        self.assertEqual(len(second["values"]), 20)
        third = self.service.search_public_page("共同关键词", account_id="fixture-admin", project_id="fixture", cursor=second["next_cursor"])
        self.assertEqual(len(third["values"]), 2)
        self.assertIsNone(third["next_cursor"])


if __name__ == "__main__":
    unittest.main()
