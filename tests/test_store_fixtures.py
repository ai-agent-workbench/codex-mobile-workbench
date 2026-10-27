"""Protect the optimized fixture's isolation and unchanged durability settings."""
from pathlib import Path
import tempfile
import unittest

from engine.conversation_store import ConversationStore
from tests.store_fixtures import seeded_store


class SeededStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def store(self, name):
        store = seeded_store(self.root / name)
        self.addCleanup(store.close)
        return store

    def test_instances_are_disk_backed_isolated_and_keep_full_wal(self):
        first, second = self.store("one.sqlite3"), self.store("two.sqlite3")
        first.create_conversation("only-first", title="fixture", cwd=str(self.root), settings={})
        self.assertIsNotNone(first.get_conversation("only-first"))
        self.assertEqual(second._db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)
        for store in (first, second):
            self.assertEqual(store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(store._db.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertTrue(store.path.is_file())
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_schema_matches_real_cold_start_including_search_triggers(self):
        cold = ConversationStore(self.root / "cold.sqlite3")
        self.addCleanup(cold.close)
        seeded = self.store("seeded.sqlite3")
        query = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        self.assertEqual([tuple(row) for row in cold._db.execute(query)],
                         [tuple(row) for row in seeded._db.execute(query)])
        self.assertEqual([tuple(row) for row in cold._db.execute("SELECT * FROM schema_meta ORDER BY key")],
                         [tuple(row) for row in seeded._db.execute("SELECT * FROM schema_meta ORDER BY key")])

    def test_existing_database_and_symlink_are_never_overwritten(self):
        existing = self.root / "existing.sqlite3"
        existing.write_bytes(b"keep fixture")
        with self.assertRaises(FileExistsError):
            seeded_store(existing)
        self.assertEqual(existing.read_bytes(), b"keep fixture")
        link = self.root / "link.sqlite3"
        link.symlink_to(self.root / "missing.sqlite3")
        with self.assertRaises(FileExistsError):
            seeded_store(link)
        self.assertTrue(link.is_symlink())

    def test_data_survives_close_and_real_reopen_without_reseeding(self):
        path = self.root / "restart.sqlite3"
        first = seeded_store(path)
        try:
            first.create_conversation("persistent", title="fixture", cwd=str(self.root), settings={})
        finally:
            first.close()
        reopened = ConversationStore(path)
        self.addCleanup(reopened.close)
        self.assertIsNotNone(reopened.get_conversation("persistent"))
