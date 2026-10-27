"""Retain source-scoped administrator lockout after employee quota retirement."""
import unittest
from unittest import mock

from shared import security


class AdministratorLockoutTests(unittest.TestCase):
    def setUp(self):
        self.old_buckets = security._login_fails
        security._login_fails = {}
        self.notifier = mock.patch.object(security, "_notify_account_event")
        self.notifier.start()

    def tearDown(self):
        self.notifier.stop()
        security._login_fails = self.old_buckets

    def test_lockout_is_per_account_and_source(self):
        for _ in range(5):
            security.record_failed_login("admin", "192.0.2.1")
        self.assertTrue(security.check_account_locked("admin", "192.0.2.1")[0])
        self.assertFalse(security.check_account_locked("admin", "192.0.2.2")[0])
        self.assertFalse(security.check_account_locked("other", "192.0.2.1")[0])
        security.clear_failed_logins("admin", "192.0.2.1")
        self.assertFalse(security.check_account_locked("admin", "192.0.2.1")[0])

    def test_lockout_expires_without_mutating_account_store(self):
        with mock.patch.object(security.time, "monotonic", return_value=10):
            for _ in range(5):
                security.record_failed_login("admin", "192.0.2.1")
        with mock.patch.object(security.time, "monotonic", return_value=611):
            self.assertFalse(security.check_account_locked("admin", "192.0.2.1")[0])
