"""Focused behavioral coverage for first-turn AI conversation titles."""

import asyncio
import copy
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from engine.codex_app_server import AppServerRPCError
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from shared.conversation_artifacts import ConversationArtifactService
from shared.conversation_titles import TITLE_CONTEXT, TITLE_DESCRIPTION, TITLE_TOOL_NAME
from tests.store_fixtures import seeded_store
from tests.test_conversation_runtime import FakeClient, FakeFactory


class TitleSavingClient(FakeClient):
    """The shared runtime fake records requests; this variant models name state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title_set_entered = asyncio.Event()
        self.title_set_release: asyncio.Event | None = None
        self.title_set_block_name: str | None = None

    async def start_thread(self, **params):
        thread = await super().start_thread(**params)
        if "name" in thread:
            self.threads[thread["id"]]["name"] = thread["name"]
        return thread

    async def set_thread_name(self, thread_id, name):
        if name == self.title_set_block_name and self.title_set_release is not None:
            self.title_set_entered.set()
            await self.title_set_release.wait()
        await super().set_thread_name(thread_id, name)
        self.threads[thread_id]["name"] = name


class TitleSavingFactory(FakeFactory):
    def __call__(self, **handlers):
        client = TitleSavingClient(shared_threads=self.threads, **handlers)
        if self.connect_errors:
            client.connect_error = self.connect_errors.pop(0)
        self.clients.append(client)
        return client


class ConversationTitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-conversation-title-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.artifacts = ConversationArtifactService(self.store, self.root / "artifacts")
        self.factory = TitleSavingFactory()
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory)
        self.service = ConversationService(self.store, self.artifacts, self.runtime)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    def draft(self, *, origin="mobile_web"):
        return self.service.create_draft(cwd=str(self.workspace), client_origin=origin)

    async def start_mobile(self, text="请帮我整理这份报告"):
        draft = self.draft()
        result = await self.service.start(draft["id"], command_id="start-1", text=text)
        return draft["id"], result, self.factory.clients[0]

    @staticmethod
    def calls(client, method):
        return [params for called, params in client.calls if called == method]

    async def title_call(self, conversation_id, turn_id, call_id, title):
        return await self.runtime.handle_server_request("item/tool/call", {
            "threadId": self.store.get_conversation(conversation_id)["thread_id"],
            "turnId": turn_id,
            "callId": call_id,
            "tool": TITLE_TOOL_NAME,
            "arguments": {"title": title},
        })

    async def complete_turn(self, conversation_id, turn_id, *, status="completed"):
        conversation = self.store.get_conversation(conversation_id)
        thread = self.factory.threads[conversation["thread_id"]]
        native_turn = next(value for value in thread["turns"] if value["id"] == turn_id)
        native_turn.update({
            "status": status,
            "items": [{
                "id": "answer-" + turn_id,
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "done",
            }],
        })
        thread["status"] = {"type": "idle"}
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {"threadId": conversation["thread_id"], "turnId": turn_id,
                       "item": copy.deepcopy(native_turn["items"][0])},
        })
        await self.runtime.handle_notification({
            "method": "turn/completed",
            "params": {"threadId": conversation["thread_id"],
                       "turn": copy.deepcopy(native_turn)},
        })

    async def test_first_mobile_turn_adds_exact_title_instruction_and_one_tool(self):
        conversation_id, result, client = await self.start_mobile("分析销售情况")
        thread_start = self.calls(client, "thread/start")
        turns = self.calls(client, "turn/start")
        self.assertEqual(len(thread_start), 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(result["command"]["result_turn_id"], "turn-1")
        tools = [tool for tool in thread_start[0]["dynamic_tools"] if tool["name"] == TITLE_TOOL_NAME]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["description"], TITLE_DESCRIPTION)
        self.assertEqual(tools[0]["inputSchema"], {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        })
        self.assertTrue(turns[0]["leading_context_after_text"])
        self.assertEqual(turns[0]["leading_context"].count(TITLE_CONTEXT), 1)
        self.assertTrue(turns[0]["leading_context"].endswith(TITLE_CONTEXT))
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], "分析销售情况")
        public = self.service.public_snapshot(conversation_id)["conversation"]
        self.assertNotIn("_ai_initial_title", public["settings"])
        self.assertNotIn("_native_initial_title", public["settings"])

    async def test_model_title_preserves_long_special_text_and_syncs_web_and_native(self):
        conversation_id, _, client = await self.start_mobile("x" * 100)
        fallback = "x" * 72
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], fallback)
        generated = "《季度复盘》 / 特殊\n字符：" + "标题" * 120
        response = await self.title_call(conversation_id, "turn-1", "title-long", generated)
        self.assertTrue(response["success"])
        visible = json.loads(response["contentItems"][0]["text"])
        self.assertIs(visible["success"], True)
        self.assertIs(visible["changed"], True)
        self.assertEqual(visible["operation"], TITLE_TOOL_NAME)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], generated)
        self.assertEqual(self.factory.threads["thread-1"]["name"], generated)
        self.assertEqual(self.service.public_snapshot(conversation_id)["conversation"]["title"], generated)
        self.assertEqual(self.calls(client, "thread/name/set")[-1]["name"], generated)

    async def test_manual_same_text_before_auto_generation_permanently_wins(self):
        conversation_id, _, client = await self.start_mobile("保留这个首问标题")
        fallback = self.store.get_conversation(conversation_id)["title"]
        await self.service.update_metadata(conversation_id, title=fallback)
        response = await self.title_call(conversation_id, "turn-1", "title-after-manual", "模型标题")
        self.assertFalse(response["success"])
        visible = json.loads(response["contentItems"][0]["text"])
        self.assertIs(visible["success"], False)
        self.assertIs(visible["changed"], False)
        self.assertIs(visible["task_can_continue"], True)
        self.assertEqual(visible["operation"], TITLE_TOOL_NAME)
        self.assertNotIn("Artifact publishing failed", str(response))
        conversation = self.store.get_conversation(conversation_id)
        self.assertEqual(conversation["title"], fallback)
        self.assertEqual(conversation["settings"]["_ai_initial_title"], {"state": "manual"})
        self.assertEqual(self.factory.threads["thread-1"]["name"], fallback)
        self.assertNotIn("模型标题", [call["name"] for call in self.calls(client, "thread/name/set")])

    async def test_manual_unchanged_long_generated_title_is_preserved_everywhere(self):
        conversation_id, _, client = await self.start_mobile()
        title = "长标题" * 100
        self.assertTrue((await self.title_call(conversation_id, "turn-1", "title-full", title))["success"])
        await self.service.update_metadata(conversation_id, title=title)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], title)
        self.assertEqual(self.factory.threads["thread-1"]["name"], title)

    async def assert_title_does_not_write_after_stop(self, *, terminal):
        conversation_id, _, client = await self.start_mobile("保留首问")
        entered, release = asyncio.Event(), asyncio.Event()
        read_thread = client.read_thread

        async def blocked_read(*args, **kwargs):
            snapshot = await read_thread(*args, **kwargs)
            entered.set()
            await release.wait()
            return snapshot

        with mock.patch.object(client, "read_thread", side_effect=blocked_read):
            automatic = asyncio.create_task(self.title_call(conversation_id, "turn-1", "title-stop", "不应写入"))
            await asyncio.wait_for(entered.wait(), timeout=3)
            if terminal:
                await self.complete_turn(conversation_id, "turn-1", status="interrupted")
            else:
                await self.runtime.interrupt(conversation_id, command_id="stop-first", expected_turn_id="turn-1")
            name_count = len(self.calls(client, "thread/name/set"))
            release.set()
            self.assertFalse((await automatic)["success"])
        self.assertEqual(len(self.calls(client, "thread/name/set")), name_count)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], "保留首问")
        self.assertEqual(self.factory.threads["thread-1"]["name"], "保留首问")

    async def test_root_turn_stop_intent_during_native_read_prevents_title_write(self):
        await self.assert_title_does_not_write_after_stop(terminal=False)

    async def test_terminal_source_during_native_read_prevents_title_write(self):
        await self.assert_title_does_not_write_after_stop(terminal=True)

    async def test_desktop_stop_received_during_read_is_flushed_before_title_write(self):
        conversation_id, _, client = await self.start_mobile("保留首问")
        native_read, original_flush = client.read_thread, client.flush_notifications
        stop_received = False

        async def read_with_queued_desktop_stop(*args, **kwargs):
            nonlocal stop_received
            native = await native_read(*args, **kwargs)
            stop_received = True
            return native

        async def flush_queued_stop():
            nonlocal stop_received
            await original_flush()
            if stop_received:
                stop_received = False
                await self.complete_turn(conversation_id, "turn-1", status="interrupted")

        count = len(self.calls(client, "thread/name/set"))
        with mock.patch.object(client, "read_thread", side_effect=read_with_queued_desktop_stop), \
             mock.patch.object(client, "flush_notifications", side_effect=flush_queued_stop):
            response = await self.title_call(conversation_id, "turn-1", "title-desktop-stop", "不应写入")
        self.assertFalse(response["success"])
        self.assertEqual(len(self.calls(client, "thread/name/set")), count)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], "保留首问")

    async def test_authoritative_native_idle_blocks_title_even_without_stop_notification(self):
        conversation_id, _, client = await self.start_mobile("保留首问")
        self.factory.threads["thread-1"]["status"] = {"type": "idle"}
        count = len(self.calls(client, "thread/name/set"))
        response = await self.title_call(conversation_id, "turn-1", "title-native-idle", "不应写入")
        self.assertFalse(response["success"])
        self.assertEqual(len(self.calls(client, "thread/name/set")), count)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], "保留首问")

    async def test_manual_default_literal_before_first_message_is_not_replaced_by_fallback(self):
        conversation_id = self.draft()["id"]
        await self.service.update_metadata(conversation_id, title="新对话")
        await self.service.start(
            conversation_id, command_id="start-1", text="首问不能改掉人工默认名称"
        )
        conversation = self.store.get_conversation(conversation_id)
        self.assertEqual(conversation["title"], "新对话")
        self.assertEqual(conversation["settings"]["_ai_initial_title"], {"state": "manual"})
        self.assertEqual(self.factory.threads["thread-1"]["name"], "新对话")

    async def test_manual_rename_queued_while_auto_rpc_is_blocked_wins_everywhere(self):
        conversation_id, _, client = await self.start_mobile("初始标题")
        generated, manual = "模型生成标题", "人工最终标题"
        client.title_set_block_name = generated
        client.title_set_release = asyncio.Event()
        automatic = asyncio.create_task(
            self.title_call(conversation_id, "turn-1", "title-blocked", generated)
        )
        await asyncio.wait_for(client.title_set_entered.wait(), timeout=3)
        rename = asyncio.create_task(self.service.update_metadata(conversation_id, title=manual))
        await asyncio.sleep(0)
        self.assertFalse(rename.done())
        client.title_set_release.set()
        self.assertTrue((await automatic)["success"])
        await rename
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], manual)
        self.assertEqual(self.factory.threads["thread-1"]["name"], manual)
        self.assertEqual(self.service.public_snapshot(conversation_id)["conversation"]["title"], manual)

    async def test_existing_native_name_protects_fallback_from_auto_overwrite(self):
        client = await self.runtime._ensure_client()
        client.thread_start_name = "原生已有名称"
        conversation_id = self.draft()["id"]
        await self.service.start(conversation_id, command_id="start-1", text="首问兜底标题")
        response = await self.title_call(conversation_id, "turn-1", "title-protected", "模型不应覆盖")
        conversation = self.store.get_conversation(conversation_id)
        self.assertFalse(response["success"])
        self.assertEqual(conversation["title"], "首问兜底标题")
        self.assertEqual(self.factory.threads["thread-1"]["name"], "原生已有名称")
        self.assertEqual(conversation["settings"]["_ai_initial_title"]["state"], "protected")

    async def test_native_title_failure_keeps_first_turn_and_fallback(self):
        conversation_id, result, client = await self.start_mobile("首问不应失败")
        fallback = self.store.get_conversation(conversation_id)["title"]
        client.thread_name_error = AppServerRPCError(-32000, "reject title")
        response = await self.title_call(conversation_id, "turn-1", "title-native-fails", "不可保存")
        self.assertFalse(response["success"])
        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], fallback)
        self.assertEqual(self.store.get_conversation(conversation_id)["state"], "active")
        self.assertEqual(self.store.get_conversation(conversation_id)["settings"]["_ai_initial_title"]["state"], "failed")

    async def test_duplicate_call_is_cached_and_two_call_ids_allow_only_one_success(self):
        conversation_id, _, client = await self.start_mobile("可重复回执")
        first = await self.title_call(conversation_id, "turn-1", "title-one", "第一次标题")
        name_count = len(self.calls(client, "thread/name/set"))
        duplicate = await self.title_call(conversation_id, "turn-1", "title-one", "第一次标题")
        second = await self.title_call(conversation_id, "turn-1", "title-two", "第二次标题")
        self.assertTrue(first["success"])
        self.assertEqual(duplicate, first)
        self.assertFalse(second["success"])
        self.assertEqual(len(self.calls(client, "thread/name/set")), name_count)
        self.assertEqual(self.store.get_conversation(conversation_id)["title"], "第一次标题")

    async def test_only_first_turn_is_eligible_and_applying_marker_is_never_replayed(self):
        conversation_id, _, client = await self.start_mobile("首轮标题")
        await self.complete_turn(conversation_id, "turn-1")
        second = await self.service.send(
            conversation_id, command_id="turn-2", text="第二轮", force_new_turn=True
        )
        self.assertEqual(second["command"]["result_turn_id"], "turn-2")
        rejected = await self.title_call(conversation_id, "turn-2", "title-second-turn", "第二轮标题")
        self.assertFalse(rejected["success"])
        before = len(self.calls(client, "thread/name/set"))
        marker = self.store.get_conversation(conversation_id)["settings"]["_ai_initial_title"]
        self.store.update_settings(conversation_id, {
            "_ai_initial_title": {**marker, "state": "applying", "call_id": "crashed-call"}
        })
        await self.runtime.startup_recover()
        self.assertEqual(len(self.calls(client, "thread/name/set")), before)
        self.assertEqual(
            self.store.get_conversation(conversation_id)["settings"]["_ai_initial_title"]["state"],
            "applying",
        )

    async def test_new_marker_and_tool_are_not_backfilled_to_old_or_nonmobile_conversations(self):
        mobile = self.draft()
        desktop = self.draft(origin="desktop_web")
        self.store.create_conversation(
            "old-mobile", title="旧草稿", cwd=str(self.workspace), client_origin="mobile_web",
            settings={"model": "fixture", "effort": "medium", "permission": "danger-full-access",
                      "service_tier": None, "collaboration_mode": "default"},
        )
        self.assertIn("_ai_initial_title", mobile["settings"])
        self.assertNotIn("_ai_initial_title", desktop["settings"])
        self.assertNotIn("_ai_initial_title", self.store.get_conversation("old-mobile")["settings"])
        for conversation_id, command_id in ((desktop["id"], "desktop-start"), ("old-mobile", "old-start")):
            await self.service.start(conversation_id, command_id=command_id, text="不应注册标题工具")
        starts = self.calls(self.factory.clients[0], "thread/start")
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(
            all(tool["name"] != TITLE_TOOL_NAME for tool in params["dynamic_tools"])
            for params in starts
        ))


if __name__ == "__main__":
    unittest.main()
