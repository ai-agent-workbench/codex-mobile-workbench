import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.conversation_service import ConversationService
from shared.conversation_artifacts import ConversationArtifactService
from shared.conversation_pagination import (
    ConversationPageCursorError,
    ConversationPageCursorExpired,
    ConversationPageSnapshots,
)
from tests.store_fixtures import seeded_store


class FakeRuntime:
    pass


class ConversationPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-pagination-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.service = ConversationService(
            self.store,
            ConversationArtifactService(self.store, self.root / "artifacts"),
            FakeRuntime(),
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, count, *, project_id=None, prefix="c"):
        values = []
        for number in range(count):
            conversation_id = prefix + format(number, "05x")
            self.store.create_conversation(
                conversation_id, title=conversation_id, cwd=str(self.workspace),
                project_id=project_id, settings={},
            )
            self.store.bind_thread(
                conversation_id, {"id": "thread-" + conversation_id},
                server_version="fixture",
            )
            values.append(conversation_id)
        return values

    def page_batches(self, *, project_id=..., account_id="account-a"):
        page = self.service.list_public_page(
            project_id=project_id, account_id=account_id,
        )
        batches = [page["values"]]
        while page["next_cursor"]:
            page = self.service.list_public_page(
                project_id=project_id, account_id=account_id,
                cursor=page["next_cursor"],
            )
            batches.append(page["values"])
        return batches

    def pages(self, **kwargs):
        return [value for batch in self.page_batches(**kwargs) for value in batch]

    def ranking_row(self, name, activity, *, terminal=None, status="completed",
                    state="idle", pinned=False, project="project-a"):
        conversation_id = self.add(1, project_id=project, prefix=name)[0]
        stamp = lambda day: f"2026-09-{day:02d}T12:00:00+00:00"
        self.store._db.execute(
            "UPDATE conversations SET activity_at=?, created_at=?, state=?, pinned=? WHERE id=?",
            (stamp(activity), stamp(1), state, int(pinned), conversation_id),
        )
        if terminal is not None:
            turn = "turn-" + conversation_id
            self.store._db.execute(
                "INSERT INTO turns(id,conversation_id,ordinal,status,completed_at) VALUES(?,?,1,?,?)",
                (turn, conversation_id, status, stamp(terminal)),
            )
            self.store._db.execute(
                "UPDATE conversations SET last_terminal_turn_id=?, last_terminal_at=?, "
                "terminal_generation=1 WHERE id=?", (turn, stamp(terminal), conversation_id),
            )
        return conversation_id

    def test_history_priority_account_scope_and_terminal_time(self):
        pin_old = self.ranking_row("pin-old", 2, terminal=9, pinned=True)
        pin_new = self.ranking_row("pin-new", 3, terminal=1, pinned=True)
        unread_old = self.ranking_row("unread-old", 8, terminal=4)
        unread_new = self.ranking_row("unread-new", 1, terminal=5)
        active = self.ranking_row("active", 10, terminal=2, state="active")
        failed = self.ranking_row("failed", 9, terminal=9, status="failed")
        stopped = self.ranking_row("stopped", 8, terminal=8, status="interrupted")
        read = self.ranking_row("read", 7, terminal=6)
        ready = self.ranking_row("ready", 6)
        outside = self.ranking_row("outside", 12, project="project-b")
        self.store.mark_read(pin_new, "account-a")
        self.store.mark_read(read, "account-a")
        expected = [pin_new, pin_old, unread_new, unread_old, active, failed, stopped, read, ready]
        values = self.pages(project_id="project-a", account_id="account-a")
        self.assertEqual([x["id"] for x in values], expected)
        self.assertNotIn(outside, expected)
        global_ids = [x["id"] for x in self.pages(account_id="account-a")]
        self.assertEqual(global_ids, expected[:4] + [outside] + expected[4:])
        other_account = [x["id"] for x in self.pages(project_id="project-a", account_id="account-b")]
        self.assertEqual(other_account[:5], [pin_new, pin_old, read, unread_new, unread_old])
        # No account means no unread prioritization, not "everything is unread".
        anonymous = self.store.list_conversation_ids(project_id="project-a")
        self.assertEqual(anonymous, [pin_new, pin_old, active, failed, unread_old, stopped, read, ready, unread_new])
        self.store.mark_read(unread_new, "account-a")
        after_read = [x["id"] for x in self.pages(project_id="project-a", account_id="account-a")]
        self.assertEqual(after_read, [pin_new, pin_old, unread_old, active, failed, stopped, read, ready, unread_new])

    def test_unread_priority_changes_only_on_new_snapshot(self):
        ids = [self.ranking_row("item-" + str(i), i + 1) for i in range(25)]
        first = self.service.list_public_page(account_id="account-a", project_id="project-a")
        first_ids = [x["id"] for x in first["values"]]
        target = ids[0]
        turn = "turn-" + target
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,completed_at) "
            "VALUES(?,?,1,'completed','2026-09-29T12:00:00+00:00')", (turn, target),
        )
        self.store._db.execute(
            "UPDATE conversations SET last_terminal_turn_id=?,last_terminal_at='2026-09-29T12:00:00+00:00',"
            "terminal_generation=1 WHERE id=?", (turn, target),
        )
        second = self.service.list_public_page(
            account_id="account-a", project_id="project-a", cursor=first["next_cursor"],
        )
        self.assertEqual(first_ids + [x["id"] for x in second["values"]], list(reversed(ids)))
        refreshed = self.service.list_public_page(account_id="account-a", project_id="project-a")
        self.assertEqual(refreshed["values"][0]["id"], target)
        self.store.mark_read(target, "account-a")
        self.assertEqual(self.pages(project_id="project-a")[0]["id"], ids[-1])

    def test_page_sizes_zero_twenty_twenty_one_forty_and_forty_five(self):
        for count in (0, 20, 21, 40, 45):
            with self.subTest(count=count):
                self.add(count, prefix="c" + str(count) + "_")
                batches = self.page_batches()
                values = [value for batch in batches for value in batch]
                self.assertEqual([len(batch) for batch in batches], {
                    0: [0], 20: [20], 21: [20, 1], 40: [20, 20], 45: [20, 20, 5],
                }[count])
                self.assertTrue(all(len(batch) <= 20 for batch in batches))
                self.assertEqual(len(values), count)
                self.assertEqual(len({value["id"] for value in values}), count)
                self.store._db.execute("DELETE FROM conversations")
                self.service._pages = ConversationPageSnapshots()

    def test_snapshot_keeps_original_order_without_new_or_reordered_rows(self):
        ids = self.add(45)
        first = self.service.list_public_page(account_id="account-a")
        first_ids = [value["id"] for value in first["values"]]
        self.store.set_pinned([ids[0]], True)
        self.add(1, prefix="new")
        second = self.service.list_public_page(
            account_id="account-a", cursor=first["next_cursor"],
        )
        third = self.service.list_public_page(
            account_id="account-a", cursor=second["next_cursor"],
        )
        snapshot_ids = first_ids + [value["id"] for value in second["values"]] + [
            value["id"] for value in third["values"]
        ]
        self.assertEqual(len(snapshot_ids), 45)
        self.assertEqual(len(set(snapshot_ids)), 45)
        self.assertNotIn("new00000", snapshot_ids)

    def test_order_probe_does_not_allocate_or_evict_cursors_or_read_body(self):
        self.add(45, project_id="project-a")
        self.service._pages = ConversationPageSnapshots(maximum=2)
        first = self.service.list_public_page(account_id="account-a", project_id="project-a")
        snapshots = dict(self.service._pages._snapshots)
        with mock.patch.object(self.store, "snapshot", side_effect=AssertionError("body read")), \
             mock.patch.object(self.store, "mark_read", side_effect=AssertionError("read receipt")):
            for _ in range(130):
                version = self.service.public_order_version(account_id="account-a", project_id="project-a")
                self.assertEqual(version, first["order_version"])
        self.assertEqual(self.service._pages._snapshots, snapshots)
        self.assertEqual(len(self.service.list_public_page(
            account_id="account-a", project_id="project-a", cursor=first["next_cursor"],
        )["values"]), 20)

    def test_new_order_snapshot_changes_cursor_and_has_no_duplicate_continuation(self):
        ids = self.add(45, project_id="project-a")
        first = self.service.list_public_page(account_id="account-a", project_id="project-a")
        self.store.set_pinned([ids[0]], True)
        version = self.service.public_order_version(account_id="account-a", project_id="project-a")
        self.assertNotEqual(version, first["order_version"])
        fresh = self.service.list_public_page(account_id="account-a", project_id="project-a")
        self.assertEqual(fresh["order_version"], version)
        self.assertNotEqual(fresh["next_cursor"].split(".")[0], first["next_cursor"].split(".")[0])
        self.assertEqual(fresh["values"][0]["id"], ids[0])
        values = list(fresh["values"])
        while fresh["next_cursor"]:
            fresh = self.service.list_public_page(
                account_id="account-a", project_id="project-a", cursor=fresh["next_cursor"],
            )
            self.assertEqual(fresh["order_version"], version)
            values.extend(fresh["values"])
        self.assertEqual(len(values), 45)
        self.assertEqual({value["id"] for value in values}, set(ids))

    def test_order_version_covers_unread_groups_and_is_account_and_project_scoped(self):
        cid = self.ranking_row("one", 20, terminal=20)
        before = self.service.public_order_version(account_id="account-a", project_id="project-a")
        outside = self.add(1, project_id="outside")[0]
        self.store.set_pinned([outside], True)
        self.assertEqual(self.service.public_order_version(
            account_id="account-a", project_id="project-a",
        ), before)
        self.store.mark_read(cid, "account-a")
        self.assertNotEqual(self.service.public_order_version(
            account_id="account-a", project_id="project-a",
        ), before)
        self.assertEqual(self.service.public_order_version(
            account_id="account-b", project_id="project-a",
        ), before)
        self.store._db.execute("UPDATE conversations SET revision=revision+1 WHERE id=?", (cid,))
        self.assertEqual(self.service.public_order_version(
            account_id="account-b", project_id="project-a",
        ), before)

    def test_large_snapshot_filter_and_selection_past_old_limit(self):
        project_ids = self.add(501, project_id="project-a", prefix="a")
        self.add(21, project_id="project-b", prefix="b")
        values = self.pages(project_id="project-a")
        self.assertEqual(len(values), 501)
        self.assertEqual({value["project_id"] for value in values}, {"project-a"})
        selected = self.service.scoped_public_ids(
            [values[-1]["id"], "b00014", "missing"],
            project_id="project-a", account_id="account-a",
        )
        self.assertEqual(selected, [values[-1]["id"]])

    def test_delete_or_project_move_between_pages_is_skipped_and_page_refills(self):
        ids = self.add(45, project_id="project-a")
        first = self.service.list_public_page(
            account_id="account-a", project_id="project-a",
        )
        self.store.delete_conversation(ids[20])
        second = self.service.list_public_page(
            account_id="account-a", project_id="project-a", cursor=first["next_cursor"],
        )
        self.assertEqual(len(second["values"]), 20)
        self.assertNotIn(ids[20], [value["id"] for value in second["values"]])
        self.store._db.execute(
            "UPDATE conversations SET project_id = ? WHERE id = ?", ("project-b", ids[3])
        )
        third = self.service.list_public_page(
            account_id="account-a", project_id="project-a", cursor=second["next_cursor"],
        )
        self.assertEqual(len(third["values"]), 3)
        self.assertIsNone(third["next_cursor"])
        self.assertNotIn(ids[3], [value["id"] for value in third["values"]])

    def test_retry_cursor_is_repeatable_and_activity_change_does_not_change_snapshot(self):
        self.add(45)
        first = self.service.list_public_page(account_id="account-a")
        cursor = first["next_cursor"]
        self.store._db.execute(
            "UPDATE conversations SET activity_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", first["values"][-1]["id"]),
        )
        retry = self.service.list_public_page(account_id="account-a", cursor=cursor)
        again = self.service.list_public_page(account_id="account-a", cursor=cursor)
        self.assertEqual([value["id"] for value in retry["values"]], [value["id"] for value in again["values"]])
        self.assertEqual(len(retry["values"]), 20)
        last = self.service.list_public_page(account_id="account-a", cursor=retry["next_cursor"])
        all_ids = [value["id"] for value in first["values"] + retry["values"] + last["values"]]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_cursor_rejects_other_account_or_scope_and_expiry(self):
        self.add(21, project_id="project-a")
        first = self.service.list_public_page(
            account_id="account-a", project_id="project-a",
        )
        with self.assertRaises(ConversationPageCursorError):
            self.service.list_public_page(
                account_id="account-b", project_id="project-a",
                cursor=first["next_cursor"],
            )
        with self.assertRaises(ConversationPageCursorError):
            self.service.list_public_page(
                account_id="account-a", cursor=first["next_cursor"],
            )
        with self.assertRaises(ConversationPageCursorError):
            self.service.list_public_page(
                account_id="account-a", project_id="project-a", cursor="not-a-cursor",
            )
        self.service._pages = ConversationPageSnapshots(ttl_seconds=-1)
        expired = self.service.list_public_page(
            account_id="account-a", project_id="project-a",
        )
        with self.assertRaises(ConversationPageCursorExpired):
            self.service.list_public_page(
                account_id="account-a", project_id="project-a",
                cursor=expired["next_cursor"],
            )

    def test_full_page_has_no_cursor_when_remaining_snapshot_rows_are_deleted(self):
        ids = self.add(45)
        first = self.service.list_public_page(account_id="account-a")
        for conversation_id in ids[:5]:
            self.store.delete_conversation(conversation_id)
        last = self.service.list_public_page(account_id="account-a", cursor=first["next_cursor"])
        self.assertEqual(len(last["values"]), 20)
        self.assertIsNone(last["next_cursor"])

    def test_cache_is_bounded_and_restart_or_invalid_positions_require_refresh(self):
        cache = ConversationPageSnapshots(maximum=2)
        first = cache.start(["a", "b"], account_id="a", scope="all")
        cache.start(["a"], account_id="a", scope="all")
        cache.start(["a"], account_id="a", scope="all")
        self.assertEqual(len(cache._snapshots), 2)
        with self.assertRaises(ConversationPageCursorError):
            cache.resume(cache.cursor(first, 1), account_id="a", scope="all")
        current = cache.start(["a", "b"], account_id="a", scope="all")
        for cursor in [cache.cursor(current, 2), cache.cursor(current, -1), "x" * 129]:
            with self.assertRaises(ConversationPageCursorError):
                cache.resume(cursor, account_id="a", scope="all")
        with self.assertRaises(ConversationPageCursorError):
            ConversationPageSnapshots().resume(cache.cursor(current, 1), account_id="a", scope="all")

    def test_same_timestamp_and_pinned_order_match_store_snapshot(self):
        ids = self.add(21)
        timestamp = "2026-09-01T00:00:00+00:00"
        self.store._db.execute(
            "UPDATE conversations SET activity_at = ?, created_at = ?", (timestamp, timestamp)
        )
        self.store.set_pinned([ids[4]], True)
        expected = [value["id"] for value in self.store.list_conversations(500)]
        actual = [value["id"] for value in self.pages()]
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
