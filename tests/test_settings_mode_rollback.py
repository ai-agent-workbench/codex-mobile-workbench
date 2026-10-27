"""Plan-mode metadata remains readable by the pre-mode settings implementation."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from shared.workbench_settings import SettingsStore


class SettingsModeRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="settings-mode-rollback-", dir=Path.home())
        self.path = Path(self.temp.name) / "settings.sqlite3"
        self.store = SettingsStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def _old_store(self):
        source = (Path(__file__).parent / 'fixtures/settings_pre_modes.py').read_text(encoding='utf-8')
        module_path = Path(self.temp.name) / "old_workbench_settings.py"
        module_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("old_workbench_settings", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SettingsStore(self.path)

    def test_mode_metadata_does_not_break_old_reader_or_revive_after_old_save(self):
        presets = [
            {"id": "plan", "name": "计划", "defaults": {"model": "gpt-6-sol", "collaboration_mode": "plan"}},
            {"id": "normal", "name": "普通", "defaults": {"collaboration_mode": "default"}},
        ]
        account = self.store.save_account(
            "account", 0, {"defaults": {"collaboration_mode": "plan"}, "presets": presets}
        )
        project = self.store.save_project("account", "project", 0, {"collaboration_mode": "plan"})
        self.assertEqual(account["defaults"]["collaboration_mode"], "plan")
        self.assertEqual(project["defaults"]["collaboration_mode"], "plan")
        with sqlite3.connect(self.path) as db:
            defaults_json, presets_json, native_json, native_revision = db.execute(
                "SELECT defaults_json,presets_json,native_modes_json,native_modes_revision "
                "FROM workbench_account_settings WHERE account='account'"
            ).fetchone()
            project_json, project_mode, project_revision = db.execute(
                "SELECT defaults_json,native_mode,native_mode_revision FROM workbench_project_settings "
                "WHERE account='account' AND project_id='project'"
            ).fetchone()
        self.assertNotIn("collaboration_mode", defaults_json)
        self.assertNotIn("collaboration_mode", presets_json)
        self.assertEqual(json.loads(native_json), {"defaults": "plan", "presets": {"normal": "default", "plan": "plan"}})
        self.assertEqual(native_revision, account["revision"])
        self.assertNotIn("collaboration_mode", project_json)
        self.assertEqual((project_mode, project_revision), ("plan", project["revision"]))

        old = self._old_store()
        self.assertEqual(old.read_account("account")["defaults"], {})
        self.assertEqual(old.read_project("account", "project")["defaults"], {})
        self.assertEqual(old.resolve("account", "project")["values"]["model"], "gpt-6-sol")
        old_account = old.save_account("account", account["revision"], {"personal": "old writer"})
        old_project = old.save_project("account", "project", project["revision"], {"model": "gpt-6-astra"})
        self.assertEqual(old_account["revision"], account["revision"] + 1)
        self.assertEqual(old_project["revision"], project["revision"] + 1)

        resolved = self.store.resolve("account", "project")
        self.assertEqual(resolved["values"]["collaboration_mode"], "default")
        self.assertEqual(resolved["sources"]["collaboration_mode"], "system")
        self.assertEqual(resolved["values"]["model"], "gpt-6-astra")

    def test_read_old_schema_uses_null_metadata_without_mutating_it(self):
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE workbench_account_settings (
                    account TEXT PRIMARY KEY, revision INTEGER NOT NULL, defaults_json TEXT NOT NULL,
                    personal TEXT NOT NULL, previous_personal TEXT, presets_json TEXT NOT NULL
                );
                CREATE TABLE workbench_project_settings (
                    account TEXT NOT NULL, project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    defaults_json TEXT NOT NULL, PRIMARY KEY(account, project_id)
                );
            """)
            db.execute("INSERT INTO workbench_account_settings VALUES(?,?,?,?,?,?)", ("account", 1, '{"model":"gpt-6-sol"}', "x", None, '[]'))
            db.execute("INSERT INTO workbench_project_settings VALUES(?,?,?,?)", ("account", "project", 1, '{"effort":"high"}'))
        self.assertEqual(self.store.read_account("account")["defaults"], {"model": "gpt-6-sol"})
        self.assertEqual(self.store.read_project("account", "project")["defaults"], {"effort": "high"})
        with sqlite3.connect(self.path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(workbench_account_settings)")}
        self.assertNotIn("native_modes_json", columns)
        self.store.save_account("account", 1, {"defaults": {"collaboration_mode": "plan"}})
        with sqlite3.connect(self.path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(workbench_account_settings)")}
        self.assertTrue({"native_modes_json", "native_modes_revision"}.issubset(columns))

    def test_current_unpublished_json_mode_is_read_then_normalized_on_save(self):
        # Simulates the briefly-unpublished writer which placed mode inside the
        # legacy JSON columns before this compatible metadata layout existed.
        self.store.save_account("account", 0, {"personal": "x"})
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE workbench_account_settings SET defaults_json=? WHERE account='account'", (json.dumps({"collaboration_mode": "plan"}),))
        self.assertEqual(self.store.resolve("account")["values"]["collaboration_mode"], "plan")
        self.store.save_account("account", 1, {"personal": "y"})
        with sqlite3.connect(self.path) as db:
            defaults_json, metadata, revision = db.execute(
                "SELECT defaults_json,native_modes_json,native_modes_revision FROM workbench_account_settings WHERE account='account'"
            ).fetchone()
        self.assertNotIn("collaboration_mode", defaults_json)
        self.assertEqual(json.loads(metadata), {"defaults": "plan"})
        self.assertEqual(revision, 2)


if __name__ == "__main__":
    unittest.main()
