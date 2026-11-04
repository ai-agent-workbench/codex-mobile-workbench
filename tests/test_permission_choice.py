"""Managed enrollment boundaries, without a native process or live account."""
import os
import sqlite3
import unittest
from unittest import mock

from shared import config, permission_choice, workbench_settings


class PermissionChoiceBoundaryTests(unittest.TestCase):
    def test_profile_environment_is_explicit_and_invalid_values_do_not_mean_legacy(self):
        for value, expected in (("0", False), ("1", True)):
            with mock.patch.dict(os.environ, {"CODEX_WEB_PERMISSION_CHOICE_REQUIRED": value}):
                self.assertIs(config._permission_choice_required(), expected)
        for value in ("", "false", "true", "2", " 1"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CODEX_WEB_PERMISSION_CHOICE_REQUIRED": value}):
                with self.assertRaises(RuntimeError):
                    config._permission_choice_required()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config._permission_choice_required())

    def test_legacy_does_not_require_a_settings_database_and_new_defaults_do_not_select_for_user(self):
        with mock.patch.object(permission_choice, "PERMISSION_CHOICE_REQUIRED", False), \
             mock.patch.object(permission_choice, "settings_store", side_effect=AssertionError("legacy read")), \
             mock.patch.object(workbench_settings, "PERMISSION_CHOICE_REQUIRED", False):
            self.assertFalse(permission_choice.pending("legacy"))
            self.assertEqual(workbench_settings.system_defaults()["permission"], "danger-full-access")
        with mock.patch.object(permission_choice, "PERMISSION_CHOICE_REQUIRED", True), \
             mock.patch.object(workbench_settings, "PERMISSION_CHOICE_REQUIRED", True):
            self.assertTrue(permission_choice.pending(None))
            self.assertEqual(workbench_settings.system_defaults()["permission"], "workspace-write")

    def test_unreadable_completion_fails_closed_without_exposing_storage_details(self):
        store = mock.Mock()
        store.permission_choice_completed.side_effect = sqlite3.DatabaseError("private storage detail")
        with mock.patch.object(permission_choice, "PERMISSION_CHOICE_REQUIRED", True), \
             mock.patch.object(permission_choice, "settings_store", return_value=store):
            with self.assertRaises(ValueError) as error:
                permission_choice.require_completed("owner")
            self.assertNotIn("private storage detail", str(error.exception))
