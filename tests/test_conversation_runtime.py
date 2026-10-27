import asyncio
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import (
    AppServerError,
    AppServerDisconnected,
    AppServerProtocolError,
    AppServerRPCError,
    CodexAppServerClient,
)
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from tests.store_fixtures import seeded_store


MODEL = {
    "catalog_id": "fixture",
    "model": "fixture-model",
    "reasoning_effort": "medium",
    "service_tiers": [{"id": "priority"}],
}


class FakeClient:
    def __init__(
        self,
        *,
        notification_handler=None,
        server_request_handler=None,
        disconnect_handler=None,
        shared_threads=None,
    ):
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.disconnect_handler = disconnect_handler
        self.connected = False
        self.server_version = "fixture"
        self.connection_epoch = 0
        self.threads = shared_threads if shared_threads is not None else {}
        self.calls = []
        self.turn_no = sum(len(value.get("turns", [])) for value in self.threads.values())
        self.steer_error = None
        self.start_turn_error = None
        self.interrupt_error = None
        self.delete_error_before_delivery = None
        self.delete_error_after_delivery = None
        self.notify_turn_started_before_response = False
        self.notify_turn_completed_before_response = False
        self.thread_start_error_after_notification = None
        self.thread_start_error_without_notification = None
        self.pre_thread_start_notification = None
        self.supports_thread_name_set = True
        self.thread_start_name = None
        self.thread_name_error = None
        self.thread_name_connection_lost = False
        self.connect_error = None

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.connection_epoch += 1
        self.calls.append(("connect", {}))
        return {}

    async def close(self):
        self.connected = False

    async def resolve_model_selection(self, model, effort, force_refresh=True):
        self.calls.append(("resolve_model", {"model": model, "effort": effort}))
        selected = copy.deepcopy(MODEL)
        selected["reasoning_effort"] = effort or selected["reasoning_effort"]
        return selected

    async def resolve_skill_inputs(self, cwd, selectors, force_reload=True):
        return [
            {"type": "skill", "name": str(value), "path": f"{cwd}/{value}/SKILL.md"}
            for value in selectors
        ]

    async def start_thread(self, **params):
        self.calls.append(("thread/start", copy.deepcopy(params)))
        if self.pre_thread_start_notification is not None and self.notification_handler:
            await self.notification_handler({
                "method": "thread/started",
                "params": {"thread": copy.deepcopy(self.pre_thread_start_notification)},
            })
        thread_id = f"thread-{len(self.threads) + 1}"
        self.threads[thread_id] = {
            "id": thread_id,
            "cwd": params["cwd"],
            "status": {"type": "idle"},
            "turns": [],
        }
        thread = {"id": thread_id, "cwd": params["cwd"], "turns": []}
        if self.thread_start_name is not None:
            thread["name"] = self.thread_start_name
        if self.thread_start_error_after_notification is not None:
            if self.notification_handler:
                await self.notification_handler({
                    "method": "thread/started",
                    "params": {"thread": copy.deepcopy(thread)},
                })
            self.connected = False
            raise self.thread_start_error_after_notification
        if self.thread_start_error_without_notification is not None:
            self.connected = False
            raise self.thread_start_error_without_notification
        return thread

    async def start_review(self, thread_id, target, *, delivery="inline"):
        self.calls.append(("review/start", {"thread_id": thread_id, "target": target, "delivery": delivery}))
        self.turn_no += 1
        turn = {
            "id": f"review-turn-{self.turn_no}", "status": "completed",
            "items": [{"id": "review-result", "type": "exitedReviewMode", "review": "official findings"}],
        }
        self.threads[thread_id]["turns"].append(copy.deepcopy(turn))
        return {"reviewThreadId": thread_id, "turn": {**turn, "status": "inProgress"}}

    async def resume_thread(self, thread_id):
        self.calls.append(("thread/resume", {"thread_id": thread_id}))
        if thread_id not in self.threads:
            raise AppServerRPCError(-32600, f"thread not loaded: {thread_id}")
        return {"thread": copy.deepcopy(self.threads[thread_id])}

    async def read_thread(self, thread_id, include_turns=True):
        self.calls.append(("thread/read", {"thread_id": thread_id}))
        if thread_id not in self.threads:
            raise AppServerRPCError(-32600, f"thread not loaded: {thread_id}")
        return {"thread": copy.deepcopy(self.threads[thread_id])}

    async def flush_notifications(self):
        self.calls.append(("notifications/flush", {}))

    async def read_thread_item(self, thread_id, turn_id, item_id, *, predecessor_id=None):
        self.calls.append(("thread/items/list", {
            "thread_id": thread_id, "turn_id": turn_id, "item_id": item_id,
            "predecessor_id": predecessor_id,
        }))
        thread = self.threads.get(thread_id)
        if thread is None:
            raise AppServerRPCError(-32600, f"thread not loaded: {thread_id}")
        matches = [
            item for turn in thread.get("turns", [])
            if turn.get("id") == turn_id
            for item in turn.get("items", [])
            if item.get("id") == item_id
        ]
        if len(matches) != 1:
            raise AppServerProtocolError("fixture item is unavailable")
        return copy.deepcopy(matches[0])

    async def thread_exists(self, thread_id, *, cwd, page_limit=100):
        self.calls.append(("thread/list", {"thread_id": thread_id, "cwd": cwd}))
        return thread_id in self.threads

    async def start_turn(
        self,
        thread_id,
        text,
        *,
        client_user_message_id=None,
        skill_inputs=None,
        leading_context=None,
        leading_context_after_text=None,
        options=None,
    ):
        self.calls.append(("turn/start", {
            "thread_id": thread_id,
            "text": text,
            "client_user_message_id": client_user_message_id,
            "skill_inputs": copy.deepcopy(skill_inputs),
            "leading_context": leading_context,
            "leading_context_after_text": leading_context_after_text,
            "options": copy.deepcopy(options),
        }))
        if self.start_turn_error is not None:
            raise self.start_turn_error
        self.turn_no += 1
        turn = {
            "id": f"turn-{self.turn_no}",
            "status": "inProgress",
            "items": [{
                "id": f"user-{self.turn_no}",
                "type": "userMessage",
                "content": [{"type": "text", "text": text}],
                "clientUserMessageId": client_user_message_id,
            }],
        }
        self.threads[thread_id]["turns"].append(copy.deepcopy(turn))
        self.threads[thread_id]["status"] = {"type": "active"}
        if self.notify_turn_started_before_response and self.notification_handler:
            await self.notification_handler({
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": copy.deepcopy(turn)},
            })
        if self.notify_turn_completed_before_response and self.notification_handler:
            completed = copy.deepcopy(turn)
            completed["status"] = "completed"
            completed["items"] = [{
                "id": f"answer-{self.turn_no}",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "fast",
            }]
            self.threads[thread_id]["turns"][-1] = copy.deepcopy(completed)
            self.threads[thread_id]["status"] = {"type": "idle"}
            await self.notification_handler({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": completed["id"],
                    "item": copy.deepcopy(completed["items"][0]),
                },
            })
            await self.notification_handler({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": completed},
            })
        return copy.deepcopy(turn)

    async def steer_turn(
        self,
        thread_id,
        expected_turn_id,
        text,
        *,
        client_user_message_id=None,
        skill_inputs=None,
        leading_context=None,
    ):
        self.calls.append(("turn/steer", {
            "thread_id": thread_id,
            "turn_id": expected_turn_id,
            "text": text,
            "client_user_message_id": client_user_message_id,
            "leading_context": leading_context,
        }))
        if self.steer_error is not None:
            raise self.steer_error
        return expected_turn_id

    async def interrupt_turn(self, thread_id, turn_id):
        self.calls.append(("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}))
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def set_thread_name(self, thread_id, name):
        self.calls.append(("thread/name/set", {"thread_id": thread_id, "name": name}))
        if self.thread_name_error is not None:
            if (
                isinstance(self.thread_name_error, AppServerDisconnected)
                or self.thread_name_connection_lost
            ):
                self.connected = False
                if self.disconnect_handler is not None:
                    self.disconnect_handler(
                        self.connection_epoch, "reader_eof", "EOF"
                    )
            raise self.thread_name_error

    async def update_thread_settings(self, thread_id, service_tier):
        self.calls.append(("thread/settings/update", {"service_tier": service_tier}))

    async def compact_thread(self, thread_id):
        self.calls.append(("thread/compact/start", {"thread_id": thread_id}))

    async def delete_thread(self, thread_id):
        self.calls.append(("thread/delete", {"thread_id": thread_id}))
        if self.delete_error_before_delivery is not None:
            raise self.delete_error_before_delivery
        self.threads.pop(thread_id, None)
        if self.delete_error_after_delivery is not None:
            raise self.delete_error_after_delivery


class FakeFactory:
    def __init__(self):
        self.threads = {}
        self.clients = []
        self.connect_errors = []

    def __call__(self, **handlers):
        client = FakeClient(shared_threads=self.threads, **handlers)
        if self.connect_errors:
            client.connect_error = self.connect_errors.pop(0)
        self.clients.append(client)
        return client


class ConversationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-conversation-runtime-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.artifacts = ConversationArtifactService(
            self.store, self.root / "managed-artifacts"
        )
        self.factory = FakeFactory()
        self.statuses = []
        self.terminals = []
        self.runtime = ConversationRuntime(
            self.store,
            self.artifacts,
            self.factory,
            status_handler=lambda conversation_id, snapshot: self.statuses.append(
                (conversation_id, snapshot["conversation"]["revision"])
            ),
            terminal_handler=lambda conversation_id, turn_id, snapshot: self.terminals.append(
                (conversation_id, turn_id)
            ),
        )
        self.store.create_conversation(
            "conversation-1",
            title="native",
            cwd=str(self.workspace),
            settings={
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": None,
            },
        )

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    async def start(self):
        return await self.runtime.start_conversation(
            "conversation-1",
            command_id="message-1",
            text="first",
        )

    def enable_native_initial_title(self, title="Web generated title"):
        self.store.update_conversation_title("conversation-1", title)
        self.store.update_settings(
            "conversation-1", {"_native_initial_title": True}
        )
        self.store._db.execute(
            "UPDATE conversations SET client_origin='mobile_web' WHERE id=?",
            ("conversation-1",),
        )

    @staticmethod
    def native_name_calls(client):
        return [params for method, params in client.calls if method == "thread/name/set"]

    async def complete_turn(self, turn_id="turn-1", text="done", status="completed"):
        thread = self.factory.threads["thread-1"]
        native_turn = next(value for value in thread["turns"] if value["id"] == turn_id)
        native_turn.update({
            "status": status,
            "items": [{
                "id": "answer-" + turn_id,
                "type": "agentMessage",
                "phase": "final_answer",
                "text": text,
            }],
        })
        thread["status"] = {"type": "idle"}
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": turn_id,
                "item": copy.deepcopy(native_turn["items"][0]),
            },
        })
        await self.runtime.handle_notification({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": copy.deepcopy(native_turn)},
        })

    async def test_startup_repairs_old_terminal_regression_without_replay(self):
        await self.start()
        await self.complete_turn(status="interrupted")
        while self.runtime._background:
            await asyncio.gather(*tuple(self.runtime._background))
        await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="continue",
        )
        # Persisted native history can retain the pre-restart active status
        # after Web already observed an interrupted terminal for the old turn.
        self.factory.threads["thread-1"]["turns"][0]["status"] = "inProgress"
        self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='turn-1'")
        self.store.set_conversation_state(
            "conversation-1", "needs_user", recovery_state="ambiguous_native_state",
            active_turn_id=None,
        )
        before = self.store.snapshot("conversation-1")
        self.terminals.clear()
        before_mutations = [
            call for client in self.factory.clients for call in client.calls
            if call[0] in {"turn/start", "turn/steer", "turn/interrupt"}
        ]
        with mock.patch.object(self.runtime, "_settle_terminal", new=mock.AsyncMock()) as settle:
            await self.runtime.startup_recover()
            await asyncio.sleep(0)
            await self.runtime.reconcile("conversation-1")
            await asyncio.sleep(0)
            settle.assert_not_awaited()
        value = self.store.snapshot("conversation-1")
        self.assertEqual(value["conversation"]["state"], "active")
        self.assertEqual(value["conversation"]["active_turn_id"], "turn-2")
        self.assertEqual(value["turns"][0]["status"], "interrupted")
        self.assertEqual(value["turns"][0]["completed_at"], before["turns"][0]["completed_at"])
        self.assertEqual(value["conversation"]["terminal_generation"],
                         before["conversation"]["terminal_generation"])
        self.assertEqual(self.terminals, [])
        after_mutations = [
            call for client in self.factory.clients for call in client.calls
            if call[0] in {"turn/start", "turn/steer", "turn/interrupt"}
        ]
        self.assertEqual(after_mutations, before_mutations)

    async def test_start_uses_one_thread_turn_and_publish_tool(self):
        result = await self.start()
        client = self.factory.clients[0]
        start_params = next(params for method, params in client.calls if method == "thread/start")
        self.assertEqual(start_params["dynamic_tools"][0]["name"], "publish_artifact")
        self.assertEqual(start_params["dynamic_tools"][1]["name"], "code_review")
        self.assertEqual(
            set(start_params), {"cwd", "model", "sandbox", "dynamic_tools"}
        )
        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(result["snapshot"]["conversation"]["active_turn_id"], "turn-1")
        self.assertEqual(len(result["snapshot"]["turns"]), 1)
        turn = next(params for method, params in client.calls if method == "turn/start")
        self.assertIn("manage_scheduled_task", turn["leading_context"])
        self.assertEqual(start_params["dynamic_tools"][2]["name"], "manage_scheduled_task")
        self.assertTrue(all(tool.get("type") == "function" for tool in start_params["dynamic_tools"]))

    async def test_code_review_tool_uses_isolated_read_only_native_thread(self):
        await self.start()
        with mock.patch("engine.conversation_review.repository_fingerprint", return_value={
            "root": str(self.workspace), "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0,
        }), mock.patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope):
            response = await self.runtime.handle_server_request(
                "item/tool/call",
                {
                    "threadId": "thread-1", "turnId": "turn-1", "callId": "review-call-1",
                    "tool": "code_review", "arguments": {
                        "instructions": "review the change", "scope": {"type": "uncommittedChanges"},
                    },
                },
            )
        self.assertTrue(response["success"])
        self.assertIn("official findings", response["contentItems"][0]["text"])
        reviewer = self.factory.clients[-1]
        start = next(params for method, params in reviewer.calls if method == "thread/start")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertEqual(start["review_reasoning_effort"], "high")
        self.assertEqual(start["dynamic_tools"], [])
        self.assertTrue(start["review_mode"])
        self.assertIn("review the change", start["review_instructions"])
        self.assertIn(("resolve_model", {"model": "fixture-model", "effort": "high"}), reviewer.calls)
        self.assertNotEqual(next(params for method, params in reviewer.calls if method == "review/start")["thread_id"], "thread-1")

    async def test_review_crash_reservation_survives_empty_history_until_terminal(self):
        await self.start()
        self.store.claim_tool_call("conversation-1", "turn-1", "crashed-review", "code_review", {})
        self.store.set_review_lifecycle("crashed-review", {
            "phase": "start_requested", "review_thread_id": "orphan-thread",
        })
        reviewer = FakeClient(shared_threads={
            "orphan-thread": {"id": "orphan-thread", "turns": []},
        })
        self.runtime._new_review_client = lambda **handlers: reviewer
        await self.runtime._reconcile_review_orphans()
        self.assertEqual(len(self.runtime._review_orphans), 1)
        self.assertEqual(len(self.store.pending_review_cleanups()), 1)
        reviewer.threads["orphan-thread"]["turns"] = [
            {"id": "finished-review", "status": "interrupted"},
        ]
        await self.runtime._reconcile_review_orphans()
        self.assertFalse(self.runtime._review_orphans)
        self.assertFalse(self.store.pending_review_cleanups())
        self.assertFalse(any(method in {"review/start", "turn/start", "thread/start"}
                             for method, _ in reviewer.calls))

    async def test_source_deletion_preserves_unconfirmed_review_journal(self):
        await self.start()
        self.store.claim_tool_call("conversation-1", "turn-1", "delete-review", "code_review", {})
        self.store.set_review_lifecycle("delete-review", {
            "phase": "start_requested", "review_thread_id": "orphan-thread",
        })
        await self.complete_turn()
        reviewer = FakeClient(shared_threads={
            "orphan-thread": {"id": "orphan-thread", "turns": []},
        })
        self.runtime._new_review_client = lambda **handlers: reviewer
        with self.assertRaisesRegex(AppServerProtocolError, "cancellation is not confirmed"):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertEqual(len(self.store.pending_review_cleanups()), 1)
        self.assertIn("thread-1", self.factory.threads)
        reviewer.threads["orphan-thread"]["turns"] = [{"id": "finished", "status": "interrupted"}]
        await self.runtime.delete_native_thread("conversation-1")
        self.assertFalse(self.store.pending_review_cleanups())
        self.assertNotIn("thread-1", self.factory.threads)

    async def test_cancel_before_review_start_releases_reserved_journal(self):
        await self.start()
        started = asyncio.Event()
        async def preparing(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with mock.patch("engine.conversation_runtime.run_native_review", new=preparing):
            request = asyncio.create_task(self.runtime.handle_server_request(
                "item/tool/call", {"threadId": "thread-1", "turnId": "turn-1",
                    "callId": "unlaunched-review", "tool": "code_review", "arguments": {
                        "instructions": "review", "scope": {"type": "uncommittedChanges"}}},
            ))
            await asyncio.wait_for(started.wait(), timeout=5)
            self.assertEqual(self.store.pending_review_cleanups()[0]["phase"], "reserved")
            await self.complete_turn()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertFalse(self.store.pending_review_cleanups())
        await self.runtime.delete_native_thread("conversation-1")

    async def test_delete_releases_retired_safe_reservation_without_runtime_restart(self):
        await self.start()
        self.store.claim_tool_call("conversation-1", "turn-1", "retired-reservation", "code_review", {})
        self.store.set_review_lifecycle("retired-reservation", {"phase": "reserved"})
        await self.complete_turn()
        self.runtime._review_orphans_loaded = True
        await self.runtime.delete_native_thread("conversation-1")
        self.assertFalse(self.store.pending_review_cleanups())
        self.assertNotIn("thread-1", self.factory.threads)

    async def test_code_review_allows_independent_parallel_calls(self):
        await self.start()
        for number in (2, 3):
            self.store.create_conversation(
                f"conversation-{number}", title="probe", cwd=str(self.workspace),
                settings={"model": "fixture", "effort": "medium", "permission": "read-only", "service_tier": None},
            )
            await self.runtime.start_conversation(
                f"conversation-{number}", command_id=f"message-{number}", text="probe",
            )
        release = asyncio.Event()
        invoked = []
        result = {"official_channel": "app_server_native_review", "review_thread_id": "r", "review_turn_id": "t", "configured_model": "fixture-model", "configured_reasoning_effort": "high", "scope": {"type": "uncommittedChanges"}, "request_target": {"type": "uncommittedChanges"}, "before": {}, "after": {}, "scope_changed": None, "scope_verification": {"status": "unverified", "error": "fixture"}, "result_text": "findings"}
        async def blocked(*args, **kwargs):
            invoked.append(kwargs["reasoning_effort"])
            await release.wait()
            return result
        async def request(number, call_id):
            conversation = self.store.get_conversation(f"conversation-{number}")
            return await self.runtime.handle_server_request(
                "item/tool/call", {"threadId": conversation["thread_id"], "turnId": conversation["active_turn_id"], "callId": call_id, "tool": "code_review", "arguments": {"instructions": "review", "scope": {"type": "uncommittedChanges"}}},
            )
        with mock.patch("engine.conversation_runtime.run_native_review", new=blocked):
            tasks = [
                asyncio.create_task(request(1, "review-first")),
                asyncio.create_task(request(1, "review-second")),
                asyncio.create_task(request(2, "review-third")),
                asyncio.create_task(request(3, "review-fourth")),
            ]
            async with asyncio.timeout(5):
                while len(invoked) != 4:
                    await asyncio.sleep(0)
            self.assertEqual(invoked, ["high"] * 4)
            release.set()
            completed = await asyncio.gather(*tasks)
        self.assertTrue(all(value["success"] for value in completed))
        self.assertEqual(self.runtime._review_tasks, {})

    async def test_code_review_registers_before_preparation_and_terminal_cancels(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime._ensure_client

        async def delayed_client():
            entered.set()
            await release.wait()
            return await original()
        self.runtime._ensure_client = delayed_client
        pending = asyncio.create_task(self.runtime.handle_server_request(
            "item/tool/call", {"threadId": "thread-1", "turnId": "turn-1", "callId": "review-race",
            "tool": "code_review", "arguments": {"instructions": "review", "scope": {"type": "uncommittedChanges"}}},
        ))
        await entered.wait()
        self.assertIn(("thread-1", "turn-1", "review-race"), self.runtime._review_tasks)
        await self.complete_turn()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(any(method == "review/start" for client in self.factory.clients for method, _ in client.calls))

    async def test_code_review_failure_receipt_reports_reason_model_and_cleanup_state(self):
        await self.start()
        with mock.patch(
            "engine.conversation_runtime.run_native_review",
            new=mock.AsyncMock(side_effect=AppServerProtocolError("high is unavailable")),
        ):
            response = await self.runtime.handle_server_request(
                "item/tool/call", {"threadId": "thread-1", "turnId": "turn-1", "callId": "review-failure", "tool": "code_review", "arguments": {"instructions": "review", "scope": {"type": "uncommittedChanges"}}},
            )
        self.assertFalse(response["success"])
        failure = response["review_failure"]
        self.assertEqual(failure["reason"], "review_protocol_error")
        self.assertEqual(failure["configured_model"], "fixture-model")
        self.assertEqual(failure["configured_reasoning_effort"], "high")
        self.assertEqual(failure["cancellation"], "not_required")
        self.assertIsNone(failure["review_thread_id"])
        self.assertIsNone(failure["review_turn_id"])

    async def test_code_review_duplicate_inprogress_never_restarts(self):
        await self.start()
        args = {"instructions": "review", "scope": {"type": "uncommittedChanges"}}
        self.store.claim_tool_call("conversation-1", "turn-1", "review-duplicate", "code_review", args)
        with self.assertRaises(AppServerRPCError):
            await self.runtime.handle_server_request(
                "item/tool/call", {"threadId": "thread-1", "turnId": "turn-1", "callId": "review-duplicate", "tool": "code_review", "arguments": args},
            )
        self.assertFalse(any(method == "review/start" for method, _ in self.factory.clients[0].calls))

    async def test_code_review_large_result_is_delivered_in_ordered_full_chunks(self):
        await self.start()
        large = "finding-" * 13_000
        from engine import conversation_runtime as runtime_module
        value = {"official_channel": "app_server_native_review", "review_thread_id": "r", "review_turn_id": "t", "configured_model": "fixture-model", "configured_reasoning_effort": "high", "scope": {"type": "uncommittedChanges"}, "request_target": {"type": "uncommittedChanges"}, "before": {}, "after": {}, "scope_changed": None, "scope_verification": {"status": "unverified", "error": "fixture"}, "result_text": large}
        with mock.patch.object(runtime_module, "run_native_review", new=mock.AsyncMock(return_value=value)):
            response = await self.runtime.handle_server_request(
                "item/tool/call", {"threadId": "thread-1", "turnId": "turn-1", "callId": "review-large", "tool": "code_review", "arguments": {"instructions": "review", "scope": {"type": "uncommittedChanges"}}},
            )
        self.assertTrue(response["success"])
        self.assertGreater(len(response["contentItems"]), 1)
        self.assertIn(large, "".join(item["text"] for item in response["contentItems"]))

    async def test_native_request_user_input_waits_then_returns_wire_answers(self):
        await self.start()
        pending = asyncio.create_task(self.runtime.handle_server_request(
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "ask-1",
                "isBlocking": False, "autoResolutionMs": None,
                "questions": [{
                    "id": "why", "header": "Why", "question": "Why?",
                    "options": None, "isOther": False, "isSecret": False,
                }],
            }, request_id=0, connection_id="test-instance:1",
        ))
        await asyncio.sleep(0)
        question = self.store.snapshot("conversation-1")["questions"][0]
        self.assertEqual(question["deadline_source"], "host_optional_default")
        await self.runtime.answer_question(
            "conversation-1", question_id=question["id"], command_id="answer-1",
            answers={"why": "because"},
        )
        response = await pending
        self.assertEqual(response.result, {"answers": {"why": {"answers": ["because"]}}})
        await response.on_sent()
        await self.runtime.handle_notification({
            "method": "serverRequest/resolved",
            "params": {"threadId": "thread-1", "requestId": 0},
        }, connection_id="test-instance:1")
        self.assertEqual(self.store.question(question["id"])["state"], "awaiting_activity")
        await self.runtime.handle_notification({
            "method": "item/started",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "id": "reason-1", "type": "reasoning", "summary": [],
            }},
        })
        self.assertEqual(self.store.question(question["id"])["state"], "answered")

    async def test_async_agent_question_deadline_sends_recommendation_as_system(self):
        await self.start()
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {
                    "id": "async-ask", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async",
                    "text": "请选择", "questions": [{
                        "title": "采用哪个方案？", "options": ["方案 A", "方案 B"],
                    }],
                },
            },
        })
        request = self.store.snapshot("conversation-1")["async_questions"][0]
        self.assertEqual(request["recommended_answer"], "方案 A")
        self.assertEqual(request["state"], "awaiting_answer")
        self.store._db.execute(
            "UPDATE async_question_requests SET deadline_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", request["id"]),
        )
        self.runtime._restore_async_question_deadlines("conversation-1")
        for _ in range(50):
            if self.store.async_question_request(request["id"])["state"] == "answered":
                break
            await asyncio.sleep(0.01)
        saved = self.store.async_question_request(request["id"])
        self.assertEqual(saved["state"], "answered")
        self.assertEqual(saved["answer_source"], "deadline")
        command = self.store.command(saved["command_id"])
        self.assertEqual(command["text"], "方案 A")
        self.assertEqual(command["origin"], "system")
        self.assertEqual(command["state"], "accepted")

    async def test_async_agent_question_manual_answer_uses_one_durable_command(self):
        await self.start()
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {
                    "id": "async-manual", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async",
                    "text": "请选择", "questions": [{
                        "title": "采用哪个方案？", "options": ["方案 A", "方案 B"],
                    }],
                },
            },
        })
        request = self.store.snapshot("conversation-1")["async_questions"][0]
        await self.runtime.answer_async_question(
            "conversation-1", request_id=request["id"],
            command_id="async-manual-answer", answer_text="方案 B",
        )
        saved = self.store.async_question_request(request["id"])
        self.assertEqual(saved["state"], "answered")
        self.assertEqual(saved["answer_source"], "user")
        command = self.store.command("async-manual-answer")
        self.assertEqual(command["text"], "方案 B")
        self.assertEqual(command["origin"], "user")
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_async_question(
                "conversation-1", request_id=request["id"],
                command_id="async-second-answer", answer_text="方案 A",
            )

    async def test_async_agent_question_deadline_survives_runtime_restart(self):
        await self.start()
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {
                    "id": "async-restart", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async",
                    "text": "请选择", "questions": [{
                        "title": "采用哪个方案？", "options": ["方案 A", "方案 B"],
                    }],
                },
            },
        })
        before = self.store.snapshot("conversation-1")["async_questions"][0]
        deadline = "2030-01-01T00:00:00+00:00"
        self.store._db.execute(
            "UPDATE async_question_requests SET deadline_at = ? WHERE id = ?",
            (deadline, before["id"]),
        )
        await self.runtime.close()
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory)
        await self.runtime.startup_recover()
        restored = self.store.async_question_request(before["id"])
        self.assertEqual(restored["state"], "awaiting_answer")
        self.assertEqual(restored["deadline_at"], deadline)
        self.assertIn(before["id"], self.runtime._async_question_deadlines)

    async def test_notification_path_counts_only_button_compaction_once(self):
        await self.start()
        await self.complete_turn()
        await self.runtime.compact("conversation-1", command_id="compact-button")
        await self.runtime.handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "compact-turn", "status": "inProgress", "items": []},
            },
        })
        started = {
            "method": "item/started",
            "params": {
                "threadId": "thread-1", "turnId": "compact-turn",
                "item": {"id": "compaction-1", "type": "contextCompaction"},
            },
        }
        completed = {**started, "method": "item/completed"}
        await self.runtime.handle_notification(started)
        await self.runtime.handle_notification(completed)
        await self.runtime.handle_notification(completed)
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_completed_count"],
            1,
        )

    async def test_safe_context_switch_observation_updates_runtime_snapshot(self):
        await self.start()
        await self.runtime.handle_notification({
            "method": "internal/context-switch/observed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "callKeySha256": "c" * 64, "state": "call_seen",
                "serverVersion": "fixture",
            },
        })
        await asyncio.sleep(0)
        snapshot = self.store.snapshot("conversation-1")
        self.assertEqual(
            snapshot["conversation"]["context_switches"][0]["state"], "call_seen"
        )
        self.assertTrue(any(
            conversation_id == "conversation-1" for conversation_id, _snapshot
            in self.statuses
        ))

    async def test_mobile_project_first_turn_gets_context_without_polluting_user_text(self):
        project_cwd = self.root / "mobile-project"
        project_cwd.mkdir()
        self.store.create_conversation(
            "mobile-project",
            title="mobile project",
            cwd=str(project_cwd),
            project_id="project-1",
            client_origin="mobile_web",
            settings={
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": None,
            },
        )
        result = await self.runtime.start_conversation(
            "mobile-project", command_id="mobile-project-message", text="刚上传截图"
        )
        client = self.factory.clients[0]
        starts = [params for method, params in client.calls if method == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("手机上传资料", starts[0]["leading_context"])
        self.assertIn("固定目录", starts[0]["leading_context"])
        self.assertIn("兼容旧上传", starts[0]["leading_context"])
        self.assertIn("直接 sudo 失败", starts[0]["leading_context"])
        self.assertIn("受控维护或发布入口", starts[0]["leading_context"])
        self.assertIn("不绕过安全限制", starts[0]["leading_context"])
        self.assertIn("publish_artifact", starts[0]["leading_context"])
        self.assertIn("success=true", starts[0]["leading_context"])
        self.assertIn("不要为交付扫描无关目录", starts[0]["leading_context"])
        self.assertNotIn("run_code_review.py", starts[0]["leading_context"])
        self.assertNotIn("只有调用 code_review", starts[0]["leading_context"])
        self.assertEqual(starts[0]["text"], "刚上传截图")
        self.assertEqual(
            self.store.command("mobile-project-message")["text"], "刚上传截图"
        )
        self.assertEqual(result["snapshot"]["commands"][0]["text"], "刚上传截图")

        await self.complete_turn()
        await self.runtime.send_message(
            "mobile-project",
            command_id="mobile-project-followup",
            text="继续",
            force_new_turn=True,
        )
        starts = [params for method, params in client.calls if method == "turn/start"]
        self.assertEqual(len(starts), 2)

    async def test_mobile_temp_first_turn_gets_private_directory_context(self):
        temp_cwd = self.root / "mobile-temp"
        temp_cwd.mkdir()
        self.store.create_conversation(
            "mobile-temp",
            title="mobile temp",
            cwd=str(temp_cwd),
            client_origin="mobile_web",
            settings={
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": None,
            },
        )
        await self.runtime.start_conversation(
            "mobile-temp", command_id="mobile-temp-message", text="刚上传图片"
        )
        client = self.factory.clients[0]
        turn = [params for method, params in client.calls if method == "turn/start"][-1]
        self.assertIn("专用的文件接收目录", turn["leading_context"])
        self.assertNotIn("手机上传资料", turn["leading_context"])
        self.assertNotIn("直接 sudo 失败", turn["leading_context"])
        self.assertIn("publish_artifact", turn["leading_context"])
        self.assertEqual(turn["text"], "刚上传图片")

    async def test_initial_native_title_uses_current_web_title_once(self):
        self.enable_native_initial_title("Generated Web title")
        await self.start()
        client = self.factory.clients[0]
        self.assertEqual(self.native_name_calls(client), [{
            "thread_id": "thread-1", "name": "Generated Web title",
        }])
        first_turn = next(params for method, params in client.calls if method == "turn/start")
        self.assertTrue(first_turn["leading_context_after_text"])
        self.assertIn("这是手机发起的临时对话", first_turn["leading_context"])

        # Duplicate command, reconciliation after a desktop rename, and later
        # turns never enter fresh Thread naming.
        await self.runtime.start_conversation(
            "conversation-1", command_id="message-1", text="first"
        )
        await self.complete_turn()
        client.threads["thread-1"]["name"] = "desktop renamed title"
        client.connected = False
        self.runtime.handle_disconnect(client.connection_epoch, "reader_eof", "EOF")
        await self.runtime._disconnect_recovery
        await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="later",
            force_new_turn=True,
        )
        self.assertEqual(sum(
            len(self.native_name_calls(value)) for value in self.factory.clients
        ), 1)
        later_start = [
            params for value in self.factory.clients
            for method, params in value.calls if method == "turn/start"
        ][-1]
        self.assertIsNone(later_start["leading_context_after_text"])

    async def test_initial_native_title_uses_manual_web_title(self):
        self.enable_native_initial_title("桌面手动标题")
        await self.start()
        self.assertEqual(
            self.native_name_calls(self.factory.clients[0])[0]["name"],
            "桌面手动标题",
        )

    async def test_initial_native_title_skips_old_drafts_and_bound_records(self):
        # The setup record has a legacy settings object, so it cannot be
        # retroactively named when it first starts after this release.
        raw = self.store._db.execute(
            "SELECT settings_json FROM conversations WHERE id=?", ("conversation-1",)
        ).fetchone()[0]
        self.assertNotIn("_native_initial_title", raw)
        await self.start()
        client = self.factory.clients[0]
        self.assertEqual(self.native_name_calls(client), [])

        self.store.create_conversation(
            "old-bound", title="Old bound", cwd=str(self.workspace),
            settings={
                "model": "fixture", "effort": "medium",
                "permission": "danger-full-access", "service_tier": None,
                "_native_initial_title": True,
            },
        )
        self.store.bind_thread("old-bound", {"id": "old-thread"}, server_version="fixture")
        result = await self.runtime.start_conversation(
            "old-bound", command_id="old-bound-start", text="do not rename"
        )
        self.assertEqual(result["command"]["state"], "rejected")
        self.assertEqual(self.native_name_calls(client), [])

    async def test_initial_native_title_requires_mobile_origin(self):
        self.store.update_conversation_title("conversation-1", "not mobile")
        self.store.update_settings(
            "conversation-1", {"_native_initial_title": True}
        )
        await self.start()
        client = self.factory.clients[0]
        self.assertEqual(self.native_name_calls(client), [])
        turn = next(params for method, params in client.calls if method == "turn/start")
        self.assertIsNone(turn["leading_context_after_text"])

    async def test_initial_native_title_disconnect_rejects_before_turn_and_uses_global_recovery(self):
        self.enable_native_initial_title()
        client = await self.runtime._ensure_client()
        client.thread_name_error = AppServerDisconnected("name transport lost")
        result = await self.runtime.start_conversation(
            "conversation-1", command_id="message-1", text="first",
            skill_selectors=["fixture"],
        )
        self.assertEqual(result["command"]["state"], "rejected")
        self.assertEqual(
            result["command"]["reason"], "native_initial_title_connection_lost"
        )
        self.assertNotEqual(result["command"]["reason"], "turn_delivery_unknown")
        self.assertEqual(len([call for value in self.factory.clients for call in value.calls
                              if call[0] == "thread/start"]), 1)
        self.assertEqual(len([call for value in self.factory.clients for call in value.calls
                              if call[0] == "turn/start"]), 0)
        self.assertEqual(sum(
            len(self.native_name_calls(value)) for value in self.factory.clients
        ), 1)

        await self.runtime._disconnect_recovery
        self.assertEqual(len(self.factory.clients), 2)
        self.assertEqual(len([call for call in self.factory.clients[1].calls
                              if call[0] == "thread/resume"]), 1)
        self.assertEqual(self.store.command("message-1")["state"], "rejected")
        self.assertNotEqual(
            self.store.command("message-1")["reason"], "turn_delivery_unknown"
        )

    async def test_initial_native_title_rejects_generic_error_after_connection_loss(self):
        self.enable_native_initial_title()
        client = await self.runtime._ensure_client()
        client.thread_name_error = AppServerError("name transport lost")
        client.thread_name_connection_lost = True
        result = await self.start()
        self.assertEqual(result["command"]["state"], "rejected")
        self.assertEqual(
            result["command"]["reason"], "native_initial_title_connection_lost"
        )
        self.assertEqual(len([call for value in self.factory.clients for call in value.calls
                              if call[0] == "turn/start"]), 0)
        await self.runtime._disconnect_recovery
        self.assertEqual(len(self.factory.clients), 2)
        self.assertEqual(self.store.command("message-1")["state"], "rejected")

    async def test_initial_native_title_skips_unsupported_client_and_starts_turn(self):
        self.enable_native_initial_title()
        client = await self.runtime._ensure_client()
        client.supports_thread_name_set = False
        result = await self.start()
        self.assertEqual(self.native_name_calls(client), [])
        self.assertEqual(len([call for call in client.calls if call[0] == "turn/start"]), 1)
        self.assertEqual(result["command"]["state"], "accepted")

    async def test_initial_native_title_preserves_explicit_native_name(self):
        self.enable_native_initial_title()
        client = await self.runtime._ensure_client()
        client.thread_start_name = "desktop-owned title"
        await self.start()
        self.assertEqual(self.native_name_calls(client), [])

    async def test_initial_native_title_rejection_does_not_block_first_turn(self):
        self.enable_native_initial_title()
        client = await self.runtime._ensure_client()
        client.thread_name_error = AppServerRPCError(-32000, "rejected title")
        result = await self.start()
        self.assertEqual(len(self.native_name_calls(client)), 1)
        self.assertEqual(len([call for call in client.calls if call[0] == "turn/start"]), 1)
        self.assertEqual(result["command"]["state"], "accepted")

    async def test_initial_native_title_falls_back_to_message_without_mobile_context(self):
        self.store.update_settings("conversation-1", {"_native_initial_title": True})
        self.store.update_conversation_title("conversation-1", "新对话")
        self.store._db.execute(
            "UPDATE conversations SET client_origin='mobile_web' WHERE id=?", ("conversation-1",)
        )
        text = "  中文\n 标题   含 空白  "
        await self.runtime.start_conversation(
            "conversation-1", command_id="message-1", text=text
        )
        name = self.native_name_calls(self.factory.clients[0])[0]["name"]
        self.assertEqual(name, "中文 标题 含 空白")
        self.assertNotIn("手机发起", name)
        self.assertNotIn("publish_artifact", name)

    async def test_followup_steers_the_exact_active_turn(self):
        await self.start()
        result = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="add this"
        )
        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(result["command"]["result_turn_id"], "turn-1")
        steer = [call for call in self.factory.clients[0].calls if call[0] == "turn/steer"]
        self.assertEqual(len(steer), 1)
        self.assertEqual(steer[0][1]["turn_id"], "turn-1")

    async def test_preflight_active_native_force_new_turn_queues_without_steering(self):
        await self.start()
        # Model a stale local idle projection while thread/read still reports
        # the original native turn as active.
        self.store.set_conversation_state(
            "conversation-1", "idle", active_turn_id=None
        )

        result = await self.runtime.send_message(
            "conversation-1",
            command_id="message-2",
            text="start separately",
            force_new_turn=True,
        )

        self.assertEqual(result["command"]["state"], "queued")
        self.assertEqual(result["command"]["reason"], "next_turn_required")
        self.assertFalse(any(
            method == "turn/steer" for method, _ in self.factory.clients[0].calls
        ))

    async def test_preflight_active_native_pending_permission_queues_without_steering(self):
        await self.start()
        await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-read-only",
            updates={"permission": "read-only"},
        )
        self.store.set_conversation_state(
            "conversation-1", "idle", active_turn_id=None
        )

        result = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="use read-only next"
        )

        self.assertEqual(result["command"]["state"], "queued")
        self.assertEqual(result["command"]["reason"], "next_turn_required")
        self.assertFalse(any(
            method == "turn/steer" for method, _ in self.factory.clients[0].calls
        ))

    async def test_preflight_active_native_ordinary_followup_still_steers(self):
        await self.start()
        self.store.set_conversation_state(
            "conversation-1", "idle", active_turn_id=None
        )

        result = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="continue normally"
        )

        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(result["command"]["result_turn_id"], "turn-1")
        steers = [params for method, params in self.factory.clients[0].calls
                  if method == "turn/steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["client_user_message_id"], "message-2")

    async def test_drain_reentered_force_new_turn_command_does_not_steer_active_native_turn(self):
        await self.start()
        queued = await self.runtime.send_message(
            "conversation-1",
            command_id="message-2",
            text="remain queued",
            force_new_turn=True,
        )
        self.assertEqual(queued["command"]["state"], "queued")
        self.store.set_conversation_state(
            "conversation-1", "idle", active_turn_id=None
        )

        await self.runtime._drain_queue("conversation-1")

        command = self.store.command("message-2")
        self.assertEqual(command["state"], "queued")
        self.assertEqual(command["reason"], "next_turn_required")
        self.assertFalse(any(
            method == "turn/steer" for method, _ in self.factory.clients[0].calls
        ))

    async def test_queued_read_only_message_starts_once_after_old_turn_ends(self):
        await self.start()
        await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-read-only",
            updates={"permission": "read-only"},
        )
        queued = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="run read-only"
        )
        self.assertEqual(queued["command"]["state"], "queued")
        await self.complete_turn()
        for _ in range(100):
            if self.store.command("message-2")["state"] == "accepted":
                break
            await asyncio.sleep(0.01)

        starts = [params for method, params in self.factory.clients[0].calls
                  if method == "turn/start" and params["client_user_message_id"] == "message-2"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["options"]["sandbox"], "read-only")
        self.assertEqual(self.store.command("message-2")["result_turn_id"], "turn-2")
        self.assertFalse(any(
            method == "turn/steer" and params["client_user_message_id"] == "message-2"
            for method, params in self.factory.clients[0].calls
        ))

    async def test_nonsteerable_message_queues_then_starts_next_native_turn(self):
        await self.start()
        self.factory.clients[0].steer_error = AppServerRPCError(
            -32602, "activeTurnNotSteerable"
        )
        queued = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="next"
        )
        self.assertEqual(queued["command"]["state"], "queued")
        self.factory.clients[0].steer_error = None
        await self.complete_turn()
        for _ in range(20):
            if self.store.command("message-2")["state"] == "accepted":
                break
            await asyncio.sleep(0)
        snapshot = self.store.snapshot("conversation-1")
        self.assertEqual(self.store.command("message-2")["result_turn_id"], "turn-2")
        self.assertEqual([turn["id"] for turn in snapshot["turns"]], ["turn-1", "turn-2"])

    async def test_queue_skips_rejected_head_and_starts_next(self):
        await self.start()
        for command_id in ("message-2", "message-3"):
            queued = await self.runtime.send_message(
                "conversation-1",
                command_id=command_id,
                text=command_id,
                force_new_turn=True,
            )
            self.assertEqual(queued["command"]["state"], "queued")
        client = self.factory.clients[0]
        original_start_turn = client.start_turn
        attempted = []

        async def reject_second(*args, **kwargs):
            attempted.append(kwargs.get("client_user_message_id"))
            if kwargs.get("client_user_message_id") == "message-2":
                raise AppServerRPCError(-32602, "fixture rejection")
            return await original_start_turn(*args, **kwargs)

        client.start_turn = reject_second
        await self.complete_turn()
        for _ in range(100):
            if self.store.command("message-3")["state"] == "accepted":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(self.store.command("message-2")["state"], "rejected")
        self.assertEqual(self.store.command("message-3")["state"], "accepted")
        self.assertEqual(self.store.command("message-3")["result_turn_id"], "turn-2")
        self.assertEqual(attempted, ["message-2", "message-3"])

    async def test_queue_stops_after_preflight_failure_needing_reconcile(self):
        await self.start()
        for command_id in ("message-2", "message-3"):
            await self.runtime.send_message(
                "conversation-1",
                command_id=command_id,
                text=command_id,
                force_new_turn=True,
            )
        client = self.factory.clients[0]

        async def fail_read(*_args, **_kwargs):
            raise AppServerDisconnected("fixture disconnect")

        client.read_thread = fail_read
        await self.complete_turn()
        for _ in range(100):
            if self.store.command("message-2")["state"] == "rejected":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(
            self.store.get_conversation("conversation-1")["state"], "needs_user"
        )
        self.assertEqual(self.store.command("message-2")["state"], "rejected")
        self.assertEqual(self.store.command("message-3")["state"], "queued")

    async def test_queue_stops_after_unknown_delivery(self):
        await self.start()
        for command_id in ("message-2", "message-3"):
            await self.runtime.send_message(
                "conversation-1",
                command_id=command_id,
                text=command_id,
                force_new_turn=True,
            )
        client = self.factory.clients[0]
        client.start_turn_error = AppServerDisconnected("fixture disconnect")
        await self.complete_turn()
        for _ in range(100):
            if self.store.command("message-2")["state"] == "delivery_unknown":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(
            self.store.get_conversation("conversation-1")["state"], "needs_user"
        )
        self.assertEqual(
            self.store.command("message-2")["state"], "delivery_unknown"
        )
        self.assertEqual(self.store.command("message-3")["state"], "queued")

    async def _join_projection_recovery(self):
        tasks = [task for task in self.runtime._background
                 if task.get_name() == "conversation-projection-recovery"]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        # Settlement and queue dispatch use the existing independent workers.
        for _ in range(5):
            await asyncio.sleep(0)

    async def _fail_terminal_projection(self, *, connection_id=None):
        apply = self.store.apply_notification

        def fail(message):
            if message.get("method") == "turn/completed":
                raise AppServerProtocolError("synthetic projection failure")
            return apply(message)

        native = self.factory.threads["thread-1"]["turns"][0]
        native.update(status="completed", items=[{
            "id": "answer-1", "type": "agentMessage",
            "phase": "final_answer", "text": "authoritative final",
        }])
        self.factory.threads["thread-1"]["status"] = {"type": "idle"}
        with mock.patch.object(self.store, "apply_notification", side_effect=fail):
            with self.assertRaises(AppServerProtocolError):
                await self.runtime.handle_notification({
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": copy.deepcopy(native)},
                }, connection_id=connection_id)

    async def test_terminal_projection_failure_reconciles_once_without_replay(self):
        await self.start()
        await self.runtime.send_message("conversation-1", command_id="message-2",
                                        text="next", force_new_turn=True)
        client = self.factory.clients[0]
        await self._fail_terminal_projection()
        await self._join_projection_recovery()

        self.assertEqual(self.store.command("message-1")["state"], "completed")
        self.assertEqual(self.store.command("message-2")["result_turn_id"], "turn-2")
        self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        self.assertEqual(len(self.factory.clients), 1)
        self.assertTrue(client.connected)
        starts = [params["client_user_message_id"] for method, params in client.calls
                  if method == "turn/start"]
        self.assertEqual(starts, ["message-1", "message-2"])
        # One recovery read plus the existing next-command preflight read.
        self.assertEqual(sum(method == "thread/read" for method, _ in client.calls), 2)
        await self.runtime.reconcile("conversation-1")
        await self._join_projection_recovery()
        self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        self.assertEqual(sum(method == "turn/start" for method, _ in client.calls), 2)

    async def test_projection_failure_burst_shares_one_recovery(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        reconcile = self.runtime._reconcile_locked

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return await reconcile(*args, **kwargs)

        with mock.patch.object(self.runtime, "_reconcile_locked", side_effect=blocked) as recover:
            await self._fail_terminal_projection()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self._fail_terminal_projection()
            release.set()
            await self._join_projection_recovery()
            self.assertEqual(recover.await_count, 1)
        self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "idle")
        self.assertEqual(sum(m == "thread/read" for m, _ in self.factory.clients[0].calls), 1)

    async def test_projection_recovery_failure_is_bounded_and_manual_retry_works(self):
        await self.start()
        client = self.factory.clients[0]
        with (mock.patch.object(client, "read_thread", side_effect=RuntimeError("unavailable")) as read,
              self.assertLogs("engine.conversation_runtime", level="ERROR")):
            await self._fail_terminal_projection()
            with self.assertRaises(RuntimeError):
                await self._join_projection_recovery()
            self.assertEqual(read.await_count, 1)
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "needs_user")
        self.assertEqual(self.terminals, [])
        await self.runtime.reconcile("conversation-1")
        await self._join_projection_recovery()
        self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        self.assertEqual(sum(m == "turn/start" for m, _ in client.calls), 1)

    async def test_projection_recovery_ignores_retired_connection(self):
        await self.start()
        client = self.factory.clients[0]
        client.connection_id = "fixture:1"
        async with self.runtime._conversation_locks["conversation-1"]:
            await self._fail_terminal_projection(connection_id="fixture:1")
            client.connection_epoch += 1
            client.connection_id = "fixture:2"
        await self._join_projection_recovery()
        self.assertEqual(sum(m == "thread/read" for m, _ in client.calls), 0)
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "active")
        self.assertEqual(self.terminals, [])

    async def test_projection_recovery_does_not_cross_native_delete_gate(self):
        await self.start()
        await self.runtime.send_message("conversation-1", command_id="message-2",
                                        text="queued", force_new_turn=True)
        self.store.set_conversation_state("conversation-1", "needs_user",
            recovery_state="delete_delivery_unknown", native_delete_pending=True)
        await self._fail_terminal_projection()
        await self._join_projection_recovery()
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertEqual(self.store.command("message-2")["state"], "queued")
        self.assertEqual(self.terminals, [])
        self.assertEqual(sum(m == "turn/start" for m, _ in self.factory.clients[0].calls), 1)

    async def test_projection_recovery_never_retries_uncertain_queued_delivery(self):
        await self.start()
        for number in (2, 3):
            await self.runtime.send_message("conversation-1", command_id=f"message-{number}",
                                            text="queued", force_new_turn=True)
        client = self.factory.clients[0]
        client.start_turn_error = AppServerDisconnected("synthetic lost response")
        await self._fail_terminal_projection()
        await self._join_projection_recovery()
        self.assertEqual(self.store.command("message-2")["state"], "delivery_unknown")
        self.assertEqual(self.store.command("message-3")["state"], "queued")
        starts = [p["client_user_message_id"] for m, p in client.calls if m == "turn/start"]
        self.assertEqual(starts, ["message-1", "message-2"])

    async def test_projection_failure_before_turn_response_uses_exact_read(self):
        client = await self.runtime._ensure_client()
        start = client.start_turn
        apply = self.store.apply_notification

        def fail(message):
            if message.get("method") == "turn/completed":
                raise AppServerProtocolError("synthetic projection failure")
            return apply(message)

        async def early_terminal(*args, **kwargs):
            turn = await start(*args, **kwargs)
            native = self.factory.threads["thread-1"]["turns"][0]
            # Keep the authoritative UserMessage identity. The real client's
            # independent notification worker logs a handler failure without
            # failing an otherwise successful turn/start response.
            native["status"] = "completed"
            self.factory.threads["thread-1"]["status"] = {"type": "idle"}
            with self.assertRaises(AppServerProtocolError):
                await self.runtime.handle_notification({"method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": copy.deepcopy(native)}})
            return turn

        with (mock.patch.object(self.store, "apply_notification", side_effect=fail),
              mock.patch.object(client, "start_turn", side_effect=early_terminal)):
            await self.start()
        await self._join_projection_recovery()
        self.assertEqual(self.store.command("message-1")["state"], "completed")
        self.assertEqual(self.store.command("message-1")["result_turn_id"], "turn-1")
        self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        self.assertEqual(sum(m == "turn/start" for m, _ in client.calls), 1)

    async def test_close_cancels_projection_recovery(self):
        await self.start()
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(self.factory.clients[0], "read_thread", side_effect=blocked):
            await self._fail_terminal_projection()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self.runtime.close()
        self.assertFalse(self.runtime._background)
        self.assertFalse(self.runtime._projection_recoveries)
        self.assertEqual(self.terminals, [])

    async def test_unowned_projection_failure_does_not_open_a_connection(self):
        with mock.patch.object(self.store, "apply_notification", side_effect=AppServerProtocolError("fault")):
            with self.assertRaises(AppServerProtocolError):
                await self.runtime.handle_notification({
                    "method": "turn/completed", "params": {"threadId": "unowned",
                    "turn": {"id": "turn-foreign", "status": "completed"}},
                })
        await self._join_projection_recovery()
        self.assertEqual(self.factory.clients, [])
        self.assertEqual(self.terminals, [])

    async def test_foreign_or_malformed_projection_failures_do_not_reconcile(self):
        await self.start()
        client = self.factory.clients[0]
        client.connection_id = "fixture:current"
        packets = [
            ({"method": "turn/completed", "params": {"threadId": "foreign",
                "turn": {"id": "turn-foreign", "status": "completed"}}}, "fixture:current"),
            ({"method": "turn/completed", "params": {"threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"}}}, "fixture:retired"),
            ({"method": "turn/completed", "params": {"threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "unknown"}}}, "fixture:current"),
            ({"method": "item/agentMessage/delta", "params": {"threadId": "thread-1",
                "turnId": "turn-1", "itemId": "i", "delta": "text"}}, "fixture:current"),
        ]
        with mock.patch.object(self.store, "apply_notification", side_effect=AppServerProtocolError("fault")):
            for packet, connection in packets:
                with self.subTest(packet=packet["method"], connection=connection):
                    with self.assertRaises(AppServerProtocolError):
                        await self.runtime.handle_notification(packet, connection_id=connection)
        await self._join_projection_recovery()
        self.assertEqual(sum(m == "thread/read" for m, _ in client.calls), 0)
        self.assertEqual(len(self.factory.clients), 1)
        self.assertEqual(self.terminals, [])

    async def test_terminal_handler_does_not_block_next_queued_turn(self):
        await self.start()
        queued = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="next", force_new_turn=True
        )
        self.assertEqual(queued["command"]["state"], "queued")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_terminal(*_args):
            entered.set()
            await release.wait()

        self.runtime._terminal_handler = slow_terminal
        await self.complete_turn()
        for _ in range(100):
            if self.store.command("message-2")["state"] == "accepted":
                break
            await asyncio.sleep(0.01)

        self.assertTrue(entered.is_set())
        self.assertEqual(self.store.command("message-2")["state"], "accepted")
        release.set()

    async def test_reconcile_baseline_failure_finishes_in_needs_user(self):
        await self.start()
        with mock.patch.object(self.runtime, '_repair_delta_baselines',
                               side_effect=AppServerProtocolError('repair failed')):
            result = await self.runtime.reconcile('conversation-1')
        self.assertEqual(result['conversation']['state'], 'needs_user')
        self.assertEqual(result['conversation']['recovery_state'], 'reconcile_failed')

    async def test_preflight_baseline_failure_rejects_without_sending(self):
        await self.start()
        self.store.begin_command('conversation-1', 'message-2', kind='start', text='second')
        client = self.factory.clients[0]
        before = len(client.calls)
        with mock.patch.object(self.runtime, '_repair_delta_baselines',
                               side_effect=AppServerProtocolError('repair failed')):
            result = await self.runtime._preflight_start_locked(
                'conversation-1', 'message-2', 'second', skill_selectors=None,
            )
        self.assertEqual(result['command']['state'], 'rejected')
        self.assertEqual(result['snapshot']['conversation']['state'], 'needs_user')
        self.assertFalse(any(method in {'turn/start', 'turn/steer'}
                             for method, _ in client.calls[before:]))

    async def test_reconcile_preserves_completed_item_with_unchanged_turn_status(self):
        await self.start()
        client = self.factory.clients[0]
        self.factory.threads['thread-1']['turns'][0]['items'].append({
            'id': 'answer-same-status', 'type': 'agentMessage', 'text': 'stale',
        })
        original_read = client.read_thread

        async def read_with_live_item(thread_id, include_turns=True):
            stale = await original_read(thread_id, include_turns=include_turns)
            await self.runtime.handle_notification({
                'method': 'item/completed',
                'params': {'threadId': thread_id, 'turnId': 'turn-1', 'item': {
                    'id': 'answer-same-status', 'type': 'agentMessage',
                    'text': 'latest live text',
                }},
            })
            return stale

        client.read_thread = read_with_live_item
        value = await self.runtime.reconcile('conversation-1')
        self.assertEqual(value['conversation']['state'], 'active')
        self.assertEqual(value['turns'][0]['status'], 'inProgress')
        item = next(i for i in value['turns'][0]['items']
                    if i['native_item_id'] == 'answer-same-status')
        self.assertEqual(item['text'], 'latest live text')
        self.assertEqual(item['status'], 'completed')
        self.assertEqual(sum(method == 'turn/start' for method, _ in client.calls), 1)
        self.assertNotIn('turn/steer', [method for method, _ in client.calls])

    async def test_reconcile_repairs_delta_only_history_item_from_single_item_read(self):
        await self.start()
        client = self.factory.clients[0]
        native_turn = self.factory.threads["thread-1"]["turns"][0]
        native_turn["items"].append({
            "id": "answer-1", "type": "agentMessage",
            "text": "prefix repeated", "phase": "final_answer",
        })
        original_read = client.read_thread

        async def read_with_delta_only_item(thread_id, include_turns=True):
            stale_snapshot = await original_read(thread_id, include_turns=include_turns)
            await self.runtime.handle_notification({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id, "turnId": "turn-1",
                    "itemId": "answer-1", "delta": "repeated",
                },
            })
            await self.runtime.handle_notification({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {
                    "id": "turn-1", "status": "completed", "items": [],
                }},
            })
            return stale_snapshot

        client.read_thread = read_with_delta_only_item
        value = await self.runtime.reconcile("conversation-1", origin="test")

        answer = next(item for item in value["turns"][0]["items"]
                      if item["native_item_id"] == "answer-1")
        self.assertEqual(answer["text"], "prefix repeated")
        self.assertEqual(answer["phase"], "final_answer")
        self.assertEqual(value["turns"][0]["final_text"], "prefix repeated")
        reads = [params for method, params in client.calls if method == "thread/items/list"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["predecessor_id"], "user-1")
        self.assertGreaterEqual(
            len([1 for method, _params in client.calls if method == "notifications/flush"]),
            2,
        )

    async def test_repaired_baseline_keeps_later_deltas_without_repairing_again(self):
        await self.start()
        client = self.factory.clients[0]
        turn = self.factory.threads['thread-1']['turns'][0]
        turn['items'].extend([
            {'id': 'answer-one', 'type': 'agentMessage', 'text': 'prefix-one x'},
            {'id': 'answer-two', 'type': 'agentMessage', 'text': 'prefix-two y'},
        ])
        checkpoint = self.store.capture_reconcile_checkpoint('conversation-1')
        snapshot = await client.read_thread('thread-1')
        for item_id, delta in [('answer-one', ' x'), ('answer-two', ' y')]:
            self.store.apply_notification({
                'method': 'item/agentMessage/delta',
                'params': {'threadId': 'thread-1', 'turnId': 'turn-1',
                           'itemId': item_id, 'delta': delta},
            })
        original = client.read_thread_item

        async def read_item(thread_id, turn_id, item_id, *, predecessor_id=None):
            if item_id == 'answer-two':
                self.store.apply_notification({
                    'method': 'item/agentMessage/delta',
                    'params': {'threadId': thread_id, 'turnId': turn_id,
                               'itemId': 'answer-one', 'delta': ' later'},
                })
            return await original(thread_id, turn_id, item_id, predecessor_id=predecessor_id)

        client.read_thread_item = read_item
        await self.runtime._repair_delta_baselines('conversation-1', client, snapshot, checkpoint)
        value = self.store.reconcile_thread('conversation-1', snapshot, checkpoint=checkpoint)
        answer = next(i for i in value['turns'][0]['items'] if i['native_item_id'] == 'answer-one')
        self.assertEqual(answer['text'], 'prefix-one x later')
        self.assertEqual(sum(method == 'thread/items/list' for method, _ in client.calls), 2)

    async def test_delta_baseline_repair_fails_closed_after_continued_changes(self):
        await self.start()
        client = self.factory.clients[0]
        native_turn = self.factory.threads["thread-1"]["turns"][0]
        native_turn["items"].append({
            "id": "answer-1", "type": "agentMessage", "text": "full baseline",
        })
        snapshot = await client.read_thread("thread-1", include_turns=True)
        checkpoint = self.store.capture_reconcile_checkpoint("conversation-1")
        self.store.apply_notification({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "answer-1", "delta": "suffix",
            },
        })
        original_item_read = client.read_thread_item
        reads = 0

        async def changing_item_read(*args, **kwargs):
            nonlocal reads
            reads += 1
            self.store.apply_notification({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1", "turnId": "turn-1",
                    "itemId": "answer-1", "delta": "suffix",
                },
            })
            return await original_item_read(*args, **kwargs)

        client.read_thread_item = changing_item_read
        with self.assertRaisesRegex(AppServerProtocolError, "delta baseline changed"):
            await self.runtime._repair_delta_baselines(
                "conversation-1", client, snapshot, checkpoint
            )
        self.assertEqual(reads, 3)

    async def test_reconcile_checkpoint_preserves_notification_during_history_read(self):
        await self.start()
        client = self.factory.clients[0]
        native_turn = self.factory.threads["thread-1"]["turns"][0]
        native_turn["items"].append({
            "id": "answer-1", "type": "agentMessage", "text": "old page one",
        })
        original_read = client.read_thread

        async def read_with_live_second_page(thread_id, include_turns=True):
            stale_snapshot = await original_read(thread_id, include_turns=include_turns)
            await self.runtime.handle_notification({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id, "turnId": "turn-1",
                    "item": {
                        "id": "answer-1", "type": "agentMessage",
                        "text": "new page two", "phase": "final_answer",
                    },
                },
            })
            await self.runtime.handle_notification({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {
                    "id": "turn-1", "status": "completed", "items": [],
                }},
            })
            await self.runtime.handle_notification({
                "method": "thread/status/changed",
                "params": {"threadId": thread_id, "status": {"type": "idle"}},
            })
            return stale_snapshot

        client.read_thread = read_with_live_second_page
        value = await self.runtime.reconcile("conversation-1", origin="test")

        self.assertEqual(value["conversation"]["state"], "idle")
        self.assertEqual(value["conversation"]["thread_status"], {"type": "idle"})
        self.assertEqual(value["turns"][0]["status"], "completed")
        answer = next(item for item in value["turns"][0]["items"]
                      if item["native_item_id"] == "answer-1")
        self.assertEqual(answer["text"], "new page two")
        methods = [method for candidate in self.factory.clients
                   for method, _params in candidate.calls]
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/steer", methods)

    async def test_unknown_native_turn_status_does_not_drain_or_start(self):
        await self.start()
        queued = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="next", force_new_turn=True
        )
        self.assertEqual(queued["command"]["state"], "queued")
        native = self.factory.threads["thread-1"]["turns"][0]
        native["status"] = "futureStatus"
        await self.runtime.reconcile("conversation-1")

        self.assertEqual(
            self.store.get_conversation("conversation-1")["state"], "needs_user"
        )
        self.assertEqual(self.store.command("message-2")["state"], "queued")
        starts = [
            call for client in self.factory.clients for call in client.calls
            if call[0] == "turn/start"
        ]
        self.assertEqual(len(starts), 1)

    async def test_interrupt_waits_for_authoritative_terminal(self):
        await self.start()
        requested = await self.runtime.interrupt(
            "conversation-1",
            command_id="interrupt-1",
            expected_turn_id="turn-1",
        )
        self.assertEqual(requested["command"]["state"], "accepted")
        self.assertEqual(requested["snapshot"]["conversation"]["state"], "interrupting")
        await self.complete_turn(status="interrupted", text="stopped")
        snapshot = self.store.snapshot("conversation-1")
        self.assertEqual(snapshot["conversation"]["state"], "idle")
        self.assertEqual(snapshot["turns"][0]["status"], "interrupted")

    async def test_turn_started_before_rpc_response_binds_once(self):
        client = await self.runtime._ensure_client()
        client.notify_turn_started_before_response = True
        result = await self.start()
        self.assertEqual(result["command"]["result_turn_id"], "turn-1")
        self.assertEqual(len(result["snapshot"]["turns"]), 1)

    async def test_terminal_before_turn_response_never_regresses_to_active(self):
        client = await self.runtime._ensure_client()
        client.notify_turn_started_before_response = True
        client.notify_turn_completed_before_response = True
        result = await self.start()
        self.assertEqual(result["command"]["state"], "completed")
        self.assertEqual(result["snapshot"]["conversation"]["state"], "idle")
        self.assertEqual(result["snapshot"]["turns"][0]["status"], "completed")

    async def test_thread_started_never_recovers_lost_thread_start_response(self):
        client = await self.runtime._ensure_client()
        client.thread_start_error_after_notification = AppServerDisconnected("lost")
        result = await self.start()
        self.assertEqual(result["command"]["state"], "delivery_unknown")
        self.assertIsNone(result["snapshot"]["conversation"]["thread_id"])
        starts = [
            method
            for value in self.factory.clients
            for method, _params in value.calls
            if method in {"thread/start", "turn/start"}
        ]
        self.assertEqual(starts.count("thread/start"), 1)
        self.assertEqual(starts.count("turn/start"), 0)

    async def test_uncertain_turn_is_never_replayed(self):
        client = await self.runtime._ensure_client()
        client.start_turn_error = AppServerDisconnected("lost")
        result = await self.start()
        self.assertEqual(result["command"]["state"], "delivery_unknown")
        self.assertEqual(result["snapshot"]["conversation"]["state"], "needs_user")
        await self.runtime.reconcile("conversation-1")
        starts = [call for call in client.calls if call[0] == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.store.command("message-1")["state"], "delivery_unknown")

    async def test_disconnect_reconnects_and_reconciles_without_new_turn(self):
        await self.start()
        first = self.factory.clients[0]
        first.connected = False
        self.runtime.handle_disconnect(first.connection_epoch, "reader_eof", "EOF")
        await self.runtime._disconnect_recovery
        self.assertEqual(len(self.factory.clients), 2)
        self.assertEqual(
            sum(
                1
                for client in self.factory.clients
                for method, _ in client.calls
                if method == "turn/start"
            ),
            1,
        )
        self.assertEqual(
            self.store.get_conversation("conversation-1")["state"], "active"
        )

    async def test_startup_marks_unbound_thread_creation_uncertain_without_replay(self):
        self.store.set_conversation_state("conversation-1", "starting_thread")
        self.store.begin_command(
            "conversation-1", "message-crash", kind="start", text="maybe"
        )
        values = await self.runtime.startup_recover()
        self.assertEqual(values[0]["state"], "needs_user")
        self.assertEqual(
            values[0]["recovery_state"], "thread_creation_unconfirmed"
        )
        self.assertEqual(
            self.store.command("message-crash")["state"], "delivery_unknown"
        )
        self.assertEqual(self.factory.clients, [])

    async def test_startup_rejects_journal_only_command_before_native_delivery(self):
        self.store.bind_thread(
            "conversation-1", {"id": "thread-safe"}, server_version="fixture"
        )
        self.factory.threads["thread-safe"] = {
            "id": "thread-safe",
            "status": {"type": "idle"},
            "turns": [],
        }
        self.store.begin_command(
            "conversation-1", "message-safe", kind="steer", text="not sent"
        )
        await self.runtime.startup_recover()
        self.assertEqual(self.store.command("message-safe")["state"], "rejected")
        self.assertFalse(any(
            method in {"turn/start", "turn/steer"}
            for client in self.factory.clients
            for method, _params in client.calls
        ))

    async def test_startup_reconciled_terminal_uses_recovery_notification_filter(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        self.factory.threads["thread-1"]["turns"][0]["status"] = "completed"
        self.factory.threads["thread-1"]["turns"].append({
            "id": "turn-2", "status": "inProgress", "items": []})
        service = ConversationService(self.store, self.artifacts, self.runtime)
        delivered = asyncio.Event()

        async def terminal(cid, tid, snapshot):
            self.assertEqual(snapshot.get("terminal_source"), "reconcile")
            await service.handle_terminal(cid, tid, snapshot)
            delivered.set()

        self.runtime._terminal_handler = terminal
        with (mock.patch("shared.security.notify_task_completion") as notify,
              mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock())):
            await service.startup()
            await asyncio.wait_for(delivered.wait(), timeout=2)
        notify.assert_not_called()
        self.assertEqual(self.store.snapshot("conversation-1")["turns"][0]["notification_requested"], 1)

    async def test_startup_terminal_reconcile_notifies_and_drains_queue(self):
        await self.start()
        queued = await self.runtime.send_message(
            "conversation-1",
            command_id="message-2",
            text="after restart",
            force_new_turn=True,
        )
        self.assertEqual(queued["command"]["state"], "queued")
        native = self.factory.threads["thread-1"]["turns"][0]
        native.update({
            "status": "completed",
            "items": [{
                "id": "answer-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "done while web was away",
            }],
        })
        self.factory.threads["thread-1"]["status"] = {"type": "idle"}

        await self.runtime.startup_recover()
        for _ in range(100):
            if self.store.command("message-2")["state"] == "accepted":
                break
            await asyncio.sleep(0.01)

        self.assertIn(("conversation-1", "turn-1"), self.terminals)
        self.assertEqual(
            self.store.command("message-2")["result_turn_id"], "turn-2"
        )

    async def test_startup_drains_queue_left_after_local_terminal_commit(self):
        self.store.bind_thread(
            "conversation-1", {"id": "thread-idle"}, server_version="fixture"
        )
        self.factory.threads["thread-idle"] = {
            "id": "thread-idle",
            "status": {"type": "idle"},
            "turns": [],
        }
        self.store.begin_command(
            "conversation-1", "message-queued", kind="steer", text="resume queue"
        )
        self.store.update_command(
            "message-queued", "queued", reason="conversation_busy"
        )

        await self.runtime.startup_recover()
        for _ in range(100):
            if self.store.command("message-queued")["state"] == "accepted":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(
            self.store.command("message-queued")["result_turn_id"], "turn-1"
        )

    async def test_known_late_thread_notification_cannot_hijack_pending_start(self):
        self.store.create_conversation(
            "conversation-known",
            title="known",
            cwd=str(self.workspace),
            settings={
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
            },
        )
        self.store.bind_thread(
            "conversation-known", {"id": "thread-known"}, server_version="fixture"
        )
        client = await self.runtime._ensure_client()
        client.pre_thread_start_notification = {
            "id": "thread-known",
            "cwd": str(self.workspace),
            "turns": [],
        }
        result = await self.start()
        self.assertNotEqual(
            result["snapshot"]["conversation"]["thread_id"], "thread-known"
        )

    async def test_foreign_same_cwd_broadcast_never_binds_after_our_lost_response(self):
        client = await self.runtime._ensure_client()
        client.pre_thread_start_notification = {
            "id": "foreign-thread", "cwd": str(self.workspace), "turns": [],
        }
        client.thread_start_error_without_notification = AppServerDisconnected("lost")
        result = await self.start()
        self.assertEqual(result["command"]["state"], "delivery_unknown")
        self.assertIsNone(result["snapshot"]["conversation"]["thread_id"])

    async def test_delete_checks_native_idle_before_deleting_exact_thread(self):
        await self.start()
        with self.assertRaisesRegex(AppServerProtocolError, "not idle"):
            await self.runtime.delete_native_thread("conversation-1")
        await self.complete_turn()
        await self.runtime.delete_native_thread("conversation-1")
        deletes = [
            params
            for method, params in self.factory.clients[0].calls
            if method == "thread/delete"
        ]
        self.assertEqual(deletes, [{"thread_id": "thread-1"}])

    async def test_delete_response_loss_is_persisted_and_retry_settles_absence(self):
        await self.start()
        await self.complete_turn()
        client = self.factory.clients[0]
        client.delete_error_after_delivery = AppServerDisconnected("lost response")
        with self.assertRaises(AppServerDisconnected):
            await self.runtime.delete_native_thread("conversation-1")
        uncertain = self.store.get_conversation("conversation-1")
        self.assertEqual(uncertain["state"], "needs_user")
        self.assertEqual(
            uncertain["recovery_state"], "delete_delivery_unknown"
        )

        client.delete_error_after_delivery = None
        await self.runtime.delete_native_thread("conversation-1")
        settled = self.store.get_conversation("conversation-1")
        self.assertEqual(settled["state"], "idle")
        self.assertEqual(settled["recovery_state"], "native_thread_absent")

    async def test_delete_not_delivered_is_retried_against_existing_thread(self):
        await self.start()
        await self.complete_turn()
        client = self.factory.clients[0]
        client.delete_error_before_delivery = AppServerDisconnected("not delivered")
        with self.assertRaises(AppServerDisconnected):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertIn("thread-1", client.threads)

        client.delete_error_before_delivery = None
        await self.runtime.delete_native_thread("conversation-1")
        self.assertNotIn("thread-1", client.threads)
        deletes = [
            params for method, params in client.calls if method == "thread/delete"
        ]
        self.assertEqual(len(deletes), 2)
        settled = self.store.get_conversation("conversation-1")
        self.assertEqual(settled["recovery_state"], "native_thread_deleted")

    async def restart_delete_fixture(self):
        # Reopen real SQLite and discard all process-local Runtime state.
        await self.runtime.close()
        self.store.close()
        self.store = ConversationStore(self.root / "state" / "conversations.sqlite3")
        self.artifacts = ConversationArtifactService(self.store, self.root / "managed-artifacts")
        self.runtime = ConversationRuntime(self.store, self.artifacts, self.factory)
        return ConversationService(self.store, self.artifacts, self.runtime)

    async def uncertain_delete_fixture(self, *, delivered):
        await self.start()
        await self.complete_turn()
        client = self.factory.clients[0]
        setattr(client, "delete_error_after_delivery" if delivered else
                "delete_error_before_delivery", AppServerDisconnected("lost"))
        with self.assertRaises(AppServerDisconnected):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])

    async def test_delete_response_loss_after_sqlite_restart_cleans_only_owned_data(self):
        private_root = self.root / "private-workspaces"
        self.workspace = private_root / "conversation-1"
        self.workspace.mkdir(parents=True)
        self.store._db.execute("UPDATE conversations SET cwd=? WHERE id=?",
                               (str(self.workspace), "conversation-1"))
        upload = self.workspace / "upload.txt"
        upload.write_text("task-owned upload")
        sibling = private_root / "unrelated"
        sibling.mkdir()
        await self.uncertain_delete_fixture(delivered=True)
        published = self.artifacts.publish("conversation-1", turn_id="turn-1",
                                          source_path=str(upload), idempotency_key="r2-artifact", origin="tool")
        self.assertTrue(published)
        service = await self.restart_delete_fixture()
        await service.startup()
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        with mock.patch("engine.conversation_service.CONVERSATION_WORKSPACES", private_root):
            await service.delete("conversation-1")
        self.assertFalse(self.workspace.exists())
        self.assertTrue(sibling.exists())
        self.assertIsNone(self.store.conversation_for_thread("thread-1"))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(sum(m == "thread/delete" for c in self.factory.clients
                             for m, _ in c.calls), 1)

    async def test_delete_not_delivered_after_restart_retries_existing_idle_thread(self):
        await self.uncertain_delete_fixture(delivered=False)
        service = await self.restart_delete_fixture()
        await service.startup()
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "needs_user")
        self.assertIn("thread-1", self.factory.threads)
        await service.delete("conversation-1")
        self.assertNotIn("thread-1", self.factory.threads)
        self.assertEqual(sum(m == "thread/delete" for c in self.factory.clients
                             for m, _ in c.calls), 2)

    async def test_delete_intent_survives_manual_sync_disconnect_and_failed_reconnect(self):
        await self.uncertain_delete_fixture(delivered=True)
        await self.runtime.reconcile("conversation-1")
        with mock.patch.object(self.runtime, "_replace_client",
                               side_effect=AppServerDisconnected("offline")):
            await self.runtime._recover_disconnect("fixture", "offline")
        state = self.store.get_conversation("conversation-1")
        self.assertEqual(state["recovery_state"], "reconnect_failed")
        self.assertTrue(state["native_delete_pending"])
        await self.runtime._recover_disconnect("fixture", "offline")
        await ConversationService(self.store, self.artifacts, self.runtime).delete("conversation-1")
        self.assertIsNone(self.store.conversation_for_thread("thread-1"))

    async def test_delete_unknown_verification_failure_keeps_local_data_and_retry(self):
        await self.uncertain_delete_fixture(delivered=True)
        service = await self.restart_delete_fixture()
        await service.startup()
        client = self.factory.clients[-1]
        with mock.patch.object(client, "thread_exists", side_effect=AppServerDisconnected("lost list")):
            with self.assertRaises(AppServerDisconnected):
                await service.delete("conversation-1")
        self.assertTrue(self.workspace.exists())
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        await service.delete("conversation-1")
        self.assertIsNone(self.store.conversation_for_thread("thread-1"))

    async def test_pending_delete_active_native_must_settle_before_cleanup(self):
        await self.uncertain_delete_fixture(delivered=False)
        self.factory.threads["thread-1"]["turns"][0]["status"] = "inProgress"
        service = await self.restart_delete_fixture()
        await service.startup()
        with self.assertRaises(AppServerProtocolError):
            await service.delete("conversation-1")
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "needs_user")
        result = await self.runtime.send_message("conversation-1", command_id="pending-message", text="never send")
        self.assertEqual(result["command"]["reason"], "native_delete_pending")
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertTrue(self.workspace.exists())
        await self.complete_turn()
        await service.delete("conversation-1")
        self.assertNotIn("thread-1", self.factory.threads)

    async def test_pending_delete_reconciliation_does_not_advance_queued_work(self):
        await self.uncertain_delete_fixture(delivered=False)
        self.store.begin_command("conversation-1", "queued-r2", kind="steer", text="do not replay")
        self.store.update_command("queued-r2", "queued")
        await self.runtime.startup_recover()
        await self.runtime._drain_queue("conversation-1")
        self.assertEqual(self.store.command("queued-r2")["state"], "queued")
        for c in self.factory.clients:
            self.assertFalse(any(m == "turn/steer" for m, _ in c.calls))
        self.assertEqual(sum(m == "turn/start" for c in self.factory.clients for m, _ in c.calls), 1)
        for kind, action in [
            ("settings", self.runtime.update_settings("conversation-1", command_id="settings-r2", updates={"effort": "low"})),
            ("compact", self.runtime.compact("conversation-1", command_id="compact-r2")),
        ]:
            self.assertEqual((await action)["command"]["reason"], "native_delete_pending", kind)

    async def test_delete_inflight_delayed_terminal_notification_retains_intent(self):
        await self.start()
        await self.complete_turn()
        client = self.factory.clients[0]
        async def lose_response(thread_id):
            self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
            await self.runtime.handle_notification({"method": "turn/completed", "params": {
                "threadId": thread_id, "turn": self.factory.threads[thread_id]["turns"][0]}})
            self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
            raise AppServerDisconnected("lost response")
        with mock.patch.object(client, "delete_thread", side_effect=lose_response):
            with self.assertRaises(AppServerDisconnected):
                await self.runtime.delete_native_thread("conversation-1")
        await self.runtime.reconcile("conversation-1")
        await self.runtime.delete_native_thread("conversation-1")
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertFalse(self.store.source_delivery_is_eligible("conversation-1"))

    async def test_pending_delete_terminal_closes_question_without_replaying_queue(self):
        await self.uncertain_delete_fixture(delivered=False)
        native = self.factory.threads["thread-1"]
        # A newer native turn can become active before deletion is settled;
        # do not revive the already completed turn in the local projection.
        native["turns"].append({"id": "turn-2", "status": "inProgress", "items": []})
        native["status"] = {"type": "active"}
        await self.runtime.reconcile("conversation-1")
        self.store.begin_command("conversation-1", "queued-terminal", kind="steer", text="never replay")
        self.store.update_command("queued-terminal", "queued")
        pending = asyncio.create_task(self.runtime.handle_server_request(
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1", "turnId": "turn-2", "itemId": "ask-delete",
                "isBlocking": False, "autoResolutionMs": None,
                "questions": [{
                    "id": "why", "header": "Why", "question": "Why?",
                    "options": None, "isOther": False, "isSecret": False,
                }],
            }, request_id=0, connection_id="test-instance:1",
        ))
        try:
            await asyncio.sleep(0)
            question = self.store.snapshot("conversation-1")["questions"][0]
            deadline = self.runtime._question_deadlines[question["id"]]
            await self.complete_turn("turn-2", status="interrupted")
            await asyncio.sleep(0)
            self.assertEqual(self.store.question(question["id"])["state"], "closed")
            self.assertNotIn(question["id"], self.runtime._question_waiters)
            self.assertNotIn(question["id"], self.runtime._question_deadlines)
            self.assertTrue(pending.cancelled())
            self.assertTrue(deadline.cancelled())
            self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
            self.assertEqual(self.store.command("queued-terminal")["state"], "queued")
            self.assertFalse(any(m == "turn/steer" for c in self.factory.clients for m, _ in c.calls))
            self.assertEqual(self.terminals, [("conversation-1", "turn-1")])
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def assert_recovery_does_not_revive_settled_delete(self, recover):
        await self.start()
        await self.complete_turn()
        self.store.create_conversation(
            "recovery-earlier", title="unbound", cwd=str(self.workspace), settings={},
        )
        self.store.set_conversation_state("recovery-earlier", "needs_user")
        client = self.factory.clients[0]
        client.delete_error_before_delivery = AppServerDisconnected("lost")
        with self.assertRaises(AppServerDisconnected):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertEqual(self.store.recoverable_conversations()[0]["id"], "recovery-earlier")
        client.delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
        settled = False

        async def settle_during_earlier_status(conversation_id, snapshot):
            nonlocal settled
            if conversation_id == "recovery-earlier" and not settled:
                settled = True
                with self.assertRaises(AppServerRPCError):
                    await self.runtime.delete_native_thread("conversation-1")
                self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])

        self.runtime._status_handler = settle_during_earlier_status
        await recover()
        self.assertTrue(settled)
        self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])
        result = await self.runtime.send_message("conversation-1", command_id="after-rejection", text="continue")
        self.assertEqual(result["command"]["state"], "accepted")

    async def test_disconnect_reads_current_delete_intent_after_status_yield(self):
        await self.assert_recovery_does_not_revive_settled_delete(
            lambda: self.runtime._recover_disconnect("fixture", "offline"),
        )

    async def test_startup_reads_current_delete_intent_after_status_yield(self):
        await self.assert_recovery_does_not_revive_settled_delete(self.runtime.startup_recover)

    async def test_pending_delete_terminal_notice_stays_suppressed_after_real_restart(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.uncertain_delete_fixture(delivered=False)
        native = self.factory.threads["thread-1"]
        native["turns"].append({"id": "turn-2", "status": "inProgress", "items": []})
        native["status"] = {"type": "active"}
        await self.runtime.reconcile("conversation-1")
        await self.complete_turn("turn-2")
        turn = next(t for t in self.store.snapshot("conversation-1")["turns"] if t["id"] == "turn-2")
        self.assertTrue(turn["notification_requested"])
        self.assertIsNone(turn["notification_state"])
        service = await self.restart_delete_fixture()
        with mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify:
            await service.startup()
            notify.assert_not_called()
        self.assertEqual(self.store.pending_completion_notifications(), [])
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])

    async def test_pending_delete_slow_status_does_not_block_shared_notification_barrier(self):
        await self.uncertain_delete_fixture(delivered=False)
        self.store.begin_command("conversation-1", "queued-slow-status", kind="steer", text="never replay")
        self.store.update_command("queued-slow-status", "queued")
        self.store.create_conversation(
            "conversation-2", title="other", cwd=str(self.workspace),
            settings=self.store.get_conversation("conversation-1")["settings"],
        )
        await self.runtime.start_conversation("conversation-2", command_id="other-message", text="other")
        status_started = asyncio.Event()
        release_status = asyncio.Event()

        async def slow_status(conversation_id, snapshot):
            if conversation_id == "conversation-1":
                status_started.set()
                await release_status.wait()

        self.runtime._status_handler = slow_status
        driver = CodexAppServerClient(
            self.root / "never-connected-socket", request_timeout=0.5,
            notification_handler=self.runtime.handle_notification,
        )
        # Use the real ordered worker and its real flush barrier, but no
        # connection or native RPC. The Runtime still uses FakeClient.
        driver._connected = True
        driver._connection_epoch = 1
        driver._notification_task = asyncio.create_task(driver._notification_worker(1))
        try:
            for thread_id in ["thread-1", "thread-2"]:
                native = self.factory.threads[thread_id]
                native["turns"][0]["status"] = "completed"
                native["status"] = {"type": "idle"}
                await driver._notifications.put((1, {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": copy.deepcopy(native["turns"][0])},
                }))
            await asyncio.wait_for(status_started.wait(), timeout=2)
            await driver.flush_notifications()
            self.assertFalse(release_status.is_set())
            self.assertEqual(self.store.get_conversation("conversation-2")["state"], "idle")
            self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
            self.assertEqual(self.store.command("queued-slow-status")["state"], "queued")
        finally:
            release_status.set()
            await driver.close()

    async def pending_delete_active_fixture(self):
        await self.uncertain_delete_fixture(delivered=False)
        native = self.factory.threads["thread-1"]
        native["turns"].append({"id": "turn-2", "status": "inProgress", "items": []})
        native["status"] = {"type": "active"}
        await self.runtime.reconcile("conversation-1")
        return native

    async def test_pending_delete_records_resumed_question_activity_across_restart(self):
        await self.pending_delete_active_fixture()
        pending = asyncio.create_task(self.runtime.handle_server_request(
            "item/tool/requestUserInput", {
                "threadId": "thread-1", "turnId": "turn-2", "itemId": "pending-ask",
                "isBlocking": False, "autoResolutionMs": None,
                "questions": [{"id": "why", "header": "Why", "question": "Why?",
                               "options": None, "isOther": False, "isSecret": False}],
            }, request_id=0, connection_id="test-instance:1",
        ))
        try:
            await asyncio.sleep(0)
            question = self.store.snapshot("conversation-1")["questions"][0]
            await self.runtime.answer_question("conversation-1", question_id=question["id"],
                                               command_id="answer-pending", answers={"why": "because"})
            response = await pending
            await response.on_sent()
            await self.runtime.handle_notification({"method": "serverRequest/resolved", "params": {
                "threadId": "thread-1", "requestId": 0,
            }}, connection_id="test-instance:1")
            self.assertEqual(self.store.question(question["id"])["state"], "awaiting_activity")
            await self.runtime.handle_notification({"method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "turn-2", "item": {
                    "id": "pending-reason", "type": "reasoning", "summary": [],
                },
            }})
            self.assertEqual(self.store.question(question["id"])["state"], "answered")
            service = await self.restart_delete_fixture()
            await service.startup()
            self.assertEqual(self.store.question(question["id"])["state"], "answered")
            self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_pending_delete_ingests_image_before_rejection_and_restart(self):
        native = await self.pending_delete_active_fixture()
        source = self.workspace / "pending-generated.png"
        content = b"\x89PNG\r\n\x1a\nfixture"
        source.write_bytes(content)
        item = {"id": "pending-image", "type": "imageGeneration", "status": "completed",
                "result": "ok", "savedPath": str(source)}
        native["turns"][1]["items"] = [item]
        message = {"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-2", "item": item,
        }}
        await self.runtime.handle_notification(message)
        await self.runtime.handle_notification(message)
        artifact = self.store.artifact_for_key("conversation-1", "image:pending-image")
        self.assertIsNotNone(artifact)
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)
        source.unlink()  # Only the owned delivery copy can survive this.
        native["turns"][1]["status"] = "completed"
        native["status"] = {"type": "idle"}
        await self.runtime.handle_notification({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": native["turns"][1],
        }})
        self.factory.clients[0].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
        with self.assertRaises(AppServerRPCError):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])
        service = await self.restart_delete_fixture()
        await service.startup()
        await self.runtime.reconcile("conversation-1")
        artifact = self.store.artifact_for_key("conversation-1", "image:pending-image")
        self.assertEqual(artifact["origin"], "image_generation_item")
        self.assertEqual(self.artifacts.path_for(artifact).read_bytes(), content)

    async def test_pending_delete_preserves_async_choice_until_delivery_is_allowed(self):
        await self.pending_delete_active_fixture()
        await self.runtime.handle_notification({"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-2", "item": {
                "id": "pending-async", "type": "agentMessage", "phase": "final_answer",
                "delivery": "async", "text": "请选择", "questions": [{
                    "title": "采用哪个方案？", "options": ["方案 A", "方案 B"],
                }],
            },
        }})
        request = self.store.snapshot("conversation-1")["async_questions"][0]
        self.store._db.execute("UPDATE async_question_requests SET deadline_at=? WHERE id=?",
                               ("2000-01-01T00:00:00+00:00", request["id"]))
        await self.complete_turn("turn-2")
        service = await self.restart_delete_fixture()
        await service.startup()
        self.assertNotIn(request["id"], self.runtime._async_question_deadlines)
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.answer_async_question("conversation-1", request_id=request["id"],
                                                      command_id="pending-choice", answer_text="方案 B")
        # Also cover an old timer that was armed before deletion began.
        self.runtime._schedule_async_question_deadline(self.store.async_question_request(request["id"]))
        timer = self.runtime._async_question_deadlines[request["id"]]
        await timer
        saved = self.store.async_question_request(request["id"])
        self.assertEqual(saved["state"], "awaiting_answer")
        self.assertIsNone(saved["command_id"])
        self.factory.clients[-1].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
        with self.assertRaises(AppServerRPCError):
            await self.runtime.delete_native_thread("conversation-1")
        self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertIn(request["id"], self.runtime._async_question_deadlines)
        await self.runtime._async_question_deadlines[request["id"]]
        self.assertEqual(self.store.async_question_request(request["id"])["state"], "answered")

    async def notifiable_service_fixture(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        await self.complete_turn()
        await asyncio.sleep(0)
        service = ConversationService(self.store, self.artifacts, self.runtime)
        self.runtime._terminal_handler = service.handle_terminal
        return service

    async def consume_fixture_push(self, conversation_id, turn_id, snapshot):
        # Model the channel's persisted dispatch, even without subscriptions.
        self.store.prepare_web_push_deliveries(conversation_id, turn_id, "fixture-admin", resume_pending=True)

    async def test_rejected_delete_resumes_actual_deferred_callback_once(self):
        service = await self.notifiable_service_fixture()
        snapshot = self.store.snapshot("conversation-1")
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            async def reject(thread_id):
                await service.handle_terminal("conversation-1", "turn-1", snapshot)
                notify.assert_not_called()
                push.assert_not_awaited()
                raise AppServerRPCError(-32600, "delete denied")
            with mock.patch.object(self.factory.clients[0], "delete_thread", side_effect=reject):
                with self.assertRaises(AppServerRPCError):
                    await self.runtime.delete_native_thread("conversation-1")
            await asyncio.gather(*tuple(self.runtime._background))
            self.runtime.resume_deferred_terminal_deliveries("conversation-1")
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_awaited_once()

    async def test_deferred_live_delivery_survives_restart_and_rejection_crash_window(self):
        service = await self.notifiable_service_fixture()
        snapshot = self.store.snapshot("conversation-1")
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        await service.handle_terminal("conversation-1", "turn-1", snapshot)
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            notify.assert_not_called()
            push.assert_not_awaited()
        # A process can die after reserving the local callback but before
        # either channel has claimed delivery. The reservation is recoverable.
        self.store.set_conversation_state("conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False)
        self.assertEqual(len(self.store.take_deferred_terminal_deliveries("conversation-1")), 1)
        self.assertLess(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_awaited_once()

    async def test_deferred_delivery_never_retries_unknown_completion_channel(self):
        service = await self.notifiable_service_fixture()
        self.assertTrue(self.store.claim_completion_notification("conversation-1", "turn-1"))
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        await service.handle_terminal("conversation-1", "turn-1", self.store.snapshot("conversation-1"))
        self.store.set_conversation_state("conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False)
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_not_called()
            push.assert_awaited_once()
        self.assertEqual(self.store.snapshot("conversation-1")["turns"][0]["notification_state"], "unknown")

    async def terminal_ingestion_crash_fixture(self, *, pending_delete):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        if pending_delete:
            self.store.set_conversation_state("conversation-1", "active", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        native = self.factory.threads["thread-1"]
        native["turns"][0].update({"status": "completed", "items": [{
            "id": "crash-image", "type": "imageGeneration", "status": "completed",
            "result": "ok", "savedPath": str(self.workspace / "not-published.png"),
        }]})
        native["status"] = {"type": "idle"}
        started = asyncio.Event()
        async def stalled_ingestion(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with mock.patch("engine.conversation_runtime.asyncio.to_thread", side_effect=stalled_ingestion):
            task = asyncio.create_task(self.runtime.handle_notification({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": copy.deepcopy(native["turns"][0]),
            }}))
            await asyncio.wait_for(started.wait(), timeout=2)
            # Simulate a process lost inside the first ingestion await. No
            # Runtime code after that await can record a recovery event.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)

    async def assert_deferred_callback_after_restart(self):
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            if self.store.get_conversation("conversation-1")["native_delete_pending"]:
                notify.assert_not_called()
                push.assert_not_awaited()
                self.factory.clients[-1].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
                with self.assertRaises(AppServerRPCError):
                    await self.runtime.delete_native_thread("conversation-1")
                await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_awaited_once()

    async def test_live_terminal_marker_commits_before_pending_image_ingestion_crash(self):
        await self.terminal_ingestion_crash_fixture(pending_delete=True)
        await self.assert_deferred_callback_after_restart()

    async def test_live_terminal_marker_survives_delete_beginning_inside_ingestion(self):
        await self.terminal_ingestion_crash_fixture(pending_delete=False)
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        await self.assert_deferred_callback_after_restart()

    async def test_real_terminal_after_pending_reconcile_is_durably_deferred(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        self.store.set_conversation_state("conversation-1", "active", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        native = self.factory.threads["thread-1"]
        native["turns"][0].update({"status": "completed", "items": []})
        native["status"] = {"type": "idle"}
        await self.runtime.reconcile("conversation-1")
        self.assertEqual(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)
        await self.runtime.handle_notification({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": copy.deepcopy(native["turns"][0]),
        }})
        self.assertGreater(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)
        await self.assert_deferred_callback_after_restart()

    async def test_restart_of_observed_delivery_preserves_ntfy_supersession(self):
        await self.terminal_ingestion_crash_fixture(pending_delete=False)
        native = self.factory.threads["thread-1"]
        native["turns"].append({"id": "turn-2", "status": "inProgress", "items": []})
        native["status"] = {"type": "active"}
        self.store.reconcile_thread("conversation-1", {"thread": native})
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_not_called()
            push.assert_awaited_once()

    async def test_late_wire_after_delete_rejection_delivers_without_more_actions(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        native = self.factory.threads["thread-1"]
        native["turns"][0].update({"status": "completed", "items": []})
        native["status"] = {"type": "idle"}
        self.store.set_conversation_state("conversation-1", "active", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        await self.runtime.reconcile("conversation-1")
        service = ConversationService(self.store, self.artifacts, self.runtime)
        self.runtime._terminal_handler = service.handle_terminal
        self.factory.clients[0].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            with self.assertRaises(AppServerRPCError):
                await self.runtime.delete_native_thread("conversation-1")
            await self.runtime.handle_notification({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": copy.deepcopy(native["turns"][0]),
            }})
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_awaited_once()
            self.assertFalse(any(m == "turn/steer" for c in self.factory.clients for m, _ in c.calls))

    async def test_reconcile_callback_during_delete_does_not_authorize_push_recovery(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        native = self.factory.threads["thread-1"]
        native["turns"][0].update({"status": "completed", "items": []})
        native["status"] = {"type": "idle"}
        self.store.reconcile_thread("conversation-1", {"thread": native})
        service = ConversationService(self.store, self.artifacts, self.runtime)
        self.runtime._terminal_handler = service.handle_terminal
        def enter_delete_during_ntfy(*args, **kwargs):
            self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
            return True, "fixture"
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", side_effect=enter_delete_during_ntfy),
              mock.patch.object(service, "_web_push_owner", return_value="fixture-admin")):
            await service.handle_terminal("conversation-1", "turn-1", {
                **self.store.snapshot("conversation-1"), "terminal_source": "reconcile",
            })
            self.assertEqual(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)
            self.factory.clients[0].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
            with self.assertRaises(AppServerRPCError):
                await self.runtime.delete_native_thread("conversation-1")
            await asyncio.gather(*tuple(self.runtime._background))
            self.assertEqual(self.store._db.execute("SELECT count(*) FROM web_push_dispatches").fetchone()[0], 0)

    async def test_native_confirmation_holds_intent_through_local_cleanup_failure_and_restart(self):
        private_root = self.root / "private-workspaces"
        self.workspace = private_root / "conversation-1"
        self.workspace.mkdir(parents=True)
        (self.workspace / "owned.txt").write_text("fixture")
        self.store._db.execute("UPDATE conversations SET cwd=? WHERE id='conversation-1'", (str(self.workspace),))
        service = await self.notifiable_service_fixture()
        with mock.patch.object(service, "_remove_private_workspace", side_effect=OSError("fixture cleanup failure")):
            with self.assertRaises(OSError):
                await service.delete("conversation-1")
        self.assertNotIn("thread-1", self.factory.threads)
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertFalse(self.store.source_delivery_is_eligible("conversation-1"))
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock()) as push,
              mock.patch("engine.conversation_service.CONVERSATION_WORKSPACES", private_root)):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_not_called()
            push.assert_not_awaited()
            await service.delete("conversation-1")
        self.assertFalse(self.workspace.exists())
        self.assertIsNone(self.store.conversation_for_thread("thread-1"))
        self.assertEqual(sum(m == "thread/delete" for c in self.factory.clients for m, _ in c.calls), 1)

    async def test_unbound_native_phase_holds_local_delete_intent_without_rpc(self):
        await self.runtime.delete_native_thread("conversation-1")
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertFalse(self.store.source_delivery_is_eligible("conversation-1"))
        self.assertEqual(self.factory.clients, [])
        service = await self.restart_delete_fixture()
        await service.startup()
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        await service.delete("conversation-1")
        self.assertFalse(any(m in {"thread/start", "turn/start", "thread/delete"} for c in self.factory.clients for m, _ in c.calls))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)

    async def test_pending_unbound_delete_blocks_native_start(self):
        await self.runtime.delete_native_thread("conversation-1")
        result = await self.runtime.start_conversation("conversation-1", command_id="start-during-delete", text="must not run")
        self.assertEqual(result["command"]["state"], "rejected")
        self.assertFalse(any(m in {"thread/start", "turn/start"} for c in self.factory.clients for m, _ in c.calls))

    async def test_delete_rechecks_unbound_creation_uncertainty_after_facade_snapshot(self):
        private_root = self.root / "private-raced-workspaces"
        self.workspace = private_root / "conversation-1"
        self.workspace.mkdir(parents=True)
        (self.workspace / "owned.txt").write_text("fixture")
        self.store._db.execute("UPDATE conversations SET cwd=? WHERE id='conversation-1'", (str(self.workspace),))
        service = ConversationService(self.store, self.artifacts, self.runtime)
        await self.runtime._ensure_client()
        original_delete = self.runtime.delete_native_thread
        async def lose_binding_before_lock(conversation_id):
            self.factory.clients[0].thread_start_error_without_notification = AppServerDisconnected("fixture lost creation receipt")
            await self.runtime.start_conversation(conversation_id, command_id="raced-start", text="first")
            self.assertIsNone(self.store.get_conversation(conversation_id)["thread_id"])
            self.assertEqual(self.store.get_conversation(conversation_id)["state"], "needs_user")
            await original_delete(conversation_id)
        with (mock.patch.object(self.runtime, "delete_native_thread", side_effect=lose_binding_before_lock),
              mock.patch("engine.conversation_service.CONVERSATION_WORKSPACES", private_root)):
            with self.assertRaises(AppServerProtocolError):
                await service.delete("conversation-1")
        self.assertTrue(self.workspace.exists())
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
        self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])
        self.assertEqual(len(self.factory.threads), 1)

    async def deferred_supersession_fixture(self):
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        await self.complete_turn()
        await asyncio.sleep(0)
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        native = self.factory.threads["thread-1"]
        native["turns"].append({"id": "turn-2", "status": "completed", "items": []})
        native["status"] = {"type": "idle"}
        await self.runtime.reconcile("conversation-1")

    async def test_restart_held_delivery_keeps_recovery_policy_after_rejection_and_wire_replay(self):
        await self.deferred_supersession_fixture()
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            await service.startup()
            await self.runtime.handle_notification({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": copy.deepcopy(self.factory.threads["thread-1"]["turns"][0]),
            }})
            self.factory.clients[-1].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
            with self.assertRaises(AppServerRPCError):
                await self.runtime.delete_native_thread("conversation-1")
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_not_called()
            push.assert_awaited_once()
            self.assertTrue(all(e["turn_id"] != "turn-1" for e in self.store.pending_completion_notifications()))

    async def test_live_held_delivery_keeps_per_turn_policy_after_rejection(self):
        await self.deferred_supersession_fixture()
        service = ConversationService(self.store, self.artifacts, self.runtime)
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("routes.ws.manager.broadcast", new=mock.AsyncMock()),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock(side_effect=self.consume_fixture_push)) as push):
            self.factory.clients[0].delete_error_before_delivery = AppServerRPCError(-32600, "delete denied")
            with self.assertRaises(AppServerRPCError):
                await self.runtime.delete_native_thread("conversation-1")
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_awaited_once()

    async def test_unbound_cleanup_failure_remains_retryable_after_shared_disconnect(self):
        private_root = self.root / "private-disconnect-workspaces"
        self.workspace = private_root / "conversation-1"
        self.workspace.mkdir(parents=True)
        (self.workspace / "owned.txt").write_text("fixture")
        self.store._db.execute("UPDATE conversations SET cwd=? WHERE id='conversation-1'", (str(self.workspace),))
        service = ConversationService(self.store, self.artifacts, self.runtime)
        client = await self.runtime._ensure_client()
        with mock.patch.object(service, "_remove_private_workspace", side_effect=OSError("fixture cleanup failure")):
            with self.assertRaises(OSError):
                await service.delete("conversation-1")
        self.runtime.handle_disconnect(client.connection_epoch, "fixture", "AppServerDisconnected")
        await self.runtime._disconnect_recovery
        self.assertEqual(self.store.get_conversation("conversation-1")["state"], "needs_user")
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        await self.runtime.reconcile("conversation-1")
        with (mock.patch("engine.conversation_service.CONVERSATION_WORKSPACES", private_root),
              mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify):
            await service.delete("conversation-1")
            notify.assert_not_called()
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)
        self.assertFalse(any(m in {"thread/start", "turn/start", "thread/delete"} for c in self.factory.clients for m, _ in c.calls))

    async def test_queued_status_after_source_cleanup_does_not_broadcast_or_fail(self):
        self.store.delete_conversation("conversation-1")
        await self.runtime._emit_status("conversation-1")
        self.assertEqual(self.statuses, [])

    async def test_startup_without_live_deferral_does_not_backfill_push_history(self):
        # Historical snapshot, not an observed wire callback. Ntfy's existing
        # recovery policy may apply; Push must not scan this history.
        self.store.set_completion_notification("conversation-1", True)
        await self.start()
        native = self.factory.threads["thread-1"]
        native["turns"][0].update({"status": "completed", "items": []})
        native["status"] = {"type": "idle"}
        self.store.reconcile_thread("conversation-1", {"thread": native})
        service = await self.restart_delete_fixture()
        self.runtime._terminal_handler = service.handle_terminal
        with (mock.patch("shared.security.notify_task_completion", return_value=(True, "fixture")) as notify,
              mock.patch.object(service, "_deliver_web_push_notification", new=mock.AsyncMock()) as push):
            await service.startup()
            await asyncio.gather(*tuple(self.runtime._background))
            notify.assert_called_once()
            push.assert_not_awaited()

    async def test_legacy_inflight_delete_marker_is_migrated_on_reopen(self):
        await self.start()
        await self.complete_turn()
        self.store.set_conversation_state("conversation-1", "reconciling",
                                          recovery_state="deleting_native_thread")
        # An old v6 database has no additive journal column.
        self.store._db.execute("ALTER TABLE conversations DROP COLUMN native_delete_pending")
        self.factory.threads.pop("thread-1")
        service = await self.restart_delete_fixture()
        self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
        await service.startup()
        await service.delete("conversation-1")
        self.assertIsNone(self.store.conversation_for_thread("thread-1"))

    async def test_non_delete_uncertainty_does_not_gain_cleanup_qualification(self):
        await self.start()
        await self.complete_turn()
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="thread_unavailable")
        self.factory.threads.pop("thread-1")
        with self.assertRaises(AppServerProtocolError):
            await ConversationService(self.store, self.artifacts, self.runtime).delete("conversation-1")
        self.assertTrue(self.workspace.exists())
        self.assertFalse(self.store.get_conversation("conversation-1")["native_delete_pending"])

    async def test_dynamic_tool_publishes_and_replays_one_artifact(self):
        await self.start()
        source = self.workspace / "answer.txt"
        source.write_text("hello", encoding="utf-8")
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "tool": "publish_artifact",
            "arguments": {"path": str(source), "title": "answer.txt"},
        }
        first = await self.runtime.handle_server_request("item/tool/call", params)
        replay = await self.runtime.handle_server_request("item/tool/call", params)
        self.assertEqual(first, replay)
        self.assertEqual(first["contentItems"][0]["type"], "inputText")
        visible = json.loads(first["contentItems"][0]["text"])
        self.assertIs(visible["success"], True)
        self.assertEqual(visible["download_url"], "/conversation/conversation-1/artifact/" + visible["artifact_id"] + "?download=1")
        artifacts = self.store.snapshot("conversation-1")["turns"][0]["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["preview_kind"], "text")

    async def test_dynamic_tool_failure_replays_and_marks_turn_without_artifact(self):
        await self.start()
        missing = self.workspace / "missing-secret-name.txt"
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-failed",
            "tool": "publish_artifact",
            "arguments": {"path": str(missing)},
        }
        first = await self.runtime.handle_server_request("item/tool/call", params)
        replay = await self.runtime.handle_server_request("item/tool/call", params)
        self.assertEqual(first, replay)
        self.assertIs(first["success"], False)
        self.assertIs(json.loads(first["contentItems"][0]["text"])["success"], False)
        snapshot = self.store.snapshot("conversation-1")
        self.assertTrue(snapshot["turns"][0]["artifact_publish_failed"])
        self.assertEqual(snapshot["turns"][0]["artifacts"], [])

        source = self.workspace / "published-after-failure.txt"
        source.write_text("published", encoding="utf-8")
        success = await self.runtime.handle_server_request("item/tool/call", {
            **params,
            "callId": "call-success-after-failure",
            "arguments": {"path": str(source)},
        })
        self.assertIs(success["success"], True)
        snapshot = self.store.snapshot("conversation-1")
        self.assertTrue(snapshot["turns"][0]["artifact_publish_failed"])
        self.assertEqual(len(snapshot["turns"][0]["artifacts"]), 1)

    async def test_native_image_generation_item_becomes_artifact(self):
        await self.start()
        source = self.workspace / "generated.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        await self.runtime.handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "image-1",
                    "type": "imageGeneration",
                    "status": "completed",
                    "result": "ok",
                    "savedPath": str(source),
                },
            },
        })
        artifact = self.store.snapshot("conversation-1")["turns"][0]["artifacts"][0]
        self.assertEqual(artifact["origin"], "image_generation_item")
        self.assertEqual(artifact["preview_kind"], "image")

    async def test_unchanged_turn_settings_and_fast_do_not_queue_followup(self):
        await self.start()
        result = await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-1",
            updates={
                "model": "fixture-model",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": "priority",
            },
        )
        self.assertEqual(result["command"]["state"], "completed")
        calls = [method for method, _ in self.factory.clients[0].calls]
        self.assertIn("thread/settings/update", calls)
        self.assertFalse(result["snapshot"]["conversation"]["settings"].get("pending_for_next_turn"))

        followup = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="keep steering"
        )
        self.assertEqual(followup["command"]["state"], "accepted")
        self.assertEqual(followup["command"]["result_turn_id"], "turn-1")
        steers = [params for method, params in self.factory.clients[0].calls if method == "turn/steer"]
        self.assertEqual(steers[-1]["thread_id"], "thread-1")
        self.assertEqual(steers[-1]["turn_id"], "turn-1")
        self.assertEqual(steers[-1]["text"], "keep steering")
        self.assertEqual(steers[-1]["client_user_message_id"], "message-2")

        result = await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-2",
            updates={"service_tier": None},
        )
        self.assertEqual(result["command"]["state"], "completed")
        self.assertIsNone(
            result["snapshot"]["conversation"]["settings"]["service_tier"]
        )
        self.assertIn(
            ("thread/settings/update", {"service_tier": None}),
            self.factory.clients[0].calls,
        )

    async def test_reverting_active_turn_setting_clears_pending_and_steers(self):
        await self.start()
        changed = await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-b",
            updates={"permission": "workspace-write"},
        )
        self.assertTrue(changed["snapshot"]["conversation"]["settings"]["pending_for_next_turn"])
        reverted = await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-a",
            updates={"permission": "danger-full-access"},
        )
        settings = reverted["snapshot"]["conversation"]["settings"]
        self.assertFalse(settings["pending_for_next_turn"])
        self.assertIsNone(settings["_pending_turn_settings_base"])
        followup = await self.runtime.send_message(
            "conversation-1", command_id="message-after-revert", text="steer after revert"
        )
        self.assertEqual(followup["command"]["state"], "accepted")
        self.assertEqual(followup["command"]["result_turn_id"], "turn-1")

    async def test_plan_mode_marks_active_turn_pending_and_reaches_native_options(self):
        await self.start()
        changed = await self.runtime.update_settings(
            "conversation-1", command_id="settings-plan",
            updates={"collaboration_mode": "plan"},
        )
        self.assertTrue(changed["snapshot"]["conversation"]["settings"]["pending_for_next_turn"])
        self.assertEqual(changed["snapshot"]["conversation"]["settings"]["collaboration_mode"], "plan")
        await self.complete_turn()
        await self.runtime.send_message(
            "conversation-1", command_id="message-plan", text="plan next turn",
            force_new_turn=True,
        )
        options = [value for name, value in self.factory.clients[0].calls if name == "turn/start"][-1]["options"]
        self.assertEqual(options["collaborationMode"], {
            "mode": "plan",
            "settings": {
                "model": "fixture-model", "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        })

    async def test_actual_permission_change_marks_next_turn_pending(self):
        await self.start()
        result = await self.runtime.update_settings(
            "conversation-1",
            command_id="settings-permission",
            updates={"permission": "workspace-write"},
        )
        self.assertTrue(result["snapshot"]["conversation"]["settings"]["pending_for_next_turn"])
        queued = await self.runtime.send_message(
            "conversation-1", command_id="message-2", text="use the new permission"
        )
        self.assertEqual(queued["command"]["state"], "queued")
        self.assertEqual(queued["command"]["reason"], "next_turn_required")


if __name__ == "__main__":
    unittest.main()
