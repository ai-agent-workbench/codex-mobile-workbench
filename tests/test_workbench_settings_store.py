import os
import stat
import tempfile
import unittest
from pathlib import Path

from shared.workbench_settings import SettingsConflict, SettingsStore, system_defaults


class WorkbenchSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = Path(self.temp.name) / "state" / "workbench-settings.sqlite3"
        self.store = SettingsStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_reads_do_not_create_state(self):
        self.assertEqual(self.store.read_account("account"), {
            "revision": 0, "defaults": {}, "personal": "", "previous_personal": None,
            "presets": []
        })
        self.assertEqual(self.store.read_project("account", "project"), {
            "revision": 0, "defaults": {}
        })
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.store.resolve(None), {
            "values": system_defaults(),
            "sources": {key: "system" for key in system_defaults()},
            "personal": "",
            "account_revision": 0,
            "project_revision": 0,
        })

    def test_account_project_resolution_and_isolation(self):
        account = self.store.save_account("one", 0, {
            "defaults": {"model": "model-a", "skills": ["skill-a"]},
            "personal": "use concise Chinese",
            "presets": [{"id": "quick", "name": "快捷", "defaults": {"effort": "low"}}],
        })
        self.assertEqual(account["revision"], 1)
        project = self.store.save_project("one", "project-a", 0, {
            "model": "model-project", "followup": "queue"
        })
        self.assertEqual(project["revision"], 1)
        resolved = self.store.resolve("one", "project-a")
        self.assertEqual(resolved["values"]["model"], "model-project")
        self.assertEqual(resolved["values"]["skills"], ["skill-a"])
        self.assertEqual(resolved["values"]["followup"], "queue")
        self.assertEqual(resolved["sources"]["model"], "project")
        self.assertEqual(resolved["sources"]["skills"], "account")
        self.assertEqual(resolved["personal"], "use concise Chinese")
        self.assertEqual(self.store.read_account("one")["previous_personal"], "")
        self.assertEqual(self.store.resolve("two", "project-a")["values"], system_defaults())

    def test_cas_rejects_without_revision_change(self):
        saved = self.store.save_account("one", 0, {"defaults": {"model": "one"}})
        with self.assertRaises(SettingsConflict):
            self.store.save_account("one", 0, {"personal": "late"})
        self.assertEqual(self.store.read_account("one"), saved)
        self.store.save_project("one", "p", 0, {"effort": "low"})
        with self.assertRaises(SettingsConflict):
            self.store.save_project("one", "p", 0, {"model": "late"})
        self.assertEqual(self.store.read_project("one", "p")["revision"], 1)

    def test_invalid_values_do_not_change_revision(self):
        self.store.save_account("one", 0, {"defaults": {"model": "one"}})
        before = self.store.read_account("one")
        for changes in (
            {"defaults": {"unknown": "x"}},
            {"defaults": {"completion_notification": 1}},
            {"defaults": {"skills": ["same", "same"]}},
            {"presets": [{"id": "bad id", "name": "x", "defaults": {}}]},
        ):
            with self.assertRaises(ValueError):
                self.store.save_account("one", 1, changes)
        self.assertEqual(self.store.read_account("one"), before)

    def test_replacements_remove_old_overrides_and_personal_restore_is_versioned(self):
        self.store.save_account('one',0,{'defaults':{'permission':'read-only'},'personal':'old'})
        second=self.store.save_account('one',1,{'defaults':{},'personal':'new'})
        self.assertEqual(second['defaults'],{})
        self.assertEqual(second['previous_personal'],'old')
        third=self.store.save_account('one',2,{'personal':second['previous_personal']})
        self.assertEqual(third['personal'],'old')
        self.assertEqual(third['previous_personal'],'new')
        self.store.save_project('one','p',0,{'permission':'read-only'})
        self.assertEqual(self.store.save_project('one','p',1,{})['defaults'],{})

    def test_two_concurrent_writers_have_only_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.store.save_account('one',0,{'personal':'base'})
        def save(value):
            try:
                self.store.save_account('one',1,{'personal':value})
                return True
            except SettingsConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(save,['first','second'])),[False,True])
        self.assertEqual(self.store.read_account('one')['revision'],2)

    def test_results_are_detached_and_database_is_private(self):
        saved = self.store.save_account("one", 0, {"defaults": {"skills": ["skill"]}})
        saved["defaults"]["skills"].append("mutated")
        self.assertEqual(self.store.read_account("one")["defaults"]["skills"], ["skill"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_symlink_database_is_rejected(self):
        target = Path(self.temp.name) / "target.sqlite3"
        target.touch()
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.store.save_account("one", 0, {"personal": "x"})
        with self.assertRaises(ValueError):
            self.store.read_account("one")


if __name__ == "__main__":
    unittest.main()
