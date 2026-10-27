import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.conversation_automation import (
    AutomationScheduler,
    AutomationStore,
    next_due,
    validate_schedule,
)
from engine.conversation_store import ConversationStore


class AutomationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = Path(self.temp.name) / "automations.sqlite3"
        self.store = ConversationStore(self.path)
        self.store.create_conversation(
            "conversation-1", title="test", cwd="/tmp", owner_account_id="owner-1",
            settings={"permission": "danger-full-access"},
        )
        self.store.bind_thread("conversation-1", {"id": "thread-1"}, server_version="test")
        self.automations = AutomationStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, **extra):
        values = {"title": "每小时检查", "prompt": "检查状态", "schedule": {"kind": "interval", "interval_minutes": 60}}
        values.update(extra)
        return self.automations.create("conversation-1", "owner-1", **values)

    def make_due(self, task_id, when=None):
        when = when or datetime.now(timezone.utc)
        self.store._db.execute(
            "UPDATE conversation_automation_tasks SET next_run_at=? WHERE id=?",
            (when.isoformat().replace("+00:00", "Z"), task_id),
        )
        return when

    def test_unknown_schema_is_rejected_without_rewriting(self):
        self.store._db.execute("UPDATE schema_meta SET value='99' WHERE key='automation_schema_version'")
        with self.assertRaises(ValueError):AutomationStore(self.store)
        self.assertEqual(self.store._db.execute("SELECT value FROM schema_meta WHERE key='automation_schema_version'").fetchone()[0],'99')

    def test_schedule_timezone_validation_and_once(self):
        self.assertEqual(
            validate_schedule({"type": "daily", "time": "08:05"}),
            {"kind": "daily", "time": "08:05", "timezone": "Asia/Hong_Kong"},
        )
        after = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(
            next_due({"kind": "once", "run_at": "2026-01-01T00:01:00+00:00"}, after),
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )
        for value in ({"kind": "interval", "interval_minutes": True}, {"kind": "weekly", "time": "10:00", "weekdays": []}, {"kind": "daily", "time": "25:00"}):
            with self.assertRaises(ValueError):
                validate_schedule(value)

    def test_create_owner_thread_limits_and_public_does_not_leak_cwd(self):
        task = self.create()
        self.assertNotIn("cwd", task)
        with self.assertRaises(ValueError):
            self.automations.create("conversation-1", "other", title="x", prompt="x", schedule={"kind": "interval", "interval_minutes": 1})
        self.store.create_conversation("draft", title="draft", cwd="/tmp", owner_account_id="owner-1", settings={"permission": "danger-full-access"})
        with self.assertRaises(ValueError):
            self.automations.create("draft", "owner-1", title="x", prompt="x", schedule={"kind": "interval", "interval_minutes": 1})

    def test_once_claims_once_and_busy_task_does_not_backlog(self):
        task = self.create(schedule={"kind": "once", "run_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()})
        due = datetime.fromisoformat(task["next_run_at"].replace("Z", "+00:00"))
        first = self.automations.claim_due(due)
        self.assertEqual(len(first), 1)
        self.assertIsNone(self.automations.get(task["id"], "owner-1")["next_run_at"])
        # A second interval slot while the first one is in flight is journaled,
        # never sent as catch-up work.
        repeating = self.create()
        later = self.make_due(repeating["id"], due + timedelta(seconds=1))
        self.assertEqual(len(self.automations.claim_due(later)), 1)
        future = self.make_due(repeating["id"], later + timedelta(seconds=1))
        self.assertEqual(self.automations.claim_due(future), [])
        self.assertEqual(self.automations.runs(repeating["id"], "owner-1")[0]["reason"], "previous_run_pending")

    def test_refresh_maps_three_command_results(self):
        task = self.create()
        statuses = [("queued", "queued"), ("accepted", "accepted"), ("delivery_unknown", "delivery_unknown")]
        for index, (command_state, expected) in enumerate(statuses):
            now = self.make_due(task["id"], datetime.now(timezone.utc) + timedelta(seconds=index))
            pair = self.automations.claim_due(now)[0]
            run = pair[1]
            self.store.begin_command("conversation-1", run["command_id"], kind="start", text="x", origin="system")
            self.store.update_command(run["command_id"], command_state)
            self.automations.refresh_runs()
            rows = self.automations.runs(task["id"], "owner-1")
            self.assertEqual(next(row for row in rows if row["id"] == run["id"])["status"], expected)
            # Finish the previous command before creating the next interval slot.
            self.store.update_command(run["command_id"], "completed")
            self.automations.refresh_runs()

    def test_cas_pause_delete_and_idempotent_mutation(self):
        task = self.create(request_key="create-1")
        replay = self.create(request_key="create-1")
        self.assertEqual(replay["id"], task["id"])
        paused = self.automations.update(task["id"], "owner-1", revision=1, state="paused", request_key="pause-1")
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(self.automations.update(task["id"], "owner-1", revision=1, state="paused", request_key="pause-1")["revision"], 2)
        with self.assertRaises(ValueError):
            self.automations.update(task["id"], "owner-1", revision=1, state="active")
        deleted = self.automations.update(task["id"], "owner-1", revision=2, state="deleted")
        self.assertEqual(deleted["state"], "deleted")
        self.assertEqual(self.automations.list("owner-1"), [])
        self.assertEqual(self.store.get_conversation("conversation-1")["id"], "conversation-1")

    def test_source_change_and_recovery_do_not_replay_unknown(self):
        task = self.create()
        due = self.make_due(task["id"])
        run = self.automations.claim_due(due)[0][1]
        self.automations.recover_runs()
        self.assertEqual(self.automations.runs(task["id"], "owner-1")[0]["reason"], "interrupted_before_dispatch")
        task2 = self.create()
        self.store._db.execute("UPDATE conversations SET cwd='/var/tmp' WHERE id='conversation-1'")
        self.make_due(task2["id"])
        self.assertEqual(self.automations.claim_due(), [])
        self.assertEqual(self.automations.get(task2["id"], "owner-1")["state"], "paused")
        # A permission drift is handled identically, before dispatch.
        self.store._db.execute("UPDATE conversations SET cwd=?, settings_json=? WHERE id=?", ("/tmp", '{"permission":"danger-full-access"}', "conversation-1"))
        task3 = self.create()
        self.store._db.execute("UPDATE conversations SET settings_json=? WHERE id=?", ('{"permission":"read-only"}', "conversation-1"))
        self.make_due(task3["id"])
        self.assertEqual(self.automations.claim_due(), [])
        self.assertEqual(self.automations.get(task3["id"], "owner-1")["state"], "paused")

    def test_execution_mode_is_snapshotted_and_mode_drift_pauses(self):
        task = self.create()
        self.assertEqual(task["collaboration_mode"], "default")
        self.store._db.execute(
            "UPDATE conversations SET settings_json=? WHERE id=?",
            ('{"permission":"danger-full-access","collaboration_mode":"plan"}', "conversation-1"),
        )
        self.make_due(task["id"])
        self.assertEqual(self.automations.claim_due(), [])
        self.assertEqual(self.automations.get(task["id"], "owner-1")["state"], "paused")

    def test_two_connections_claim_atomically(self):
        task = self.create()
        now = self.make_due(task["id"])
        other_store = ConversationStore(self.path)
        other = AutomationStore(other_store)
        results = []
        lock = threading.Lock()
        def claim(instance):
            result = instance.claim_due(now)
            with lock: results.append(result)
        threads = [threading.Thread(target=claim, args=(self.automations,)), threading.Thread(target=claim, args=(other,))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        other_store.close()
        self.assertEqual(sum(len(value) for value in results), 1)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_owner_fails_and_pauses(self):
        temp = tempfile.TemporaryDirectory(dir="/tmp")
        try:
            store = ConversationStore(Path(temp.name) / "x.sqlite")
            store.create_conversation("c", title="x", cwd="/tmp", owner_account_id="o", settings={"permission": "danger-full-access"})
            store.bind_thread("c", {"id": "t"}, server_version="x")
            journal = AutomationStore(store)
            task = journal.create("c", "o", title="x", prompt="x", schedule={"kind": "interval", "interval_minutes": 1})
            store._db.execute("UPDATE conversation_automation_tasks SET next_run_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), task["id"]))
            scheduler = AutomationScheduler(journal, lambda task, run: None, lambda owner: False)
            await scheduler.tick()
            self.assertEqual(journal.get(task["id"], "o")["state"], "paused")
            self.assertEqual(journal.runs(task["id"], "o")[0]["reason"], "owner_disabled")
            store.close()
        finally:
            temp.cleanup()
