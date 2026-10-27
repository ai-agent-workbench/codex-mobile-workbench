import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from routes import conversation_api
from shared.conversation_artifacts import ConversationArtifactService
from shared.conversation_export import content_disposition_filename, export_markdown
from tests.store_fixtures import seeded_store


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def startup_recover(self):
        self.calls.append(("startup", {}))
        return []

    async def close(self):
        self.calls.append(("close", {}))

    async def start_conversation(self, conversation_id, **values):
        self.calls.append(("start", {"conversation_id": conversation_id, **values}))
        return {"command": {"state": "accepted"}}

    async def delete_native_thread(self, conversation_id):
        self.calls.append(("delete", {"conversation_id": conversation_id}))

    async def update_settings(self, conversation_id, **values):
        self.calls.append(("settings", {"conversation_id": conversation_id, **values}))
        return {"command": {"state": "accepted"}, "snapshot": {}}


class ConversationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Unit tests must never reach the real cross-project task-notification
        # sender.  Individual notification tests can safely override this mock.
        self.notification_patcher = mock.patch(
            "shared.security.notify_task_completion",
            return_value=(False, "disabled_in_test"),
        )
        self.notification_patcher.start()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-conversation-service-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.artifacts = ConversationArtifactService(
            self.store, self.root / "artifacts"
        )
        self.runtime = FakeRuntime()
        self.service = ConversationService(
            self.store, self.artifacts, self.runtime
        )

    async def asyncTearDown(self):
        self.store.close()
        self.temporary.cleanup()
        self.notification_patcher.stop()

    def draft(self, **values):
        return self.service.create_draft(
            cwd=str(self.workspace),
            model="fixture",
            effort="medium",
            permission="danger-full-access",
            **values,
        )

    def complete_notifiable_turn(self, conversation_id):
        self.store.bind_thread(
            conversation_id, {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            conversation_id, "message-1", kind="start", text="done"
        )
        self.store.bind_turn(
            conversation_id,
            {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )
        self.store.apply_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "items": []},
            },
        })

    def public_items(self, items, status="completed"):
        return self.service._public_turn({
            "id": "turn-1", "ordinal": 1, "status": status,
            "kind": "model", "started_at": None, "completed_at": None,
            "final_text": None, "error": None, "notification_state": None,
            "artifact_publish_failed": False,
            "commands": [], "items": items, "artifacts": [],
        }, False)["items"]

    def test_public_turn_revision_tracks_content_not_only_ids_or_timestamps(self):
        turn = {
            "id": "turn-1", "ordinal": 1, "status": "completed", "kind": "model",
            "started_at": "fixed", "completed_at": "fixed", "final_text": "original",
            "commands": [], "artifacts": [], "items": [{
                "native_item_id": "item-1", "kind": "agent_message", "status": "completed",
                "phase": "final_answer", "text": "original", "created_at": "fixed", "updated_at": "fixed",
            }],
        }
        original = self.service._public_turn(turn, False)["presentation_revision"]
        self.assertEqual(original, self.service._public_turn(dict(reversed(list(turn.items()))), False)["presentation_revision"])
        turn["items"][0]["text"] = "corrected without a new ID or timestamp"
        corrected = self.service._public_turn(turn, False)["presentation_revision"]
        self.assertNotEqual(original, corrected)
        turn["status"] = "interrupted"
        self.assertNotEqual(corrected, self.service._public_turn(turn, False)["presentation_revision"])

    def test_public_async_agent_message_exposes_only_native_safe_fields(self):
        public = self.public_items([{
            "native_item_id": "call-async", "kind": "agent_message",
            "status": "completed", "phase": "final_answer",
            "delivery": "async", "text": "选择一个方案",
            "questions": [{"title": "采用哪项？", "options": ["方案 A", "方案 B"]}],
            "detail": None, "revision": 1, "created_at": "now", "updated_at": "now",
            "source": "thread_read", "questions_json": "private",
        }])
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]["delivery"], "async")
        self.assertEqual(
            public[0]["questions"],
            [{"title": "采用哪项？", "options": ["方案 A", "方案 B"]}],
        )
        self.assertNotIn("source", public[0])
        self.assertNotIn("questions_json", public[0])

    def test_public_async_question_exposes_only_durable_safe_status(self):
        draft = self.draft()
        self.store.bind_thread(draft["id"], {"id": "thread-1"}, server_version="fixture")
        self.store.begin_command(draft["id"], "start-async", kind="start", text="start")
        self.store.bind_turn(
            draft["id"], {"id": "turn-async", "status": "inProgress"},
            command_id="start-async",
        )
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-async",
                "item": {
                    "id": "async-item", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async", "text": "选择",
                    "questions": [{"title": "采用哪项？", "options": ["方案 A", "方案 B"]}],
                },
            },
        })
        question = self.service.public_snapshot(draft["id"])["turns"][0]["items"][0]["questions"][0]
        request = question["request"]
        self.assertEqual(request["state"], "awaiting_answer")
        self.assertTrue(request["id"])
        self.assertTrue(request["deadline_at"])
        self.assertNotIn("answer_text", request)
        self.assertNotIn("recommended_answer", request)

    async def test_start_titles_draft_and_keeps_native_ids_out_of_title(self):
        draft = self.draft()
        await self.service.start(
            draft["id"], command_id="message-1", text="  Build\n a report  "
        )
        conversation = self.store.get_conversation(draft["id"])
        self.assertEqual(conversation["title"], "Build a report")
        self.assertEqual(self.runtime.calls[-1][0], "start")

    def test_new_draft_marks_only_its_initial_native_title_without_schema_change(self):
        draft = self.draft(client_origin="mobile_web")
        self.assertIs(draft["settings"]["_native_initial_title"], True)
        raw = self.store._db.execute(
            "SELECT settings_json FROM conversations WHERE id=?", (draft["id"],)
        ).fetchone()[0]
        self.assertEqual(json.loads(raw), draft["settings"])

    def test_non_mobile_drafts_do_not_receive_native_initial_title_marker(self):
        for origin in (None, "desktop_web", "unknown"):
            with self.subTest(origin=origin):
                draft = self.draft(client_origin=origin)
                self.assertNotIn("_native_initial_title", draft["settings"])

    def test_message_preserves_arbitrary_length_after_trimming(self):
        message = "x" * 60_001
        self.assertEqual(self.service._message("  " + message + "  "), message)

    def test_message_rejects_non_strings_and_blank_values(self):
        for value in (None, 1, "", " \t\n"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AppServerProtocolError, "message must be non-empty"):
                    self.service._message(value)

    def test_public_snapshot_redacts_storage_and_machine_path(self):
        draft = self.draft()
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            draft["id"], "message-1", kind="start", text="make file"
        )
        self.store.bind_turn(
            draft["id"],
            {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )
        source = self.workspace / "notes.md"
        source.write_text("# Notes", encoding="utf-8")
        artifact = self.artifacts.publish(
            draft["id"],
            turn_id="turn-1",
            source_path=str(source),
            idempotency_key="tool:call-1",
            origin="dynamic_tool",
        )

        value = self.service.public_snapshot(
            draft["id"], include_text_previews=True
        )
        public = value["turns"][0]["artifacts"][0]
        self.assertNotIn("cwd", value["conversation"])
        self.assertEqual(
            value["conversation"]["context_compaction_completed_count"], 0
        )
        self.assertEqual(
            value["conversation"]["context_compaction_manual_completed_count"], 0
        )
        self.assertEqual(
            value["conversation"]["context_compaction_other_completed_count"], 0
        )
        self.assertEqual(value["conversation"]["context_compaction_state"], "idle")
        self.assertEqual(
            {
                key for key in value["conversation"]
                if key.startswith("context_compaction_")
            },
            {
                "context_compaction_completed_count",
                "context_compaction_manual_completed_count",
                "context_compaction_other_completed_count",
                "context_compaction_state",
            },
        )
        self.assertEqual(value["conversation"]["context_switch_count"], 0)
        self.assertEqual(value["conversation"]["context_switches"], [])
        self.assertNotIn("context_compaction_events", str(value))
        self.assertNotIn("native_item_id", str(value))
        self.assertNotIn("storage_relpath", public)
        self.assertNotIn("sha256", public)
        self.assertNotIn(str(source), str(value))
        self.assertEqual(public["preview_text"], "# Notes")
        self.assertTrue(public["content_url"].endswith(artifact["id"]))

    def test_public_snapshot_exposes_only_safe_context_switch_projection(self):
        draft = self.draft()
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            draft["id"], "message-1", kind="start", text="continue"
        )
        self.store.bind_turn(
            draft["id"], {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )
        call_hash = "b" * 64
        self.store.apply_notification({
            "method": "internal/context-switch/observed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "callKeySha256": call_hash, "state": "call_seen",
                "serverVersion": "0.153.1",
            },
        })
        self.store.apply_notification({
            "method": "internal/context-switch/observed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "callKeySha256": call_hash, "state": "output_seen",
                "serverVersion": "0.153.1",
            },
        })
        public = self.service.public_snapshot(draft["id"])["conversation"]
        self.assertEqual(public["context_switch_count"], 1)
        self.assertEqual(len(public["context_switches"]), 1)
        self.assertEqual(
            set(public["context_switches"][0]), {"observed_at", "state"}
        )
        self.assertNotIn(call_hash, str(public))
        self.assertNotIn("0.153.1", str(public))

    def test_public_snapshot_exposes_only_safe_artifact_failure_flag(self):
        draft = self.draft()
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            draft["id"], "message-1", kind="start", text="publish file"
        )
        self.store.bind_turn(
            draft["id"],
            {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )
        self.store.claim_tool_call(
            draft["id"], "turn-1", "call-failed", "publish_artifact",
            {"path": "/private/secret-name.txt"},
        )
        self.store.complete_tool_call("call-failed", {
            "contentItems": [{"type": "inputText", "text": "private detail"}],
            "success": False,
        })

        value = self.service.public_snapshot(draft["id"])
        self.assertTrue(value["turns"][0]["artifact_publish_failed"])
        self.assertNotIn("private", str(value))

    def test_export_transcript_uses_public_snapshot_without_runtime(self):
        snapshot = {
            "conversation": {
                "title": "完整\n对话", "project_id": "project-fixture",
                "state": "active", "cwd": "/private/machine/path",
            },
            "turns": [{
                "ordinal": 2, "status": "active", "final_text": "备用答案",
                "commands": [{
                    "id": "steer-2", "kind": "steer", "state": "accepted",
                    "text": "中途补充",
                }],
                "items": [{
                    "kind": "agent_message", "phase": "commentary",
                    "text": "过程消息不能导出",
                }],
                "artifacts": [],
            }, {
                "ordinal": 1, "status": "completed", "final_text": "不应重复",
                "commands": [{
                    "id": "start-1", "kind": "start", "state": "completed",
                    "text": "第一轮提问",
                }],
                "items": [{
                    "kind": "agent_message", "phase": "final_answer",
                    "text": "# 最终回答\n\n保留 Markdown。",
                }, {
                    "kind": "reasoning_summary", "phase": None,
                    "text": "不应导出",
                }],
                "artifacts": [{
                    "name": "/private/exports/report.md", "mime_type": "text/markdown",
                    "size": 42, "content_url": "/conversation/c1/artifact/a1",
                }],
            }],
            "messages": [{
                "id": "queued-3", "kind": "steer", "state": "queued",
                "text": "排队的中途补充",
            }, {
                "id": "rejected-4", "kind": "start", "state": "rejected",
                "text": "被拒绝的提问",
            }, {
                "id": "unknown-5", "kind": "steer", "state": "delivery_unknown",
                "text": "送达待确认的补充",
            }],
            "artifacts": [],
        }
        with (
            mock.patch.object(self.service, "public_snapshot", return_value=snapshot),
            mock.patch("projects.get", return_value={"name": "工作台维护"}),
        ):
            value = self.service.export_transcript("conversation-export")

        self.assertIn("所属项目：工作台维护", value["markdown"])
        self.assertIn("# 完整 对话", value["markdown"])
        self.assertLess(value["markdown"].index("## 第 1 轮"), value["markdown"].index("## 第 2 轮"))
        self.assertIn("第一轮提问", value["markdown"])
        self.assertIn("中途补充", value["markdown"])
        self.assertIn("# 最终回答\n\n保留 Markdown。", value["markdown"])
        self.assertEqual(value["markdown"].count("不应重复"), 0)
        self.assertIn("备用答案", value["markdown"])
        self.assertIn("已排队，尚未送达模型", value["markdown"])
        self.assertIn("已拒绝，未送达模型", value["markdown"])
        self.assertIn("送达状态无法确认", value["markdown"])
        self.assertIn("report.md（类型：text/markdown；大小：42 字节）", value["markdown"])
        self.assertNotIn("过程消息不能导出", value["markdown"])
        self.assertNotIn("不应导出", value["markdown"])
        self.assertNotIn("/private/machine/path", value["markdown"])
        self.assertNotIn("/conversation/c1/artifact/a1", value["markdown"])
        self.assertEqual(self.runtime.calls, [])

    def test_export_marks_active_without_final_and_safe_filename(self):
        value = export_markdown({
            "conversation": {"title": "标题", "state": "active"},
            "turns": [{"ordinal": 1, "status": "active", "commands": [], "items": []}],
            "messages": [], "artifacts": [],
        }, project_name="临时对话", exported_at=datetime.now(timezone.utc))
        header = content_disposition_filename('evil"\r\nX-Injected: yes/../name')
        self.assertIn("该轮仍在运行，尚无最终回答。", value)
        self.assertIn("filename=\"conversation-export.md\"", header)
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn('"\r', header)

    def test_public_usage_exposes_only_projected_token_fields(self):
        draft = self.draft()
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        usage = {
            "last": {
                "inputTokens": 250, "cachedInputTokens": 200,
                "outputTokens": 10, "reasoningOutputTokens": 3,
                "totalTokens": 263,
            },
            "total": {
                "inputTokens": 1000, "cachedInputTokens": 800,
                "outputTokens": 50, "reasoningOutputTokens": 15,
                "totalTokens": 1065,
            },
            "modelContextWindow": 2000,
            "updatedAt": "2026-08-25T10:00:00+00:00",
        }
        self.store.apply_notification({
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-1", "tokenUsage": usage},
        })
        public = self.service.public_snapshot(draft["id"])["conversation"]
        self.assertEqual(public["usage"], {
            "context_used": 250,
            "context_limit": 2000,
            "context_percent": 12.5,
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "output_tokens": 50,
            "reasoning_output_tokens": 15,
            "updated_at": "2026-08-25T10:00:00+00:00",
        })
        self.assertNotIn("token_usage", public)
        self.assertIsNone(self.service._public_conversation(draft)["usage"])

    def test_public_items_only_keep_agent_phases_and_dedupe_thread_read_copy(self):
        items = [
            {
                "native_item_id": "msg_live", "kind": "agent_message",
                "phase": "commentary", "text": "same\n text",
                "source": "notification_delta",
            },
            {
                "native_item_id": "item-1", "kind": "agent_message",
                "phase": "commentary", "text": "same text",
                "source": "thread_read",
            },
            {
                "native_item_id": "item-2", "kind": "reasoning_summary",
                "phase": None, "text": "private activity", "source": "thread_read",
            },
            {
                "native_item_id": "msg_final", "kind": "agent_message",
                "phase": "final_answer", "text": "final", "source": "notification_item",
            },
        ]
        public = self.public_items(items)
        self.assertEqual(
            [item["native_item_id"] for item in public],
            ["msg_live", "msg_final"],
        )
        reversed_public = self.public_items([items[1], items[0], items[3]])
        self.assertEqual(
            [item["native_item_id"] for item in reversed_public],
            ["msg_live", "msg_final"],
        )

    def test_running_turn_never_exposes_reasoning_summary(self):
        items = [
            {"native_item_id": "msg_live", "kind": "agent_message", "phase": "commentary", "text": "working", "source": "notification_item"},
            {"native_item_id": "rs_1", "kind": "reasoning_summary", "phase": None, "text": "older", "updated_at": "2026-08-25T10:00:00+00:00", "revision": 1},
            {"native_item_id": "rs_2", "kind": "reasoning_summary", "phase": None, "text": "latest", "updated_at": "2026-08-25T10:00:02+00:00", "revision": 1},
            {"native_item_id": "exec_1", "kind": "command_execution", "phase": None, "detail": "private command"},
        ]
        running = self.public_items(items, status="inProgress")
        self.assertEqual([item["native_item_id"] for item in running], ["msg_live"])
        self.assertEqual([item["native_item_id"] for item in self.public_items(items)], ["msg_live"])

    def test_native_plan_deliverable_is_visible_without_becoming_final_text(self):
        draft = self.draft()
        cid = draft["id"]
        self.store.bind_thread(cid, {"id": "thread-plan"}, server_version="fixture")
        self.store.begin_command(cid, "command-plan", kind="start", text="制定计划")
        self.store.bind_turn(cid, {"id": "turn-plan-result", "status": "inProgress"},
                             command_id="command-plan")
        self.store.apply_notification({
            "method": "turn/completed", "params": {
                "threadId": "thread-plan", "turn": {
                    "id": "turn-plan-result", "status": "completed", "items": [
                        {"id": "msg_progress", "type": "agentMessage", "phase": "commentary",
                         "text": "我再核一下完整命令清单。"},
                        {"id": "native-plan", "type": "plan", "text": "# 完整计划\n\n全部命令分组验收。"},
                    ],
                },
            },
        })
        public = self.service.public_snapshot(cid)
        self.assertEqual(public["conversation"]["state"], "idle")
        turn = public["turns"][0]
        self.assertIsNone(turn["final_text"])
        self.assertEqual([item["kind"] for item in turn["items"]], ["agent_message", "plan"])
        self.assertEqual(turn["items"][-1]["text"], "# 完整计划\n\n全部命令分组验收。")

    def test_plan_projection_excludes_internal_steps_superseded_sources_and_metadata(self):
        items = [
            {"native_item_id": "native-plan", "kind": "plan", "status": "inProgress",
             "text": "# 用户计划", "phase": "final_answer", "delivery": "async",
             "detail": "private detail", "questions": [{"secret": "private"}],
             "is_current_source": True},
            {"native_item_id": "turn-plan", "kind": "plan", "text": "内部步骤"},
            {"native_item_id": "obsolete-plan", "kind": "plan", "text": "已纠正旧计划",
             "is_current_source": False},
            {"native_item_id": "rs_plan", "kind": "reasoning_summary", "text": "private reasoning"},
            {"native_item_id": "exec_plan", "kind": "command_execution", "detail": "private command"},
        ]
        public = self.public_items(items, status="inProgress")
        self.assertEqual([item["native_item_id"] for item in public], ["native-plan"])
        self.assertEqual(public[0]["text"], "# 用户计划")
        for field in ("phase", "delivery", "detail", "questions"):
            self.assertIsNone(public[0][field])

    def test_browser_item_dedup_is_limited_to_matching_same_turn_phase_copy(self):
        base = {
            "kind": "agent_message", "text": "same text",
            "status": "completed", "detail": None, "revision": 1,
            "created_at": None, "updated_at": None,
        }
        different = self.public_items([
            base | {"native_item_id": "msg_live", "phase": "commentary", "source": "notification_delta"},
            base | {"native_item_id": "item-1", "phase": "commentary", "text": "different", "source": "thread_read"},
        ])
        self.assertEqual(len(different), 2)
        different_phase = self.public_items([
            base | {"native_item_id": "msg_live", "phase": "commentary", "source": "notification_delta"},
            base | {"native_item_id": "item-1", "phase": "final_answer", "source": "thread_read"},
        ])
        self.assertEqual(len(different_phase), 2)
        legacy = self.public_items([
            base | {"native_item_id": "msg_old", "phase": "commentary", "source": None},
            base | {"native_item_id": "item-1", "phase": "commentary", "source": None},
        ])
        self.assertEqual([item["native_item_id"] for item in legacy], ["msg_old"])
        no_item_number = self.public_items([
            base | {"native_item_id": "msg_old", "phase": "commentary", "source": None},
            base | {"native_item_id": "native-copy", "phase": "commentary", "source": None},
        ])
        self.assertEqual(len(no_item_number), 2)
        # Each turn is projected independently: matching text in another turn
        # is never used as a duplicate candidate.
        self.assertEqual(len(self.public_items([
            base | {"native_item_id": "msg_other_turn", "phase": "commentary", "source": "notification_delta"},
        ])), 1)
        self.assertEqual(len(self.public_items([
            base | {"native_item_id": "item-9", "phase": "commentary", "source": "thread_read"},
        ])), 1)

    def test_list_excludes_drafts_filters_project_and_pins(self):
        draft = self.draft(project_id="project-one")
        self.assertEqual(self.service.list_public(project_id="project-one"), [])
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        self.store.set_pinned([draft["id"]], True)
        values = self.service.list_public(project_id="project-one")
        self.assertEqual([value["id"] for value in values], [draft["id"]])
        self.assertTrue(values[0]["pinned"])

    def test_public_list_uses_account_read_generation(self):
        draft = self.draft()
        self.complete_notifiable_turn(draft["id"])
        self.assertTrue(
            self.service.list_public(account_id="account-a")[0]["unread"]
        )
        self.service.mark_read(draft["id"], "account-a")
        self.assertFalse(
            self.service.list_public(account_id="account-a")[0]["unread"]
        )

    def test_public_snapshot_hides_internal_settings_baseline(self):
        draft = self.draft()
        self.store.update_settings(draft["id"], {
            "pending_for_next_turn": True,
            "_pending_turn_settings_base": {
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
            },
        })
        settings = self.service.public_snapshot(draft["id"])["conversation"][
            "settings"
        ]
        self.assertTrue(settings["pending_for_next_turn"])
        self.assertNotIn("_pending_turn_settings_base", settings)

    async def test_latest_terminal_broadcasts_once_without_enabling_ntfy(self):
        draft = self.draft()
        self.complete_notifiable_turn(draft["id"])
        snapshot = self.store.snapshot(draft["id"])
        with mock.patch(
            "routes.ws.manager.broadcast", new=mock.AsyncMock()
        ) as broadcast:
            await self.service.handle_terminal(draft["id"], "turn-1", snapshot)
        broadcast.assert_awaited_once()
        event_type, payload = broadcast.await_args.args
        self.assertEqual(event_type, "conversation.terminal")
        self.assertEqual(payload["conversation_id"], draft["id"])
        self.assertEqual(payload["turn_id"], "turn-1")
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(payload["status"], "completed")

    async def test_existing_completion_channel_runs_before_web_push(self):
        draft = self.draft()
        self.complete_notifiable_turn(draft["id"])
        snapshot = self.store.snapshot(draft["id"])
        order = []

        async def completion(*_args, **_kwargs):
            order.append("completion")

        async def web_push(*_args):
            order.append("web_push")

        with (
            mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
            mock.patch.object(
                self.service,
                "_deliver_completion_notification",
                side_effect=completion,
            ),
            mock.patch.object(
                self.service,
                "_deliver_web_push_notification",
                side_effect=web_push,
            ),
        ):
            await self.service.handle_terminal(draft["id"], "turn-1", snapshot)
        self.assertEqual(order, ["completion", "web_push"])

    def test_completion_notification_claim_is_per_terminal_turn(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.assertTrue(
            self.store.claim_completion_notification(draft["id"], "turn-1")
        )
        self.assertFalse(
            self.store.claim_completion_notification(draft["id"], "turn-1")
        )
        self.store.record_completion_notification(
            draft["id"], "turn-1", sent=True, detail="HTTP 200"
        )
        turn = self.store.snapshot(draft["id"])["turns"][0]
        self.assertEqual(turn["notification_state"], "sent")

    async def test_startup_does_not_retry_uncertain_notification(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.assertTrue(self.store.claim_completion_notification(draft["id"], "turn-1"))
        with mock.patch("shared.security.notify_task_completion") as notify:
            await self.service.startup()
            await self.service.startup()
        notify.assert_not_called()
        turn = self.store.snapshot(draft["id"])["turns"][0]
        self.assertEqual(turn["notification_state"], "unknown")
        self.assertEqual(turn["notification_detail"], "delivery_uncertain_after_restart")

    async def test_startup_recovers_fresh_completion_once_across_reopen(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        with mock.patch("shared.security.notify_task_completion",
                        return_value=(True, "fixture")) as notify:
            await self.service.startup()
            self.store.close()
            self.store = ConversationStore(self.root / "state" / "conversations.sqlite3")
            self.service = ConversationService(self.store, ConversationArtifactService(self.store, self.root / "artifacts"), self.runtime)
            await self.service.startup()
        notify.assert_called_once()
        self.assertEqual(self.store.snapshot(draft["id"])["turns"][0]["notification_state"], "sent")

    async def test_pending_delete_suppresses_fresh_completion_across_reopen(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.store.set_conversation_state(
            draft["id"], "needs_user", recovery_state="delete_delivery_unknown",
            native_delete_pending=True,
        )
        self.store.close()
        self.store = ConversationStore(self.root / "state" / "conversations.sqlite3")
        self.service = ConversationService(
            self.store, ConversationArtifactService(self.store, self.root / "artifacts"), self.runtime,
        )
        self.assertEqual(self.store.pending_completion_notifications(), [])
        with mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify:
            await self.service.startup()
            await self.service.startup()
            notify.assert_not_called()
            self.assertTrue(self.store.get_conversation(draft["id"])["native_delete_pending"])
            self.assertIsNone(self.store.snapshot(draft["id"])["turns"][0]["notification_state"])
            # A definite rejection settles the intent; do not disable all
            # future notices merely because deletion was once requested.
            self.store.set_conversation_state(
                draft["id"], "idle", recovery_state="delete_rejected", native_delete_pending=False,
            )
            await self.service.startup()
            await self.service.startup()
            notify.assert_called_once()

    async def test_completion_claim_rechecks_delete_after_discovery(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.assertEqual(len(self.store.pending_completion_notifications()), 1)
        old_snapshot = self.store.snapshot(draft["id"])
        for flag, label in [(True, "transport_lost"), (False, "deleting_native_thread")]:
            with self.subTest(flag=flag, label=label):
                self.store._db.execute("UPDATE turns SET notification_state=NULL, notification_detail=NULL WHERE id='turn-1'")
                self.store.set_conversation_state(
                    draft["id"], "needs_user", recovery_state=label, native_delete_pending=flag,
                )
                with mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify:
                    await self.service._deliver_completion_notification(draft["id"], "turn-1", old_snapshot)
                    await self.service._deliver_completion_notification(draft["id"], "turn-1", old_snapshot, recovery=True)
                notify.assert_not_called()
                self.assertIsNone(self.store.snapshot(draft["id"])["turns"][0]["notification_state"])

    async def test_delayed_terminal_callback_rechecks_current_delete_intent(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        old_snapshot = self.store.snapshot(draft["id"])
        self.store.set_conversation_state(
            draft["id"], "needs_user", recovery_state="transport_lost", native_delete_pending=True,
        )
        with (
            mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()) as broadcast,
            mock.patch.object(self.service, "_deliver_completion_notification", new=mock.AsyncMock()) as completion,
            mock.patch.object(self.service, "_deliver_web_push_notification", new=mock.AsyncMock()) as push,
        ):
            await self.service.handle_terminal(draft["id"], "turn-1", old_snapshot)
            broadcast.assert_not_awaited()
            completion.assert_not_awaited()
            push.assert_not_awaited()
            self.store.set_conversation_state(
                draft["id"], "idle", recovery_state="delete_rejected", native_delete_pending=False,
            )
            await self.service.handle_terminal(draft["id"], "turn-1", old_snapshot)
            broadcast.assert_awaited_once()
            completion.assert_awaited_once()
            push.assert_awaited_once()

    async def test_historical_terminal_import_never_requests_notification(self):
        draft = self.draft(completion_notification=True)
        self.store.bind_thread(draft["id"], {"id": "thread-1"}, server_version="fixture")
        native = {"thread": {"id": "thread-1", "turns": [
            {"id": "history-" + str(i), "status": "completed", "items": []}
            for i in range(1, 5)
        ]}}
        self.store.reconcile_thread(draft["id"], native)
        self.store.reconcile_thread(draft["id"], native)
        self.assertTrue(all(not t["notification_requested"] for t in
                            self.store.snapshot(draft["id"])["turns"]))
        with mock.patch("shared.security.notify_task_completion") as notify:
            await self.service.startup()
        notify.assert_not_called()

    def test_reconcile_preserves_live_notification_intent(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.store.reconcile_thread(draft["id"], {"thread": {
            "id": "thread-1", "turns": [{"id": "turn-1", "status": "completed"}]}})
        self.assertTrue(self.store.claim_completion_notification(draft["id"], "turn-1"))

    def test_imported_active_turn_can_notify_when_it_finishes(self):
        draft = self.draft(completion_notification=True)
        self.store.bind_thread(draft["id"], {"id": "thread-1"}, server_version="fixture")
        for status in ("inProgress", "completed"):
            self.store.reconcile_thread(draft["id"], {"thread": {
                "id": "thread-1", "turns": [{"id": "turn-1", "status": status}]}})
        self.assertTrue(self.store.claim_completion_notification(draft["id"], "turn-1"))

    async def test_old_or_invalid_completion_is_not_sent_on_startup_or_direct_delivery(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        for timestamp in (None, "invalid", "2026-09-28T01:00:00+00:00",
                          "2026-09-28T02:00:01+00:00"):
            with self.subTest(timestamp=timestamp):
                self.store._db.execute("UPDATE turns SET completed_at=?", (timestamp,))
                with (mock.patch("engine.conversation_store.utc_now",
                                 return_value="2026-09-28T02:00:00+00:00"),
                      mock.patch("shared.security.notify_task_completion") as notify):
                    await self.service.startup()
                    await self.service._deliver_completion_notification(
                        draft["id"], "turn-1", self.store.snapshot(draft["id"]))
                notify.assert_not_called()

    def test_claim_rechecks_expiry_after_pending_scan(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.store._db.execute("UPDATE turns SET completed_at='2026-09-28T01:50:00+00:00'")
        with mock.patch("engine.conversation_store.utc_now", return_value="2026-09-28T02:00:00+00:00"):
            self.assertEqual(len(self.store.pending_completion_notifications()), 1)
        with mock.patch("engine.conversation_store.utc_now", return_value="2026-09-28T02:00:01+00:00"):
            self.assertFalse(self.store.claim_completion_notification(draft["id"], "turn-1"))

    def test_newer_active_turn_supersedes_pending_completion(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.store.begin_command(draft["id"], "message-2", kind="start", text="next")
        self.store.bind_turn(draft["id"], {"id": "turn-2", "status": "inProgress"}, command_id="message-2")
        self.assertEqual(self.store.pending_completion_notifications(), [])
        self.assertFalse(self.store.claim_completion_notification(draft["id"], "turn-1", recovery=True))
        # Queue advancement must not suppress the normal live per-turn callback.
        self.assertTrue(self.store.claim_completion_notification(draft["id"], "turn-1"))

    async def test_history_discovered_during_startup_is_not_sent(self):
        draft = self.draft(completion_notification=True)
        self.store.bind_thread(draft["id"], {"id": "thread-1"}, server_version="fixture")

        async def recover():
            self.store.reconcile_thread(draft["id"], {"thread": {
                "id": "thread-1", "turns": [
                    {"id": "turn-1", "status": "completed"}]}})
            return []

        with (mock.patch.object(self.runtime, "startup_recover", side_effect=recover),
              mock.patch("shared.security.notify_task_completion") as notify):
            await self.service.startup()
        notify.assert_not_called()

    async def test_live_terminal_statuses_notify_once(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        for status, label, tags in (("completed", "回复已完成", "white_check_mark"),
                                    ("failed", "回复失败", "warning"),
                                    ("interrupted", "回复已停止", "stop_sign")):
            with self.subTest(status=status):
                self.store._db.execute(
                    "UPDATE turns SET status=?, notification_state=NULL", (status,))
                with mock.patch("shared.security.notify_task_completion",
                                return_value=(True, "fixture")) as notify:
                    for _ in range(2):
                        await self.service._deliver_completion_notification(
                            draft["id"], "turn-1", self.store.snapshot(draft["id"]))
                notify.assert_called_once()
                self.assertEqual(notify.call_args.args[0], label)
                self.assertEqual(notify.call_args.kwargs["tags"], tags)

    async def test_failed_send_is_not_retried_on_restart(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        with mock.patch("shared.security.notify_task_completion",
                        return_value=(False, "fixture_failure")) as notify:
            await self.service.startup()
            await self.service.startup()
        notify.assert_called_once()
        self.assertEqual(self.store.snapshot(draft["id"])["turns"][0]["notification_state"], "failed")

    async def test_disabling_notifications_prevents_pending_delivery(self):
        draft = self.draft(completion_notification=True)
        self.complete_notifiable_turn(draft["id"])
        self.store.set_completion_notification(draft["id"], False)
        with mock.patch("shared.security.notify_task_completion") as notify:
            await self.service.startup()
        notify.assert_not_called()

    async def test_enabling_notification_does_not_replay_terminal_history(self):
        draft = self.draft(completion_notification=False)
        self.complete_notifiable_turn(draft["id"])

        await self.service.update_settings(
            draft["id"],
            command_id="settings-notification",
            updates={},
            completion_notification=True,
        )

        self.assertEqual(self.store.pending_completion_notifications(), [])
        self.assertFalse(
            self.store.claim_completion_notification(draft["id"], "turn-1")
        )

    async def test_notification_toggle_updates_the_active_turn(self):
        draft = self.draft(completion_notification=False)
        self.store.bind_thread(
            draft["id"], {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            draft["id"], "message-1", kind="start", text="done"
        )
        self.store.bind_turn(
            draft["id"],
            {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )

        await self.service.update_settings(
            draft["id"],
            command_id="settings-notification",
            updates={},
            completion_notification=True,
        )
        self.store.apply_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "items": []},
            },
        })

        self.assertTrue(
            self.store.claim_completion_notification(draft["id"], "turn-1")
        )

    async def test_completion_notification_identifies_project_round_and_omits_final_reply(self):
        draft = self.draft(
            completion_notification=True,
            project_id="project-fixture",
        )
        self.store.update_conversation_title(
            draft["id"], "  修复\n任务完成通知  "
        )
        self.complete_notifiable_turn(draft["id"])
        self.store._db.execute(
            "UPDATE turns SET final_text=?, ordinal=? WHERE id=?",
            ("最终 **结论**", 2, "turn-1"),
        )

        with (
            mock.patch("projects.get", return_value={"name": "工作台维护"}),
            mock.patch(
                "shared.security.notify_task_completion",
                return_value=(True, "sent"),
            ) as notify,
        ):
            await self.service._deliver_completion_notification(
                draft["id"], "turn-1", self.store.snapshot(draft["id"])
            )

        notify.assert_called_once_with(
            "回复已完成",
            "项目：工作台维护  \n任务：修复 任务完成通知  \n轮次：第 2 轮  \n请打开工作台查看详细结果。",
            tags="white_check_mark",
        )

    async def test_completion_notification_can_change_without_native_settings(self):
        draft = self.draft(completion_notification=True)
        value = await self.service.update_settings(
            draft["id"],
            command_id="settings-notification",
            updates={},
            completion_notification=False,
        )
        self.assertFalse(
            value["snapshot"]["conversation"]["completion_notification"]
        )
        self.assertNotIn("settings", [call[0] for call in self.runtime.calls])

    def test_invalid_draft_settings_do_not_create_workspace_or_record(self):
        with self.assertRaisesRegex(Exception, "permission"):
            self.service.create_draft(
                model="fixture",
                effort="medium",
                permission="invalid",
            )
        self.assertEqual(self.store.list_conversations(include_drafts=True), [])

    async def test_delete_removes_database_record_and_managed_artifacts(self):
        draft = self.draft()
        source = self.workspace / "result.txt"
        source.write_text("done", encoding="utf-8")
        artifact = self.artifacts.publish(
            draft["id"],
            turn_id=None,
            source_path=str(source),
            idempotency_key="detached",
            origin="dynamic_tool",
        )
        managed = self.artifacts.path_for(artifact)
        await self.service.delete(draft["id"])
        self.assertFalse(managed.exists())
        with self.assertRaisesRegex(Exception, "does not exist"):
            self.store.get_conversation(draft["id"])

    async def test_delete_removes_nonempty_private_workspace_only(self):
        private_root = self.root / "private-conversations"
        with mock.patch(
            "engine.conversation_service.CONVERSATION_WORKSPACES", private_root
        ):
            draft = self.service.create_draft(
                model="fixture",
                effort="medium",
                permission="danger-full-access",
            )
            workspace = private_root / draft["id"]
            (workspace / "uploaded.txt").write_text("private", encoding="utf-8")
            await self.service.delete(draft["id"])
        self.assertFalse(workspace.exists())

        project = self.draft()
        project_file = self.workspace / "keep.txt"
        project_file.write_text("project", encoding="utf-8")
        await self.service.delete(project["id"])
        self.assertTrue(project_file.exists())

    def test_abandoned_cleanup_removes_nonempty_private_workspace(self):
        private_root = self.root / "private-conversations"
        with mock.patch(
            "engine.conversation_service.CONVERSATION_WORKSPACES", private_root
        ):
            draft = self.service.create_draft(
                model="fixture",
                effort="medium",
                permission="danger-full-access",
            )
            workspace = private_root / draft["id"]
            (workspace / "uploaded.txt").write_text("private", encoding="utf-8")
            removed = self.service.cleanup_abandoned_drafts(maximum_age_hours=0)
        self.assertEqual(removed, 1)
        self.assertFalse(workspace.exists())

    async def test_private_workspace_delete_failure_keeps_retry_record(self):
        private_root = self.root / "private-conversations"
        with mock.patch(
            "engine.conversation_service.CONVERSATION_WORKSPACES", private_root
        ):
            draft = self.service.create_draft(
                model="fixture",
                effort="medium",
                permission="danger-full-access",
            )
            workspace = private_root / draft["id"]
            (workspace / "uploaded.txt").write_text("private", encoding="utf-8")
            with mock.patch(
                "engine.conversation_service.shutil.rmtree",
                side_effect=OSError("busy"),
            ):
                with self.assertRaisesRegex(OSError, "busy"):
                    await self.service.delete(draft["id"])
                removed = self.service.cleanup_abandoned_drafts(
                    maximum_age_hours=0
                )
                self.assertEqual(removed, 0)
                self.assertTrue(workspace.exists())
                self.assertEqual(
                    self.store.get_conversation(draft["id"])["id"], draft["id"]
                )
            await self.service.delete(draft["id"])
        self.assertFalse(workspace.exists())
        with self.assertRaisesRegex(Exception, "does not exist"):
            self.store.get_conversation(draft["id"])


class ConversationArtifactRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-conversation-route-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.artifacts = ConversationArtifactService(
            self.store, self.root / "artifacts"
        )
        self.service = ConversationService(
            self.store, self.artifacts, FakeRuntime()
        )
        self.conversation = self.service.create_draft(
            cwd=str(self.workspace),
            model="fixture",
            effort="medium",
            permission="danger-full-access",
        )
        self.request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "scheme": "https",
            "server": ("example.test", 443),
        })
        self.auth = mock.patch.object(conversation_api, "_auth", return_value=None)
        self.service_patch = mock.patch.object(
            conversation_api, "conversation_service", return_value=self.service
        )
        self.auth.start()
        self.service_patch.start()

    def tearDown(self):
        self.auth.stop()
        self.service_patch.stop()
        self.store.close()
        self.temporary.cleanup()

    def publish(self, name, content, key):
        source = self.workspace / name
        source.write_bytes(content)
        return self.artifacts.publish(
            self.conversation["id"],
            turn_id=None,
            source_path=str(source),
            idempotency_key=key,
            origin="dynamic_tool",
        )

    def response(self, artifact, download=False):
        return conversation_api.conversation_artifact(
            self.request,
            self.conversation["id"],
            artifact["id"],
            download,
        )

    def test_image_and_pdf_use_native_inline_browser_responses(self):
        image = self.publish("image.png", b"\x89PNG\r\n\x1a\nfixture", "image")
        pdf = self.publish("report.pdf", b"%PDF-1.7\nfixture", "pdf")
        for artifact in (image, pdf):
            response = self.response(artifact)
            self.assertIsInstance(response, FileResponse)
            self.assertIn("inline", response.headers["content-disposition"])
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_text_is_plain_and_office_is_always_attachment(self):
        text = self.publish("notes.md", b"# title", "text")
        office = self.publish("table.xlsx", b"PK\x03\x04fixture", "office")
        text_response = self.response(text)
        self.assertIsInstance(text_response, PlainTextResponse)
        self.assertEqual(text_response.body, b"# title")
        self.assertTrue(
            text_response.headers["content-type"].startswith("text/plain")
        )
        self.assertEqual(text_response.headers["cache-control"], "private, no-store")
        self.assertEqual(text_response.headers["x-content-type-options"], "nosniff")
        office_response = self.response(office)
        self.assertIsInstance(office_response, FileResponse)
        self.assertIn("attachment", office_response.headers["content-disposition"])
        self.assertEqual(office_response.media_type, "application/octet-stream")

    def test_html_preview_is_separate_sandboxed_response_and_download_stays_attachment(self):
        html = self.publish(
            "interactive.html",
            b"<!doctype html><button>fixture</button><script>void 0</script>",
            "html",
        )
        public = self.service.public_snapshot(self.conversation["id"])["artifacts"][0]
        self.assertEqual(public["preview_kind"], "html")
        self.assertTrue(public["preview_url"].endswith("/preview"))
        self.assertTrue(public["download_url"].endswith("?download=1"))

        preview = conversation_api.conversation_html_artifact_preview(
            self.request, self.conversation["id"], html["id"]
        )
        self.assertIsInstance(preview, FileResponse)
        self.assertIn("inline", preview.headers["content-disposition"])
        self.assertTrue(preview.headers["content-type"].startswith("text/html"))
        self.assertEqual(preview.headers["cache-control"], "private, no-store")
        self.assertEqual(preview.headers["x-content-type-options"], "nosniff")
        self.assertEqual(preview.headers["referrer-policy"], "no-referrer")
        self.assertIn("sandbox allow-scripts", preview.headers["content-security-policy"])
        self.assertIn("connect-src 'none'", preview.headers["content-security-policy"])
        self.assertIn("form-action 'none'", preview.headers["content-security-policy"])
        self.assertIn("camera=()", preview.headers["permissions-policy"])

        raw = self.response(html, download=False)
        self.assertIn("attachment", raw.headers["content-disposition"])
        self.assertEqual(raw.media_type, "application/octet-stream")
        download = self.response(html, download=True)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.media_type, "application/octet-stream")

        image = self.publish("image.png", b"\x89PNG\r\n\x1a\nfixture", "image-for-preview")
        wrong_kind = conversation_api.conversation_html_artifact_preview(
            self.request, self.conversation["id"], image["id"]
        )
        self.assertIsInstance(wrong_kind, JSONResponse)
        self.assertEqual(wrong_kind.status_code, 404)

    def test_download_forces_attachment_and_scope_mismatch_is_not_found(self):
        image = self.publish("image.png", b"\x89PNG\r\n\x1a\nfixture", "image")
        response = self.response(image, download=True)
        self.assertIn("attachment", response.headers["content-disposition"])
        mismatch = conversation_api.conversation_artifact(
            self.request, "another-conversation", image["id"], False
        )
        self.assertIsInstance(mismatch, JSONResponse)
        self.assertEqual(mismatch.status_code, 404)
        self.assertNotIn(str(self.root).encode(), mismatch.body)
        self.assertEqual(mismatch.headers["cache-control"], "private, no-store")


class ConversationArtifactAuthenticationTests(unittest.TestCase):
    def test_unauthenticated_artifact_request_never_reaches_storage(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/conversation/c1/artifact/a1",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "scheme": "https",
            "server": ("example.test", 443),
        })
        with mock.patch.object(
            conversation_api, "conversation_service"
        ) as service:
            response = conversation_api.conversation_artifact(
                request, "c1", "a1", False
            )
        self.assertIn(response.status_code, {303, 403})
        if response.status_code == 403:
            self.assertEqual(json.loads(response.body)["reason_code"], "authentication_required")
        else:
            self.assertEqual(response.headers["location"], "/setup")
        service.assert_not_called()

    def test_unauthenticated_html_preview_never_reaches_storage(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/conversation/c1/artifact/a1/preview",
            "headers": [(b"accept", b"text/html")],
            "client": ("127.0.0.1", 1234),
            "scheme": "https",
            "server": ("example.test", 443),
        })
        with mock.patch.object(
            conversation_api, "conversation_service"
        ) as service:
            response = conversation_api.conversation_html_artifact_preview(
                request, "c1", "a1"
            )
        self.assertEqual(response.status_code, 303)
        service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
