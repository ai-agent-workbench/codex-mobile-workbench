import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_store import ConversationStore, SCHEMA_VERSION


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.store = ConversationStore(self.root / "conversations.sqlite3")
        self.store.create_conversation(
            "conversation-1",
            title="App Server only",
            cwd="/tmp",
            settings={
                "model": "fixture",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": None,
            },
        )
        self.store.bind_thread(
            "conversation-1", {"id": "thread-1"}, server_version="fixture"
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _bind_turn(self, command_id, turn_id, text, kind="start"):
        self.store.begin_command(
            "conversation-1", command_id, kind=kind, text=text
        )
        self.store.bind_turn(
            "conversation-1",
            {"id": turn_id, "status": "inProgress"},
            command_id=command_id,
        )

    def _complete(self, turn_id, text, status="completed"):
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": turn_id,
                "item": {
                    "id": f"answer-{turn_id}",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": text,
                },
            },
        })
        return self.store.apply_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {
                    "id": turn_id,
                    "status": status,
                    "items": [],
                },
            },
        })

    @staticmethod
    def _token_usage(**overrides):
        usage = {
            "last": {
                "inputTokens": 125,
                "cachedInputTokens": 100,
                "outputTokens": 12,
                "reasoningOutputTokens": 4,
                "totalTokens": 141,
            },
            "total": {
                "inputTokens": 500,
                "cachedInputTokens": 420,
                "outputTokens": 60,
                "reasoningOutputTokens": 20,
                "totalTokens": 580,
            },
            "modelContextWindow": 1000,
            "updatedAt": "2026-08-25T10:00:00+00:00",
        }
        usage.update(overrides)
        return usage

    def test_targeted_runtime_read_projections_do_not_select_items(self):
        self._bind_turn("message-1", "turn-1", "first")
        self.store.set_conversation_state(
            "conversation-1", "active", active_turn_id="turn-1"
        )
        # Exercise exact cutoff equality, excluded NULL starts, and the two
        # terminal checkpoint fields without involving a public snapshot.
        self.store._db.execute(
            "UPDATE turns SET started_at = ? WHERE id = ?",
            ("2026-10-18T00:00:00+00:00", "turn-1"),
        )
        self.store._db.execute(
            """INSERT INTO turns(id, conversation_id, ordinal, status, kind,
               started_at, completed_at) VALUES(?,?,?,?,?,?,?)""",
            ("turn-2", "conversation-1", 2, "completed", "model",
             "2026-10-18T00:00:01+00:00", None),
        )
        self.store._db.execute(
            """INSERT INTO turns(id, conversation_id, ordinal, status, kind,
               started_at, completed_at) VALUES(?,?,?,?,?,?,?)""",
            ("turn-null", "conversation-1", 3, "failed", "model", None,
             "2026-10-18T00:00:02+00:00"),
        )
        traces: list[str] = []
        self.store._db.set_trace_callback(traces.append)
        try:
            self.assertTrue(
                self.store.initial_title_source_live("conversation-1", "turn-1")
            )
            self.assertEqual(
                self.store.turn_ids_started_at_or_before(
                    "conversation-1", "2026-10-18T00:00:00+00:00"
                ),
                ["turn-1"],
            )
            checkpoints = self.store.turn_terminal_checkpoints("conversation-1")
            self.assertEqual(
                checkpoints["turn-1"],
                {"id": "turn-1", "status": "inProgress", "completed_at": None},
            )
            self.assertEqual(
                checkpoints["turn-2"]["completed_at"], None,
            )
            self.assertEqual(
                checkpoints["turn-null"],
                {"id": "turn-null", "status": "failed",
                 "completed_at": "2026-10-18T00:00:02+00:00"},
            )
            self.assertEqual(
                self.store.question_request_ids_for_deadline_cancel("conversation-1"),
                ([], []),
            )
        finally:
            self.store._db.set_trace_callback(None)
        self.assertFalse(any(" items" in query.lower() for query in traces))
        self.store.begin_command(
            "conversation-1", "stop-1", kind="interrupt", target_turn_id="turn-1"
        )
        self.assertFalse(self.store.initial_title_source_live("conversation-1", "turn-1"))
        self.store.update_command("stop-1", "rejected")
        self.store.set_conversation_state(
            "conversation-1", "active", native_delete_pending=True
        )
        self.assertFalse(self.store.initial_title_source_live("conversation-1", "turn-1"))

    def test_one_conversation_owns_multiple_native_turns(self):
        self._bind_turn("message-1", "turn-1", "first")
        first = self._complete("turn-1", "one")
        self.assertTrue(first["terminal"])

        self._bind_turn("message-2", "turn-2", "second")
        self._complete("turn-2", "two")
        snapshot = self.store.snapshot("conversation-1")

        self.assertEqual(snapshot["conversation"]["thread_id"], "thread-1")
        self.assertEqual(snapshot["conversation"]["state"], "idle")
        self.assertEqual(
            [(turn["id"], turn["final_text"]) for turn in snapshot["turns"]],
            [("turn-1", "one"), ("turn-2", "two")],
        )
        self.assertNotIn("parent_id", snapshot["conversation"])
        self.assertNotIn("pid", snapshot["conversation"])

    def test_existing_store_adds_conversation_item_snapshot_index(self):
        path = self.root / "legacy-items.sqlite3"
        legacy = ConversationStore(path)
        legacy._db.execute("DROP INDEX items_conversation_idx")
        legacy.close()

        reopened = ConversationStore(path)
        try:
            columns = [
                row[2]
                for row in reopened._db.execute(
                    "PRAGMA index_info(items_conversation_idx)"
                ).fetchall()
            ]
            self.assertEqual(columns, ["conversation_id", "row_id"])
            plan = " ".join(
                row[3]
                for row in reopened._db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM items "
                    "WHERE conversation_id = ? ORDER BY row_id ASC",
                    ("conversation-1",),
                ).fetchall()
            )
            self.assertIn("items_conversation_idx", plan)
        finally:
            reopened.close()

    def test_completed_item_wins_and_late_delta_is_ignored(self):
        self._bind_turn("message-1", "turn-1", "hello")
        self.store.apply_notification({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "answer-1",
                "delta": "draft",
            },
        })
        self._complete("turn-1", "authoritative")
        late = self.store.apply_notification({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "answer-1",
                "delta": " late",
            },
        })

        self.assertFalse(late["changed"])
        turn = self.store.snapshot("conversation-1")["turns"][0]
        self.assertEqual(turn["final_text"], "authoritative")
        self.assertEqual(turn["status"], "completed")

    def test_async_agent_message_keeps_native_questions_without_becoming_final(self):
        self._bind_turn("message-async", "turn-async", "continue")
        item = {
            "id": "call-async", "type": "agentMessage",
            "phase": "final_answer", "delivery": "async",
            "text": "是否继续？\n- 继续\n- 暂停\n\n补充说明",
            "questions": [
                {"title": "是否继续？", "options": ["继续", "暂停"]},
                {"title": "请补充说明", "options": None},
            ],
        }
        self.store.apply_notification({
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-async", "item": item},
        })
        stored = self.store.snapshot("conversation-1")["turns"][0]["items"][0]
        self.assertEqual(stored["delivery"], "async")
        self.assertEqual(stored["questions"], item["questions"])
        self.assertEqual(stored["text"], item["text"])

        self.store.apply_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-async", "status": "completed", "items": [item]},
            },
        })
        terminal = self.store.snapshot("conversation-1")["turns"][0]
        self.assertIsNone(terminal["final_text"])

    def test_malformed_async_questions_keep_readable_fallback_only(self):
        self._bind_turn("message-async-bad", "turn-async-bad", "continue")
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-async-bad",
                "item": {
                    "id": "call-async-bad", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async",
                    "text": "仍可阅读的原生回退文本", "questions": [{"title": "", "options": []}],
                },
            },
        })
        item = self.store.snapshot("conversation-1")["turns"][0]["items"][0]
        self.assertEqual(item["delivery"], "async")
        self.assertIsNone(item["questions"])
        self.assertEqual(item["text"], "仍可阅读的原生回退文本")

    def test_async_input_projection_schema_is_additive(self):
        columns = {
            row["name"] for row in self.store._db.execute(
                "PRAGMA table_info(items)"
            ).fetchall()
        }
        self.assertTrue({"delivery", "questions_json"}.issubset(columns))
        self.assertEqual(
            self.store._db.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'async_user_input_schema_version'"
            ).fetchone()["value"],
            "1",
        )
        self.assertEqual(
            self.store._db.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'async_auto_question_schema_version'"
            ).fetchone()["value"],
            "1",
        )
        command_columns = {
            row["name"] for row in self.store._db.execute(
                "PRAGMA table_info(commands)"
            ).fetchall()
        }
        self.assertIn("origin", command_columns)

    def test_async_question_extension_backfills_live_existing_card(self):
        self._bind_turn("message-async-backfill", "turn-async-backfill", "continue")
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-async-backfill",
                "item": {
                    "id": "call-async-backfill", "type": "agentMessage",
                    "phase": "final_answer", "delivery": "async", "text": "选择",
                    "questions": [{"title": "采用哪项？", "options": ["A", "B"]}],
                },
            },
        })
        self.store._db.execute("DELETE FROM async_question_requests")
        self.store.close()
        self.store = ConversationStore(self.root / "conversations.sqlite3")
        requests = self.store.snapshot("conversation-1")["async_questions"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["native_item_id"], "call-async-backfill")
        self.assertEqual(requests[0]["recommended_answer"], "A")
        self.assertIsNotNone(requests[0]["deadline_at"])

    def test_token_usage_notification_is_normalized_and_persisted(self):
        changed = self.store.apply_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": self._token_usage(),
            },
        })
        self.assertTrue(changed["changed"])
        self.assertEqual(
            self.store.get_conversation("conversation-1")["token_usage"],
            self._token_usage(),
        )
        ignored = self.store.apply_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": self._token_usage(last={"inputTokens": "125"}),
            },
        })
        self.assertFalse(ignored["changed"])
        self.assertEqual(
            self.store.get_conversation("conversation-1")["token_usage"],
            self._token_usage(),
        )

    def test_token_usage_allows_null_window_and_supplies_observed_time(self):
        usage = self._token_usage(modelContextWindow=None)
        usage.pop("updatedAt")
        changed = self.store.apply_notification({
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thread-1", "tokenUsage": usage},
        })
        self.assertTrue(changed["changed"])
        stored = self.store.get_conversation("conversation-1")["token_usage"]
        self.assertIsNone(stored["modelContextWindow"])
        self.assertTrue(stored["updatedAt"])

    def test_final_thread_usage_after_terminal_turn_is_not_dropped(self):
        self._bind_turn("message-usage", "turn-usage", "measure")
        self._complete("turn-usage", "done")
        changed = self.store.apply_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1", "turnId": "turn-usage",
                "tokenUsage": self._token_usage(),
            },
        })
        self.assertTrue(changed["changed"])
        self.assertEqual(
            self.store.get_conversation("conversation-1")["token_usage"]["last"]["inputTokens"],
            125,
        )

    def test_reconcile_reuses_thread_token_usage_validation(self):
        snapshot = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1",
                "status": {"type": "idle"},
                "turns": [],
                "tokenUsage": self._token_usage(),
            }
        })
        self.assertEqual(
            snapshot["conversation"]["token_usage"]["last"]["inputTokens"],
            125,
        )

    def test_reconcile_ignores_malformed_optional_usage(self):
        snapshot = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "idle"}, "turns": [],
                "tokenUsage": {"last": {"inputTokens": "bad"}},
            }
        })
        self.assertIsNone(snapshot["conversation"]["token_usage"])

    def test_reconcile_preserves_terminal_observed_before_checkpoint(self):
        for index, status in enumerate(("completed", "failed", "interrupted")):
            with self.subTest(status=status):
                old_id, new_id = f"old-{index}", f"new-{index}"
                self._bind_turn(f"old-command-{index}", old_id, "old task")
                self._complete(old_id, "retained answer", status=status)
                self.store._db.execute(
                    "UPDATE turns SET error_json = ?, notification_state = 'sent' WHERE id = ?",
                    (json.dumps({"message": "retained error"}), old_id),
                )
                self._bind_turn(f"new-command-{index}", new_id, "new task")
                before = self.store.snapshot("conversation-1")
                old_before = next(t for t in before["turns"] if t["id"] == old_id)
                checkpoint = self.store.capture_reconcile_checkpoint("conversation-1")
                native_turns = [
                    {"id": t["id"], "status": t["status"], "items": []}
                    for t in before["turns"]
                ]
                next(t for t in native_turns if t["id"] == old_id).update({
                    "status": "inProgress", "items": [{
                        "id": f"answer-{old_id}", "type": "agentMessage",
                        "phase": "final_answer", "text": "stale answer",
                    }],
                })
                value = self.store.reconcile_thread("conversation-1", {"thread": {
                    "id": "thread-1", "turns": native_turns,
                }}, checkpoint=checkpoint)
                old_after = next(t for t in value["turns"] if t["id"] == old_id)
                self.assertEqual(old_after, old_before)
                self.assertEqual(value["conversation"]["state"], "active")
                self.assertEqual(value["conversation"]["active_turn_id"], new_id)
                self.assertEqual(value["conversation"]["terminal_generation"],
                                 before["conversation"]["terminal_generation"])
                self._complete(new_id, "new result")

    def test_reconcile_repairs_ended_legacy_turn_without_replaying_completion(self):
        self._bind_turn("old-command", "old-turn", "old task")
        self._complete("old-turn", "retained answer", status="interrupted")
        self._bind_turn("new-command", "new-turn", "new task")
        self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='old-turn'")
        before = self.store.snapshot("conversation-1")
        old_before = next(t for t in before["turns"] if t["id"] == "old-turn")
        for newer_status in ("inProgress", "completed"):
            with self.subTest(newer_status=newer_status):
                # Simulate the same regression again to check recovery when
                # the later native turn finishes during a Web restart.
                self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='old-turn'")
                value = self.store.reconcile_thread("conversation-1", {"thread": {
                    "id": "thread-1", "turns": [
                        {"id": "old-turn", "status": "inProgress", "items": []},
                        {"id": "new-turn", "status": newer_status, "items": []},
                    ],
                }}, checkpoint=self.store.capture_reconcile_checkpoint("conversation-1"))
                old_after = next(t for t in value["turns"] if t["id"] == "old-turn")
                self.assertEqual(old_after, {**old_before, "status": "interrupted"})
                self.assertEqual(self.store.command("old-command")["state"], "completed")
                self.assertEqual(value["conversation"]["state"],
                                 "active" if newer_status == "inProgress" else "idle")
                if newer_status == "inProgress":
                    self.assertEqual(value["conversation"]["active_turn_id"], "new-turn")
                    self.assertEqual(value["conversation"]["terminal_generation"],
                                     before["conversation"]["terminal_generation"])

    def test_reconcile_does_not_guess_latest_legacy_terminal_or_real_multi_active(self):
        self._bind_turn("old-command", "old-turn", "old task")
        self._complete("old-turn", "answer")
        self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='old-turn'")
        value = self.store.reconcile_thread("conversation-1", {"thread": {
            "id": "thread-1", "turns": [
                {"id": "old-turn", "status": "inProgress", "items": []},
            ],
        }})
        self.assertEqual(value["conversation"]["state"], "needs_user")
        self.assertEqual(value["turns"][0]["status"], "inProgress")
        self.store._db.execute("UPDATE turns SET completed_at=NULL WHERE id='old-turn'")
        self._bind_turn("new-command", "new-turn", "new task")
        value = self.store.reconcile_thread("conversation-1", {"thread": {
            "id": "thread-1", "turns": [
                {"id": "old-turn", "status": "inProgress", "items": []},
                {"id": "new-turn", "status": "inProgress", "items": []},
            ],
        }})
        self.assertEqual(value["conversation"]["state"], "needs_user")
        self.assertEqual(value["conversation"]["recovery_state"], "ambiguous_native_state")
        self.assertIsNone(value["conversation"]["active_turn_id"])

    def test_reconcile_duplicate_turns_are_not_later_turn_evidence(self):
        self._bind_turn("old-command", "old-turn", "old task")
        self._complete("old-turn", "answer")
        self.store._db.execute("UPDATE turns SET status='inProgress' WHERE id='old-turn'")
        before = self.store.snapshot("conversation-1")
        with self.assertRaises(AppServerProtocolError):
            self.store.reconcile_thread("conversation-1", {"thread": {
                "id": "thread-1", "turns": [
                    {"id": "old-turn", "status": "inProgress", "items": []},
                    {"id": "old-turn", "status": "inProgress", "items": []},
                ],
            }})
        self.assertEqual(self.store.snapshot("conversation-1"), before)

    def test_reconcile_checkpoint_preserves_live_item_and_turn_updates(self):
        self._bind_turn("message-1", "turn-1", "hello")
        self.store.apply_notification({
            "method": "item/started",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "id": "answer-1", "type": "agentMessage", "text": "old",
            }},
        })
        checkpoint = self.store.capture_reconcile_checkpoint("conversation-1")
        self.store.apply_notification({
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "id": "answer-1", "type": "agentMessage", "text": "new",
                "phase": "final_answer",
            }},
        })
        self.store.apply_notification({
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "id": "answer-2", "type": "agentMessage", "text": "new arrival",
            }},
        })
        self.store.apply_notification({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {
                "id": "turn-1", "status": "completed", "items": [],
            }},
        })
        self.store.apply_notification({
            "method": "thread/status/changed",
            "params": {"threadId": "thread-1", "status": {"type": "idle"}},
        })

        value = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "active"},
                "turns": [{"id": "turn-1", "status": "inProgress", "items": [
                    {"id": "answer-1", "type": "agentMessage", "text": "old"},
                    {"id": "answer-2", "type": "agentMessage", "text": "old arrival"},
                ]}],
            },
        }, checkpoint=checkpoint)

        self.assertEqual(value["turns"][0]["status"], "completed")
        items = {item["native_item_id"]: item for item in value["turns"][0]["items"]}
        self.assertEqual(items["answer-1"]["text"], "new")
        self.assertEqual(items["answer-2"]["text"], "new arrival")
        self.assertEqual(value["conversation"]["thread_status"], {"type": "idle"})
        self.assertEqual(value["conversation"]["state"], "idle")

    def test_reconcile_checkpoint_preserves_delta_and_new_live_turn(self):
        self._bind_turn("message-1", "turn-1", "hello")
        self.store.apply_notification({
            "method": "item/started",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {
                "id": "answer-1", "type": "agentMessage", "text": "old",
            }},
        })
        checkpoint = self.store.capture_reconcile_checkpoint("conversation-1")
        self.store.apply_notification({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "answer-1", "delta": "-delta",
            },
        })
        self.store.apply_notification({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {
                "id": "turn-1", "status": "completed", "items": [],
            }},
        })
        self._bind_turn("message-2", "turn-2", "second")

        value = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "active"},
                "turns": [{"id": "turn-1", "status": "inProgress", "items": [{
                    "id": "answer-1", "type": "agentMessage", "text": "old",
                }]}],
            },
        }, checkpoint=checkpoint)

        turn_one = next(turn for turn in value["turns"] if turn["id"] == "turn-1")
        self.assertEqual(turn_one["items"][0]["text"], "old-delta")
        self.assertEqual(value["conversation"]["active_turn_id"], "turn-2")
        self.assertEqual(value["conversation"]["state"], "active")

    def test_reconcile_checkpoint_preserves_new_terminal_error(self):
        self._bind_turn('message-1', 'turn-1', 'hello')
        checkpoint = self.store.capture_reconcile_checkpoint('conversation-1')
        error = {'message': 'live failure'}
        self.store.apply_notification({
            'method': 'turn/completed',
            'params': {'threadId': 'thread-1', 'turn': {
                'id': 'turn-1', 'status': 'failed', 'items': [], 'error': error,
            }},
        })
        value = self.store.reconcile_thread('conversation-1', {'thread': {
            'id': 'thread-1', 'turns': [
                {'id': 'turn-1', 'status': 'inProgress', 'items': []},
            ],
        }}, checkpoint=checkpoint)
        self.assertEqual(value['turns'][0]['status'], 'failed')
        self.assertEqual(value['turns'][0]['error'], error)

    def test_delta_baseline_recovers_offline_gap_after_existing_full_item(self):
        self._bind_turn('message-1', 'turn-1', 'hello')
        for index, source in enumerate(('notification_item', 'thread_read')):
            with self.subTest(source=source):
                item_id = 'answer-gap-' + str(index)
                cached = {'id': item_id, 'type': 'agentMessage', 'text': 'prefix'}
                snapshot = {'thread': {'id': 'thread-1', 'turns': [{
                    'id': 'turn-1', 'status': 'inProgress', 'items': [cached],
                }]}}
                if source == 'thread_read':
                    self.store.reconcile_thread('conversation-1', snapshot)
                else:
                    self.store.apply_notification({'method': 'item/started', 'params': {
                        'threadId': 'thread-1', 'turnId': 'turn-1', 'item': cached,
                    }})
                checkpoint = self.store.capture_reconcile_checkpoint('conversation-1')
                self.store.apply_notification({'method': 'item/agentMessage/delta', 'params': {
                    'threadId': 'thread-1', 'turnId': 'turn-1',
                    'itemId': item_id, 'delta': ' suffix',
                }})
                full = dict(cached, text='prefix missed middle suffix', phase='final_answer')
                snapshot['thread']['turns'][0]['items'] = [full]
                candidates = self.store.delta_baseline_candidates(
                    'conversation-1', snapshot, checkpoint,
                )
                self.assertEqual(len(candidates), 1)
                self.assertTrue(self.store.hydrate_delta_baseline(
                    'conversation-1', 'turn-1', item_id,
                    expected_revision=candidates[0]['revision'], fresh_native_item=full,
                ))
                value = self.store.reconcile_thread('conversation-1', snapshot, checkpoint=checkpoint)
                item = next(i for i in value['turns'][0]['items'] if i['native_item_id'] == item_id)
                self.assertEqual(item['text'], full['text'])
                self.assertEqual(item['phase'], 'final_answer')

    def test_delta_baseline_keeps_explicit_native_tool_status(self):
        self._bind_turn('message-1', 'turn-1', 'hello')
        self.store.apply_notification({
            'method': 'item/commandExecution/outputDelta',
            'params': {'threadId': 'thread-1', 'turnId': 'turn-1',
                       'itemId': 'command-one', 'delta': 'suffix'},
        })
        checkpoint = self.store.capture_reconcile_checkpoint('conversation-1')
        revision = checkpoint['items'][('turn-1', 'command-one', 'command_execution')]
        self.assertTrue(self.store.hydrate_delta_baseline(
            'conversation-1', 'turn-1', 'command-one', expected_revision=revision,
            fresh_native_item={'id': 'command-one', 'type': 'commandExecution',
                               'status': 'failed', 'aggregatedOutput': 'prefix suffix'},
        ))
        value = self.store.snapshot('conversation-1')
        item = next(i for i in value['turns'][0]['items'] if i['native_item_id'] == 'command-one')
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['detail'], 'prefix suffix')

    def test_reconcile_checkpoint_still_imports_unchanged_history(self):
        checkpoint = self.store.capture_reconcile_checkpoint("conversation-1")
        value = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "idle"},
                "turns": [{"id": "turn-1", "status": "completed", "items": [{
                    "id": "answer-1", "type": "agentMessage", "text": "history",
                    "phase": "final_answer",
                }]}],
            },
        }, checkpoint=checkpoint)
        self.assertEqual(value["turns"][0]["items"][0]["text"], "history")
        self.assertEqual(value["turns"][0]["status"], "completed")

    def test_items_record_their_projection_source(self):
        self._bind_turn("message-1", "turn-1", "hello")
        self.store.apply_notification({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "msg_1", "delta": "hello",
            },
        })
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {"id": "item-1", "type": "agentMessage", "text": "done"},
            },
        })
        reconciled = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "active"},
                "turns": [{"id": "turn-1", "status": "inProgress", "items": [{
                    "id": "item-2", "type": "agentMessage", "text": "again",
                }]}],
            }
        })
        self.assertEqual(
            [item["source"] for item in reconciled["turns"][0]["items"]],
            ["notification_delta", "notification_item", "thread_read"],
        )

    def test_context_compaction_counts_only_live_completed_items_once(self):
        self._bind_turn(
            "compact-command", "compact-turn", "compact", kind="compact"
        )
        started = {
            "method": "item/started",
            "params": {
                "threadId": "thread-1", "turnId": "compact-turn",
                "item": {"id": "compaction-1", "type": "contextCompaction"},
            },
        }
        completed = {
            **started,
            "method": "item/completed",
        }
        self.store.apply_notification(started)
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_completed_count"],
            0,
        )
        self.store.apply_notification(completed)
        self.store.apply_notification(completed)
        self.store.apply_notification(started)
        completed_first = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "compact-turn",
                "item": {"id": "compaction-2", "type": "contextCompaction"},
            },
        }
        self.store.apply_notification(completed_first)
        self.store.apply_notification({**completed_first, "method": "item/started"})
        snapshot = self.store.snapshot("conversation-1")
        self.assertEqual(
            snapshot["conversation"]["context_compaction_completed_count"], 2
        )
        self.assertEqual(
            snapshot["conversation"]["context_compaction_manual_completed_count"], 2
        )
        self.assertEqual(
            snapshot["conversation"]["context_compaction_other_completed_count"], 0
        )

    def test_context_compaction_splits_mixed_sources_and_survives_reopen(self):
        self._bind_turn("model-command", "model-turn", "continue")
        model_item = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "model-turn",
                "item": {"id": "model-compaction", "type": "contextCompaction"},
            },
        }
        self.store.apply_notification(model_item)
        self._complete("model-turn", "done")
        self._bind_turn("manual-command", "manual-turn", "compact", kind="compact")
        self.store.apply_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "manual-turn",
                "item": {"id": "manual-compaction", "type": "contextCompaction"},
            },
        })
        self.store.apply_notification({
            "method": "item/started",
            "params": {
                "threadId": "thread-1", "turnId": "manual-turn",
                "item": {"id": "unfinished-compaction", "type": "contextCompaction"},
            },
        })
        self.store.close()
        self.store = ConversationStore(self.root / "conversations.sqlite3")
        self.store.apply_notification(model_item)
        value = self.store.snapshot("conversation-1")["conversation"]
        self.assertEqual(value["context_compaction_completed_count"], 2)
        self.assertEqual(value["context_compaction_manual_completed_count"], 1)
        self.assertEqual(value["context_compaction_other_completed_count"], 1)
        self.assertEqual(value["context_switch_count"], 0)

    def test_context_compaction_does_not_count_failed_or_reconciled_history(self):
        self._bind_turn(
            "compact-command", "compact-turn", "compact", kind="compact"
        )
        self.store.apply_notification({
            "method": "item/started",
            "params": {
                "threadId": "thread-1", "turnId": "compact-turn",
                "item": {"id": "compaction-live", "type": "contextCompaction"},
            },
        })
        self._complete("compact-turn", "", status="failed")
        reconciled = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1", "status": {"type": "idle"},
                "turns": [
                    {"id": "compact-turn", "status": "failed", "items": []},
                    {
                        "id": "historical-turn", "status": "completed", "items": [{
                            "id": "historical-compaction", "type": "contextCompaction",
                        }],
                    },
                ],
            },
        })
        self.assertEqual(
            reconciled["conversation"]["context_compaction_completed_count"], 0
        )
        self.assertEqual(
            reconciled["conversation"]["context_compaction_manual_completed_count"], 0
        )
        self.assertEqual(
            reconciled["conversation"]["context_compaction_other_completed_count"], 0
        )

    def test_context_switch_keeps_only_minimum_lifecycle_projection(self):
        self._bind_turn("message-command", "message-turn", "continue")
        call_hash = "a" * 64
        observed = {
            "method": "internal/context-switch/observed",
            "params": {
                "threadId": "thread-1", "turnId": "message-turn",
                "callKeySha256": call_hash, "state": "call_seen",
                "serverVersion": "0.153.1",
            },
        }
        self.assertTrue(self.store.apply_notification(observed)["changed"])
        # An associated native contextCompaction belongs to the model turn,
        # not the user's compact button command.
        other_compaction = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1", "turnId": "message-turn",
                "item": {"id": "switch-compaction", "type": "contextCompaction"},
            },
        }
        self.store.apply_notification(other_compaction)
        self.store.apply_notification(other_compaction)
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_completed_count"], 1,
        )
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_manual_completed_count"], 0,
        )
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_other_completed_count"], 1,
        )
        completed = {
            **observed,
            "params": {**observed["params"], "state": "output_seen"},
        }
        self.assertTrue(self.store.apply_notification(completed)["changed"])
        self.assertFalse(self.store.apply_notification(completed)["changed"])
        snapshot = self.store.snapshot("conversation-1")
        self.assertEqual(snapshot["conversation"]["context_switch_count"], 1)
        self.assertEqual(len(snapshot["conversation"]["context_switches"]), 1)
        self.assertEqual(
            snapshot["conversation"]["context_switches"][0]["state"], "output_seen"
        )
        self.assertNotIn(call_hash, json.dumps(snapshot))
        columns = {
            row["name"] for row in self.store._db.execute(
                "PRAGMA table_info(context_switch_events)"
            ).fetchall()
        }
        self.assertEqual(
            columns,
            {
                "conversation_id", "turn_id", "call_key_sha256",
                "call_observed_at", "output_observed_at", "server_version",
            },
        )

    def test_context_switch_snapshot_has_total_and_bounded_recent_details(self):
        for index in range(21):
            turn_id = f"switch-turn-{index}"
            self._bind_turn(f"switch-message-{index}", turn_id, "continue")
            self.store.apply_notification({
                "method": "internal/context-switch/observed",
                "params": {
                    "threadId": "thread-1", "turnId": turn_id,
                    "callKeySha256": f"{index:064x}", "state": "call_seen",
                    "serverVersion": "0.153.1",
                },
            })
        snapshot = self.store.snapshot("conversation-1")["conversation"]
        self.assertEqual(snapshot["context_switch_count"], 21)
        self.assertEqual(len(snapshot["context_switches"]), 20)

    def test_context_compaction_state_tracks_latest_compact_command(self):
        self.store.begin_command("conversation-1", "compact-1", kind="compact")
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_state"],
            "busy",
        )
        self.store.update_command("compact-1", "rejected", reason="rejected")
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_state"],
            "failed",
        )
        self.store.begin_command("conversation-1", "compact-2", kind="compact")
        self.store.update_command("compact-2", "completed")
        self.assertEqual(
            self.store.snapshot("conversation-1")["conversation"]
            ["context_compaction_state"],
            "idle",
        )

    def test_replayed_old_terminal_cannot_clear_new_active_turn(self):
        self._bind_turn("message-1", "turn-1", "first")
        self._complete("turn-1", "one")
        self._bind_turn("message-2", "turn-2", "second")

        replay = self.store.apply_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [],
                },
            },
        })

        self.assertFalse(replay["changed"])
        conversation = self.store.get_conversation("conversation-1")
        self.assertEqual(conversation["state"], "active")
        self.assertEqual(conversation["active_turn_id"], "turn-2")

    def test_browser_command_is_durable_and_idempotent(self):
        first, created = self.store.begin_command(
            "conversation-1", "message-1", kind="steer", text="same"
        )
        replay, replay_created = self.store.begin_command(
            "conversation-1", "message-1", kind="steer", text="same"
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(AppServerProtocolError, "reused"):
            self.store.begin_command(
                "conversation-1", "message-1", kind="steer", text="changed"
            )

    def test_reconcile_uses_native_turn_status_without_replaying(self):
        self._bind_turn("message-1", "turn-1", "hello")
        active = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1",
                "status": {"type": "active"},
                "turns": [{"id": "turn-1", "status": "inProgress", "items": []}],
            }
        })
        self.assertEqual(active["conversation"]["state"], "active")
        self.assertEqual(active["conversation"]["active_turn_id"], "turn-1")

        terminal = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1",
                "status": {"type": "idle"},
                "turns": [{
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "done",
                    }],
                }],
            }
        })
        self.assertEqual(terminal["conversation"]["state"], "idle")
        self.assertEqual(terminal["turns"][0]["final_text"], "done")

    def test_unknown_delivery_stays_needs_user_after_idle_reconcile(self):
        self.store.begin_command(
            "conversation-1", "message-unknown", kind="start", text="maybe"
        )
        self.store.update_command("message-unknown", "delivery_unknown")
        snapshot = self.store.reconcile_thread("conversation-1", {
            "thread": {"id": "thread-1", "status": {"type": "idle"}, "turns": []}
        })
        self.assertEqual(snapshot["conversation"]["state"], "needs_user")
        self.assertEqual(
            snapshot["conversation"]["recovery_state"], "ambiguous_native_state"
        )

    def test_unknown_native_turn_status_fails_closed(self):
        self._bind_turn("message-1", "turn-1", "hello")
        snapshot = self.store.reconcile_thread("conversation-1", {
            "thread": {
                "id": "thread-1",
                "status": {"type": "idle"},
                "turns": [{"id": "turn-1", "status": "futureStatus", "items": []}],
            }
        })

        self.assertEqual(snapshot["conversation"]["state"], "needs_user")
        self.assertIsNone(snapshot["conversation"]["active_turn_id"])
        self.assertEqual(
            snapshot["conversation"]["recovery_state"], "unknown_native_turn_status"
        )

    def test_tool_call_replays_exact_saved_response(self):
        self._bind_turn("message-1", "turn-1", "hello")
        args = {"path": "/tmp/result.png"}
        self.assertIsNone(self.store.claim_tool_call(
            "conversation-1", "turn-1", "call-1", "publish_artifact", args
        ))
        response = {"contentItems": [{"type": "input_text", "text": "ok"}], "success": True}
        self.store.complete_tool_call("call-1", response)
        replay = self.store.claim_tool_call(
            "conversation-1", "turn-1", "call-1", "publish_artifact", args
        )
        self.assertEqual(replay, response)
        with self.assertRaisesRegex(AppServerProtocolError, "reused"):
            self.store.claim_tool_call(
                "conversation-1",
                "turn-1",
                "call-1",
                "publish_artifact",
                {"path": "/tmp/other.png"},
            )

    def test_unconfirmed_review_cleanup_is_durable_and_clearable(self):
        self._bind_turn("message-1", "turn-1", "review")
        self.store.claim_tool_call(
            "conversation-1", "turn-1", "review-call", "code_review", {"scope": "test"},
        )
        self.store.complete_tool_call("review-call", {
            "success": False,
            "review_cleanup": {
                "status": "unconfirmed", "review_thread_id": "review-thread",
                "review_turn_id": "review-turn",
            },
            "review_lifecycle": {
                "phase": "turn_started", "review_thread_id": "review-thread",
                "review_turn_id": "review-turn",
            },
        })
        self.assertEqual(self.store.pending_review_cleanups(), [{
            "call_id": "review-call", "conversation_id": "conversation-1", "turn_id": "turn-1",
            "review_thread_id": "review-thread", "review_turn_id": "review-turn", "phase": "turn_started",
        }])
        self.store.clear_review_cleanup("review-call")
        self.assertEqual(self.store.pending_review_cleanups(), [])

    def test_review_reserved_lifecycle_survives_inprogress_crash_window(self):
        self._bind_turn("message-1", "turn-1", "review")
        self.store.claim_tool_call(
            "conversation-1", "turn-1", "reserved-call", "code_review", {"scope": "test"},
        )
        self.store.set_review_lifecycle("reserved-call", {"phase": "reserved"})
        self.assertEqual(self.store.pending_review_cleanups(), [{
            "call_id": "reserved-call", "conversation_id": "conversation-1", "turn_id": "turn-1",
            "review_thread_id": None, "review_turn_id": None, "phase": "reserved",
        }])

    def test_tool_call_snapshot_index_and_malformed_response_are_safe(self):
        indexes = {
            row[1]
            for row in self.store._db.execute("PRAGMA index_list(tool_calls)")
        }
        self.assertIn("tool_calls_conversation_idx", indexes)

        self._bind_turn("message-1", "turn-1", "hello")
        self.store.claim_tool_call(
            "conversation-1", "turn-1", "call-malformed",
            "publish_artifact", {"path": "/tmp/result.png"},
        )
        self.store.complete_tool_call("call-malformed", {"success": False})
        self.store._db.execute(
            "UPDATE tool_calls SET response_json = '[]' WHERE call_id = ?",
            ("call-malformed",),
        )
        snapshot = self.store.snapshot("conversation-1")
        self.assertFalse(snapshot["turns"][0]["artifact_publish_failed"])

    def test_terminal_generation_keeps_per_account_unread_until_marked(self):
        self._bind_turn("message-1", "turn-1", "first")
        completed = self._complete("turn-1", "done")
        self.assertTrue(completed["terminal"])

        conversation = self.store.get_conversation("conversation-1")
        self.assertEqual(conversation["terminal_generation"], 1)
        self.assertEqual(conversation["last_terminal_turn_id"], "turn-1")
        self.assertEqual(
            conversation["last_terminal_at"],
            self.store.snapshot("conversation-1")["turns"][0]["completed_at"],
        )
        self.assertTrue(
            self.store.list_conversations(account_id="account-a")[0]["unread"]
        )
        self.assertTrue(
            self.store.list_conversations(account_id="account-b")[0]["unread"]
        )

        self.store.mark_read("conversation-1", "account-a")
        self.assertFalse(
            self.store.list_conversations(account_id="account-a")[0]["unread"]
        )
        self.assertTrue(
            self.store.list_conversations(account_id="account-b")[0]["unread"]
        )

        replay = self._complete("turn-1", "done")
        self.assertFalse(replay.get("terminal", False))
        self.assertEqual(
            self.store.get_conversation("conversation-1")["terminal_generation"], 1
        )
        self.assertFalse(
            self.store.list_conversations(account_id="account-a")[0]["unread"]
        )

        self._bind_turn("message-2", "turn-2", "second")
        self._complete("turn-2", "done again")
        self.assertEqual(
            self.store.get_conversation("conversation-1")["terminal_generation"], 2
        )
        self.assertTrue(
            self.store.list_conversations(account_id="account-a")[0]["unread"]
        )

    def test_list_order_uses_activity_instead_of_projection_updates(self):
        self.store.create_conversation(
            "conversation-2",
            title="second",
            cwd="/tmp",
            settings={"model": "fixture", "permission": "read-only"},
        )
        self.store.bind_thread(
            "conversation-2", {"id": "thread-2"}, server_version="fixture"
        )
        self.store._db.execute(
            "UPDATE conversations SET activity_at = ?, updated_at = ? WHERE id = ?",
            ("2026-08-26T03:00:00+00:00", "2026-08-26T01:00:00+00:00", "conversation-1"),
        )
        self.store._db.execute(
            "UPDATE conversations SET activity_at = ?, updated_at = ? WHERE id = ?",
            ("2026-08-26T02:00:00+00:00", "2026-08-26T04:00:00+00:00", "conversation-2"),
        )
        self.assertEqual(
            [value["id"] for value in self.store.list_conversations()],
            ["conversation-1", "conversation-2"],
        )

        self.store.update_conversation_title("conversation-2", "renamed later")
        self.assertEqual(
            [value["id"] for value in self.store.list_conversations()],
            ["conversation-1", "conversation-2"],
        )
        self.store.set_pinned(["conversation-2"], True)
        self.assertEqual(self.store.list_conversations()[0]["id"], "conversation-2")

    def test_database_and_parent_are_private(self):
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


class ConversationStoreMigrationTests(unittest.TestCase):
    def test_v6_database_adds_async_projection_columns_without_rebuild(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            store = ConversationStore(path)
            store.close()

            db = sqlite3.connect(path)
            # This fixture represents a v6 database which predates the
            # additive local-search extension.  Remove the new derived
            # projection first; otherwise its async-delivery trigger quite
            # correctly prevents SQLite from dropping the old column.
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'conversation_search%'"
            ):
                db.execute("DROP TRIGGER " + name)
            db.execute("DROP TABLE conversation_search_fts")
            db.execute("DROP TABLE conversation_search_documents")
            db.execute("DROP TABLE conversation_search_fallback_provenance")
            db.execute("DROP TABLE conversation_search_item_sources")
            db.execute(
                "DELETE FROM schema_meta WHERE key = 'conversation_search_version'"
            )
            db.execute("ALTER TABLE items DROP COLUMN delivery")
            db.execute("ALTER TABLE items DROP COLUMN questions_json")
            db.execute(
                "DELETE FROM schema_meta "
                "WHERE key = 'async_user_input_schema_version'"
            )
            db.commit()
            db.close()

            migrated = ConversationStore(path)
            try:
                columns = {
                    row["name"] for row in migrated._db.execute(
                        "PRAGMA table_info(items)"
                    )
                }
                self.assertTrue({"delivery", "questions_json"}.issubset(columns))
                self.assertEqual(
                    migrated._db.execute(
                        "SELECT value FROM schema_meta "
                        "WHERE key = 'async_user_input_schema_version'"
                    ).fetchone()["value"],
                    "1",
                )
                self.assertEqual(
                    migrated._db.execute(
                        "SELECT value FROM schema_meta "
                        "WHERE key = 'conversation_search_version'"
                    ).fetchone()["value"],
                    "1",
                )
            finally:
                migrated.close()

    def test_v5_migration_starts_compaction_count_without_backfill(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            store = ConversationStore(path)
            store.create_conversation(
                "conversation-old", title="old", cwd="/tmp", settings={}
            )
            store.bind_thread(
                "conversation-old", {"id": "thread-old"}, server_version="fixture"
            )
            store.begin_command(
                "conversation-old", "command-old", kind="start", text="old"
            )
            store.bind_turn(
                "conversation-old", {"id": "turn-old", "status": "inProgress"},
                command_id="command-old",
            )
            store.apply_notification({
                "method": "item/completed",
                "params": {
                    "threadId": "thread-old", "turnId": "turn-old",
                    "item": {"id": "compaction-old", "type": "contextCompaction"},
                },
            })
            store.close()

            db = sqlite3.connect(path)
            db.execute("DROP TABLE context_compaction_events")
            db.execute("DROP TABLE context_switch_events")
            db.execute(
                "DELETE FROM schema_meta WHERE key = 'context_switch_schema_version'"
            )
            db.execute(
                "UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'"
            )
            db.commit()
            db.close()

            migrated = ConversationStore(path)
            try:
                self.assertEqual(
                    migrated._db.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()["value"],
                    str(SCHEMA_VERSION),
                )
                self.assertEqual(
                    migrated.snapshot("conversation-old")["conversation"]
                    ["context_compaction_completed_count"],
                    0,
                )
                self.assertEqual(
                    migrated.snapshot("conversation-old")["conversation"]
                    ["context_compaction_manual_completed_count"],
                    0,
                )
                self.assertEqual(
                    migrated.snapshot("conversation-old")["conversation"]
                    ["context_compaction_other_completed_count"],
                    0,
                )
                self.assertEqual(
                    migrated._db.execute(
                        "SELECT COUNT(*) FROM context_compaction_events"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    migrated._db.execute(
                        "SELECT COUNT(*) FROM context_switch_events"
                    ).fetchone()[0],
                    0,
                )
            finally:
                migrated.close()

    def test_v4_migration_does_not_make_historical_null_turns_pending(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            store = ConversationStore(path)
            store.create_conversation(
                "conversation-old",
                title="old",
                cwd="/tmp",
                settings={},
                completion_notification=True,
            )
            store._db.executemany(
                """
                INSERT INTO turns(
                    id, conversation_id, ordinal, status, completed_at,
                    notification_requested, notification_state
                ) VALUES(?, 'conversation-old', ?, 'completed', ?, ?, ?)
                """,
                [
                    ("turn-historical", 1, "2026-08-27T01:00:00+00:00", 1, None),
                    ("turn-sending", 2, "2026-08-27T02:00:00+00:00", 1, "sending"),
                    ("turn-sent", 3, "2026-08-27T03:00:00+00:00", 1, "sent"),
                ],
            )
            store._db.execute(
                """
                INSERT INTO turns(
                    id, conversation_id, ordinal, status,
                    notification_requested
                ) VALUES('turn-active', 'conversation-old', 4, 'inProgress', 1)
                """
            )
            store.close()

            db = sqlite3.connect(path)
            db.execute("ALTER TABLE turns DROP COLUMN notification_requested")
            db.execute(
                "UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'"
            )
            db.commit()
            db.close()

            migrated = ConversationStore(path)
            try:
                requested = {
                    row["id"]: row["notification_requested"]
                    for row in migrated._db.execute(
                        "SELECT id, notification_requested FROM turns"
                    )
                }
                self.assertEqual(requested["turn-historical"], 0)
                self.assertEqual(requested["turn-sending"], 1)
                self.assertEqual(requested["turn-sent"], 1)
                self.assertEqual(requested["turn-active"], 1)
                migrated.release_incomplete_notifications()
                self.assertEqual(
                    migrated.pending_completion_notifications(),
                    [],
                )
            finally:
                migrated.close()

    def test_v2_migration_adds_usage_and_item_source(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value)
                    VALUES('schema_version', '2');
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    active_turn_id TEXT,
                    thread_status TEXT,
                    settings_json TEXT NOT NULL,
                    recovery_state TEXT,
                    connection_state TEXT NOT NULL,
                    server_version TEXT,
                    last_error_json TEXT,
                    completion_notification INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    client_origin TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE items (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    native_item_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT,
                    text TEXT,
                    detail TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(turn_id, native_item_id, kind)
                );
                """
            )
            db.close()
            store = ConversationStore(path)
            try:
                self.assertEqual(
                    store._db.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()["value"],
                    str(SCHEMA_VERSION),
                )
                self.assertIn(
                    "token_usage_json",
                    {row["name"] for row in store._db.execute(
                        "PRAGMA table_info(conversations)"
                    )},
                )
                self.assertIn(
                    "source",
                    {row["name"] for row in store._db.execute(
                        "PRAGMA table_info(items)"
                    )},
                )
                conversation_columns = {
                    row["name"] for row in store._db.execute(
                        "PRAGMA table_info(conversations)"
                    )
                }
                self.assertIn("activity_at", conversation_columns)
                self.assertIn("terminal_generation", conversation_columns)
                self.assertIsNotNone(store._db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'conversation_reads'"
                ).fetchone())
            finally:
                store.close()

    def test_half_applied_v1_migration_recovers_idempotently(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value)
                    VALUES('schema_version', '1');
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    active_turn_id TEXT,
                    thread_status TEXT,
                    settings_json TEXT NOT NULL,
                    recovery_state TEXT,
                    connection_state TEXT NOT NULL,
                    server_version TEXT,
                    last_error_json TEXT,
                    completion_notification INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    client_origin TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'model',
                    started_at TEXT,
                    completed_at TEXT,
                    final_text TEXT,
                    error_json TEXT,
                    UNIQUE(conversation_id, ordinal)
                );
                """
            )
            db.close()

            store = ConversationStore(path)
            store.close()
            reopened = ConversationStore(path)
            try:
                version = reopened._db.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
                conversation_columns = {
                    row["name"]
                    for row in reopened._db.execute(
                        "PRAGMA table_info(conversations)"
                    )
                }
                turn_columns = {
                    row["name"]
                    for row in reopened._db.execute("PRAGMA table_info(turns)")
                }
                self.assertEqual(version, str(SCHEMA_VERSION))
                self.assertIn("sort_order", conversation_columns)
                self.assertIn("token_usage_json", conversation_columns)
                self.assertIn("activity_at", conversation_columns)
                self.assertIn("terminal_generation", conversation_columns)
                self.assertIn("last_terminal_turn_id", conversation_columns)
                self.assertIn("last_terminal_at", conversation_columns)
                self.assertIn("notification_state", turn_columns)
                self.assertIn("notification_detail", turn_columns)
                self.assertIn("notification_requested", turn_columns)
                item_columns = {
                    row["name"]
                    for row in reopened._db.execute("PRAGMA table_info(items)")
                }
                self.assertIn("source", item_columns)
            finally:
                reopened.close()

    def test_v1_migration_failure_rolls_back_every_alter_and_can_retry(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            db = sqlite3.connect(
                path,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            db.executescript(
                """
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value)
                    VALUES('schema_version', '1');
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    active_turn_id TEXT,
                    thread_status TEXT,
                    settings_json TEXT NOT NULL,
                    recovery_state TEXT,
                    connection_state TEXT NOT NULL,
                    server_version TEXT,
                    last_error_json TEXT,
                    completion_notification INTEGER NOT NULL DEFAULT 0,
                    client_origin TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'model',
                    started_at TEXT,
                    completed_at TEXT,
                    final_text TEXT,
                    error_json TEXT,
                    UNIQUE(conversation_id, ordinal)
                );
                """
            )

            def deny_turn_alter(action, first, second, database, source):
                if action == sqlite3.SQLITE_ALTER_TABLE and second == "turns":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(deny_turn_alter)
            with mock.patch(
                "engine.conversation_store.sqlite3.connect", return_value=db
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    ConversationStore(path)

            check = sqlite3.connect(path)
            try:
                version = check.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                conversation_columns = {
                    row[1] for row in check.execute(
                        "PRAGMA table_info(conversations)"
                    )
                }
                self.assertEqual(version, "1")
                self.assertNotIn("pinned", conversation_columns)
                self.assertNotIn("sort_order", conversation_columns)
            finally:
                check.close()

            recovered = ConversationStore(path)
            try:
                self.assertEqual(
                    recovered._db.execute(
                        "SELECT value FROM schema_meta "
                        "WHERE key = 'schema_version'"
                    ).fetchone()["value"],
                    str(SCHEMA_VERSION),
                )
            finally:
                recovered.close()

    def test_v3_migration_backfills_semantic_activity_without_old_unread(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root_value:
            path = Path(root_value) / "conversations.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value)
                    VALUES('schema_version', '3');
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    active_turn_id TEXT,
                    thread_status TEXT,
                    settings_json TEXT NOT NULL,
                    token_usage_json TEXT,
                    recovery_state TEXT,
                    connection_state TEXT NOT NULL,
                    server_version TEXT,
                    last_error_json TEXT,
                    completion_notification INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    client_origin TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'model',
                    started_at TEXT,
                    completed_at TEXT,
                    final_text TEXT,
                    error_json TEXT,
                    notification_state TEXT,
                    notification_detail TEXT,
                    UNIQUE(conversation_id, ordinal)
                );
                INSERT INTO conversations(
                    id, thread_id, title, cwd, state, settings_json,
                    connection_state, created_at, updated_at
                ) VALUES(
                    'conversation-old', 'thread-old', 'old', '/tmp', 'idle', '{}',
                    'connected', '2026-08-26T00:00:00+00:00',
                    '2026-08-26T09:50:00+00:00'
                );
                INSERT INTO turns(
                    id, conversation_id, ordinal, status, started_at, completed_at
                ) VALUES(
                    'turn-old', 'conversation-old', 1, 'completed',
                    '2026-08-26T01:00:00+00:00',
                    '2026-08-26T01:23:00+00:00'
                );
                """
            )
            db.close()

            store = ConversationStore(path)
            try:
                conversation = store.get_conversation("conversation-old")
                self.assertEqual(conversation["activity_at"], "2026-08-26T01:23:00+00:00")
                self.assertEqual(conversation["last_terminal_turn_id"], "turn-old")
                self.assertEqual(conversation["last_terminal_at"], "2026-08-26T01:23:00+00:00")
                self.assertEqual(conversation["terminal_generation"], 0)
                self.assertFalse(
                    store.list_conversations(account_id="account-a")[0]["unread"]
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
