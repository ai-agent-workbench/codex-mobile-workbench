import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_store import (
    ConversationStore,
    SCHEMA_VERSION,
    WEB_PUSH_SCHEMA_VERSION,
)
from shared import web_push
from shared.return_to import safe_conversation_return_to


_ENDPOINT = "https://fcm.googleapis.com/fcm/send/fixture-token"
_KEYS = {
    "p256dh": base64.urlsafe_b64encode(b"\x04" + b"x" * 64).rstrip(b"=").decode(),
    "auth": base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode(),
}


class WebPushValidationTests(unittest.TestCase):
    def test_auth_redirect_preserves_only_exact_turn_deep_link(self):
        from starlette.requests import Request
        from shared import security

        def request(path, query):
            return Request({
                "type": "http", "method": "GET", "scheme": "https",
                "server": ("workbench.test", 443), "path": path,
                "query_string": query.encode("ascii"),
                "headers": [(b"host", b"workbench.test"), (b"accept", b"text/html")],
            })

        with tempfile.TemporaryDirectory() as root:
            accounts = Path(root) / "accounts.json"
            enabled = Path(root) / "totp-enabled"
            enabled.touch()
            with (
                mock.patch.object(security, "ACCOUNTS_FILE", accounts),
                mock.patch.object(security, "ENABLED_FILE", enabled),
                mock.patch.object(security, "current_account", return_value=None),
            ):
                accepted = security.require_auth(request(
                    "/conversation/c:1", "turn=t:1"
                ))
                rejected = security.require_auth(request(
                    "/conversation/c:1", "turn=t:1&next=https://evil.test"
                ))
        self.assertEqual(
            accepted.headers["location"],
            "/login?return_to=%2Fconversation%2Fc%3A1%3Fturn%3Dt%3A1",
        )
        self.assertEqual(rejected.headers["location"], "/login")

    def test_origin_is_exact_https_origin(self):
        self.assertEqual(
            web_push.canonical_https_origin("https://WorkBench.Test:443/"),
            "https://workbench.test",
        )
        for origin in (
            "http://workbench.test",
            "https://workbench.test/path",
            "https://user@workbench.test",
            "https://[",
        ):
            with self.subTest(origin=origin), self.assertRaises(
                web_push.WebPushError
            ):
                web_push.canonical_https_origin(origin)

    def test_only_fcm_https_subscription_is_accepted(self):
        value = web_push.validate_subscription({"endpoint": _ENDPOINT, "keys": _KEYS})
        self.assertEqual(value["endpoint"], _ENDPOINT)
        for endpoint in (
            "https://example.com/fcm/send/x",
            "https://fcm.googleapis.com.evil.test/fcm/send/x",
            "https://fcm.googleapis.com:bad/fcm/send/x",
            "https://user@fcm.googleapis.com/fcm/send/x",
            "http://fcm.googleapis.com/fcm/send/x",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(
                web_push.WebPushError
            ):
                web_push.validate_subscription({
                    "endpoint": endpoint,
                    "keys": _KEYS,
                })

    def test_payload_keeps_conversation_title_and_final_text_only(self):
        payload = json.loads(web_push.build_payload(
            title="我的对话标题", final_text="  最终   回复  ",
            conversation_id="conversation-1", turn_id="turn-1",
        ))
        self.assertEqual(payload["title"], "我的对话标题")
        self.assertEqual(payload["final_text"], "最终 回复")

    def test_payload_limits_notification_preview_to_two_or_three_lines(self):
        payload = json.loads(web_push.build_payload(
            title="对话标题", final_text="甲" * 80,
            conversation_id="conversation-1", turn_id="turn-1",
        ))
        self.assertEqual(payload["final_text"], "甲" * 47 + "…")
        self.assertEqual(len(payload["final_text"]), 48)

    def test_return_to_is_exact_canonical_conversation_turn_only(self):
        self.assertEqual(
            safe_conversation_return_to("/conversation/c-1?turn=turn-1"),
            "/conversation/c-1?turn=turn-1",
        )
        for value in (
            "https://example.test/conversation/c-1?turn=turn-1",
            "//example.test/x", "/conversation/c-1?turn=turn-1&x=1",
            "/conversation/c-1", "/conversation/c-1?turn=%74urn-1",
            "/conversation/c-1?turn=turn-1#x", "/conversation/c-1?turn=turn-1\\x",
            "https://[/conversation/c-1?turn=turn-1",
        ):
            self.assertIsNone(safe_conversation_return_to(value), value)


class WebPushStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.tmp.name) / "conversations.sqlite3")
        self.store.create_conversation(
            "conversation-1", title="fixture", cwd="/tmp", settings={},
            completion_notification=True, owner_account_id="admin-a",
        )
        self.store.bind_thread("conversation-1", {"id": "thread-1", "status": "active"}, server_version="fixture")
        self.store.bind_turn("conversation-1", {"id": "turn-1", "status": "completed"}, command_id=self._start_command())

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def _start_command(self):
        self.store.begin_command("conversation-1", "command-1", kind="start", text="x")
        return "command-1"

    def test_endpoint_cannot_be_taken_over_and_delivery_is_not_retried(self):
        record = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT, origin="https://workbench.test",
            **_KEYS,
        )
        with self.assertRaises(AppServerProtocolError):
            self.store.upsert_web_push_subscription(
                account_id="admin-b", endpoint=_ENDPOINT, origin="https://workbench.test", **_KEYS,
            )
        deliveries = self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")
        self.assertEqual(len(deliveries), 1)
        self.assertTrue(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.assertEqual(self.store.release_incomplete_web_push_deliveries(), 1)
        self.assertEqual(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a"), [])

    def test_new_database_contains_backward_compatible_push_extension(self):
        names = {
            row["name"]
            for row in self.store._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertTrue({
            "web_push_subscriptions",
            "web_push_deliveries",
            "web_push_dispatches",
        }.issubset(names))
        version = self.store._db.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        extension = self.store._db.execute(
            "SELECT value FROM schema_meta "
            "WHERE key = 'web_push_schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertEqual(extension, str(WEB_PUSH_SCHEMA_VERSION))

    def test_push_preparation_and_claim_recheck_pending_delete(self):
        record = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT, origin="https://workbench.test", **_KEYS,
        )
        self.store.set_conversation_state(
            "conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True,
        )
        self.assertEqual(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a"), [])
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM web_push_dispatches").fetchone()[0], 0)
        self.store.set_conversation_state(
            "conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False,
        )
        self.assertEqual(len(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")), 1)
        for flag, label in [(True, "transport_lost"), (False, "deleting_native_thread")]:
            with self.subTest(flag=flag, label=label):
                self.store.set_conversation_state(
                    "conversation-1", "needs_user", recovery_state=label, native_delete_pending=flag,
                )
                self.assertFalse(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.store.set_conversation_state(
            "conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False,
        )
        self.assertTrue(self.store.claim_web_push_delivery("turn-1", record["id"]))

    def test_deferred_push_resumes_only_unclaimed_prepared_deliveries(self):
        record = self.store.upsert_web_push_subscription(account_id="admin-a", endpoint=_ENDPOINT,
                                                        origin="https://workbench.test", **_KEYS)
        self.assertEqual(len(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")), 1)
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        self.assertFalse(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.store.defer_terminal_delivery("conversation-1", "turn-1", live_observed=True)
        self.store.set_conversation_state("conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False)
        self.assertEqual(len(self.store.take_deferred_terminal_deliveries("conversation-1")), 1)
        self.assertEqual(len(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a", resume_pending=True)), 1)
        self.assertTrue(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.assertEqual(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a", resume_pending=True), [])
        self.store.release_incomplete_web_push_deliveries()
        self.assertEqual(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a", resume_pending=True), [])

    def test_push_claim_honors_current_opt_out_after_ntfy_claim(self):
        record = self.store.upsert_web_push_subscription(account_id="admin-a", endpoint=_ENDPOINT,
                                                        origin="https://workbench.test", **_KEYS)
        from engine.conversation_store import utc_now
        self.store._db.execute("UPDATE turns SET completed_at=? WHERE id='turn-1'", (utc_now(),))
        self.assertTrue(self.store.claim_completion_notification("conversation-1", "turn-1"))
        self.assertEqual(len(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")), 1)
        self.store.set_completion_notification("conversation-1", False)
        self.assertEqual(self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a", resume_pending=True), [])
        self.assertFalse(self.store.claim_web_push_delivery("turn-1", record["id"]))

    def test_old_local_delivery_lease_cannot_clear_new_delete_deferral(self):
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        self.store.defer_terminal_delivery("conversation-1", "turn-1", live_observed=True)
        self.store.set_conversation_state("conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False)
        old = self.store.take_deferred_terminal_deliveries("conversation-1")[0]
        self.store.set_conversation_state("conversation-1", "needs_user", recovery_state="delete_delivery_unknown", native_delete_pending=True)
        self.store.defer_terminal_delivery("conversation-1", "turn-1")
        self.store.finish_deferred_terminal_delivery("conversation-1", "turn-1", old["lease"])
        self.store.set_conversation_state("conversation-1", "idle", recovery_state="delete_rejected", native_delete_pending=False)
        new = self.store.take_deferred_terminal_deliveries("conversation-1")[0]
        self.assertGreater(new["lease"], old["lease"])

    def test_live_observation_and_projection_commit_atomically(self):
        self.store._db.execute("UPDATE turns SET status='inProgress', completed_at=NULL WHERE id='turn-1'")
        reader = sqlite3.connect(Path(self.tmp.name) / "conversations.sqlite3")
        original_finish = self.store._finish
        def check_before_commit(error):
            if error is None:
                status, marker = self.store._db.execute("SELECT status,delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()
                self.assertEqual((status, marker), ("completed", 1))
                self.assertEqual(reader.execute("SELECT status,delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone(), ("inProgress", 0))
            original_finish(error)
        try:
            with mock.patch.object(self.store, "_finish", side_effect=check_before_commit):
                self.store.apply_notification({"method": "turn/completed", "params": {
                    "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []},
                }})
            self.assertEqual(reader.execute("SELECT status,delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone(), ("completed", 1))
        finally:
            reader.close()

    def test_wire_observation_rejects_unowned_and_nonterminal_events(self):
        for thread_id, turn_id, status in [("other", "turn-1", "completed"),
                                           ("thread-1", "other", "completed"),
                                           ("thread-1", "turn-1", "inProgress")]:
            self.store.apply_notification({"method": "turn/completed", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "status": status, "items": []},
            }})
            self.assertEqual(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)

    def test_repeated_wire_observation_does_not_reopen_unknown_channels(self):
        from engine.conversation_store import utc_now
        self.store._db.execute("UPDATE turns SET completed_at=? WHERE id='turn-1'", (utc_now(),))
        record = self.store.upsert_web_push_subscription(account_id="admin-a", endpoint=_ENDPOINT,
                                                        origin="https://workbench.test", **_KEYS)
        self.assertTrue(self.store.claim_completion_notification("conversation-1", "turn-1"))
        self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")
        self.assertTrue(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.store.release_incomplete_notifications()
        self.store.release_incomplete_web_push_deliveries()
        self.store.apply_notification({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []},
        }})
        self.assertEqual(self.store.take_deferred_terminal_deliveries("conversation-1"), [])
        self.assertEqual(self.store._db.execute("SELECT delete_deferred_terminal FROM turns WHERE id='turn-1'").fetchone()[0], 0)

    def test_expired_ntfy_alone_does_not_keep_a_deferred_callback_alive(self):
        self.store._db.execute("UPDATE turns SET completed_at='2000-01-01T00:00:00+00:00' WHERE id='turn-1'")
        self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")
        self.store.defer_terminal_delivery("conversation-1", "turn-1", live_observed=True)
        self.assertEqual(self.store.take_deferred_terminal_deliveries("conversation-1"), [])

    def test_legacy_confirmed_native_phase_keeps_intent_on_reopen(self):
        for label in ["native_thread_deleted", "native_thread_absent"]:
            with self.subTest(label=label):
                self.store.set_conversation_state("conversation-1", "idle", recovery_state=label, native_delete_pending=False)
                self.store.close()
                self.store = ConversationStore(Path(self.tmp.name) / "conversations.sqlite3")
                self.assertTrue(self.store.get_conversation("conversation-1")["native_delete_pending"])
                self.assertFalse(self.store.source_delivery_is_eligible("conversation-1"))

    def test_markerless_partial_first_boot_restores_owner_column(self):
        path = Path(self.tmp.name) / "partial.sqlite3"
        partial = ConversationStore(path)
        partial.close()
        database = sqlite3.connect(path)
        try:
            database.execute(
                "ALTER TABLE conversations DROP COLUMN owner_account_id"
            )
            database.execute(
                "DELETE FROM schema_meta WHERE key IN "
                "('schema_version', 'web_push_schema_version')"
            )
            database.commit()
        finally:
            database.close()
        recovered = ConversationStore(path)
        try:
            columns = {
                row["name"]
                for row in recovered._db.execute(
                    "PRAGMA table_info(conversations)"
                ).fetchall()
            }
            version = recovered._db.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            self.assertIn("owner_account_id", columns)
            extension = recovered._db.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'web_push_schema_version'"
            ).fetchone()["value"]
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertEqual(extension, str(WEB_PUSH_SCHEMA_VERSION))
        finally:
            recovered.close()

    def test_invalid_delivery_disables_subscription(self):
        record = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT, origin="https://workbench.test", **_KEYS,
        )
        self.store.prepare_web_push_deliveries("conversation-1", "turn-1", "admin-a")
        self.assertTrue(self.store.claim_web_push_delivery("turn-1", record["id"]))
        self.store.record_web_push_delivery("turn-1", record["id"], state="invalid", detail="http_410")
        self.assertEqual(self.store.web_push_subscriptions_for_account("admin-a"), [])

    def test_same_account_can_update_and_use_multiple_devices(self):
        first = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT,
            origin="https://workbench.test", **_KEYS,
        )
        second_endpoint = _ENDPOINT + "-second-device"
        second = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=second_endpoint,
            origin="https://workbench.test", **_KEYS,
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(self.store.disable_web_push_subscription(
            account_id="admin-b", endpoint=_ENDPOINT
        ))
        self.assertTrue(self.store.disable_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT
        ))
        updated_keys = dict(_KEYS)
        updated_keys["auth"] = base64.urlsafe_b64encode(
            b"b" * 16
        ).rstrip(b"=").decode()
        updated = self.store.upsert_web_push_subscription(
            account_id="admin-a", endpoint=_ENDPOINT,
            origin="https://workbench.test", **updated_keys,
        )
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["auth"], updated_keys["auth"])
        self.assertEqual(len(
            self.store.web_push_subscriptions_for_account("admin-a")
        ), 2)
        deliveries = self.store.prepare_web_push_deliveries(
            "conversation-1", "turn-1", "admin-a"
        )
        self.assertEqual(len(deliveries), 2)

class WebPushSecurityAndDispatchTests(unittest.TestCase):
    def test_private_key_requires_regular_0600_and_reads_valid_p256(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "vapid.pem"
            key = ec.generate_private_key(ec.SECP256R1())
            path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            path.chmod(0o600)
            self.assertTrue(web_push._read_private_key(path).startswith(b"-----BEGIN"))
            with mock.patch.object(web_push, "_read_private_key", return_value=path.read_bytes()):
                self.assertEqual(len(web_push.public_key_b64url()), 87)
            path.chmod(0o644)
            with self.assertRaises(web_push.WebPushError):
                web_push._read_private_key(path)
            path.chmod(0o600)
            linked = Path(root) / "linked.pem"
            os.link(path, linked)
            with self.assertRaises(web_push.WebPushError):
                web_push._read_private_key(path)
            linked.unlink()
            path.unlink()
            path.symlink_to("/etc/passwd")
            with self.assertRaises(web_push.WebPushError):
                web_push._read_private_key(path)

    def test_sender_accepts_flat_store_delivery_and_sets_timeout(self):
        import sys
        import types
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        captured = {}
        class FakeResponse:
            status_code = 201
        class FakeError(Exception):
            response = None
        fake = types.ModuleType("pywebpush")
        fake.WebPushException = FakeError
        fake_vapid = types.ModuleType("py_vapid")
        class FakeVapid:
            def __init__(self, *, private_key):
                self.private_key = private_key
        fake_vapid.Vapid = FakeVapid
        def sender(**kwargs):
            captured.update(kwargs)
            return FakeResponse()
        fake.webpush = sender
        # prepare_web_push_deliveries returns this exact flat database shape;
        # the sender must reconstruct the browser's nested keys envelope.
        subscription = {
            "endpoint": _ENDPOINT,
            "origin": "https://workbench.test:9443",
            **_KEYS,
        }
        private_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with mock.patch.dict(
            sys.modules, {"pywebpush": fake, "py_vapid": fake_vapid}
        ), mock.patch.object(
            web_push, "_read_private_key", return_value=private_pem
        ):
            self.assertTrue(web_push.runtime_available())
            self.assertEqual(web_push.send(subscription, "{}"), ("sent", "http_201"))
        self.assertEqual(captured["timeout"], 10)
        self.assertEqual(
            captured["vapid_claims"]["sub"], "https://workbench.test"
        )

    def test_terminal_without_subscription_is_marked_and_never_backfilled(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConversationStore(Path(root) / "db.sqlite")
            try:
                store.create_conversation("conversation-2", title="x", cwd="/tmp", settings={}, completion_notification=True)
                store.bind_thread("conversation-2", {"id": "thread-2", "status": "active"}, server_version="x")
                store.begin_command("conversation-2", "command-2", kind="start", text="x")
                store.bind_turn("conversation-2", {"id": "turn-2", "status": "completed"}, command_id="command-2")
                self.assertEqual(store.prepare_web_push_deliveries("conversation-2", "turn-2", "admin-a"), [])
                store.upsert_web_push_subscription(account_id="admin-a", endpoint=_ENDPOINT, origin="https://workbench.test", **_KEYS)
                self.assertEqual(store.prepare_web_push_deliveries("conversation-2", "turn-2", "admin-a"), [])
                marker = store._db.execute("SELECT account_id FROM web_push_dispatches WHERE turn_id='turn-2'").fetchone()
                self.assertEqual(marker["account_id"], "admin-a")
            finally:
                store.close()

    def test_owner_must_remain_enabled_admin(self):
        from engine.conversation_service import ConversationService
        with mock.patch("shared.security.load_accounts", return_value={
            "owner": {"role": "admin", "enabled": False},
            "other": {"role": "admin", "enabled": True},
        }):
            self.assertIsNone(ConversationService._web_push_owner({"owner_account_id": "owner"}))
        with mock.patch("shared.security.load_accounts", return_value={
            "owner": {"role": "admin", "enabled": True},
        }):
            self.assertEqual(ConversationService._web_push_owner({"owner_account_id": "owner"}), "owner")


class WebPushServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_subscription_stops_before_body_or_store(self):
        from fastapi.responses import RedirectResponse
        from routes import web_push as web_push_routes

        denied = RedirectResponse("/login", 303)
        request = mock.Mock()
        request.body = mock.AsyncMock(side_effect=AssertionError("body read"))
        with (
            mock.patch.object(
                web_push_routes, "require_auth", return_value=denied
            ),
            mock.patch.object(web_push_routes, "conversation_service") as service,
        ):
            response = await web_push_routes.subscribe(request)
        self.assertIs(response, denied)
        request.body.assert_not_awaited()
        service.assert_not_called()

    def test_subscription_mutation_origin_must_match_request_origin(self):
        from starlette.requests import Request
        from routes import web_push as web_push_routes

        def request(origin):
            return Request({
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "server": ("workbench.test", 443),
                "path": "/settings/web-push/subscribe",
                "query_string": b"",
                "headers": [
                    (b"host", b"workbench.test"),
                    (b"origin", origin.encode("ascii")),
                ],
            })

        self.assertEqual(
            web_push_routes._change_origin(request("https://workbench.test")),
            "https://workbench.test",
        )
        with self.assertRaises(web_push.WebPushError):
            web_push_routes._change_origin(request("https://evil.test"))

    async def test_terminal_payload_uses_neutral_status_and_direct_final_text(self):
        from engine.conversation_service import ConversationService

        with tempfile.TemporaryDirectory() as root:
            store = ConversationStore(Path(root) / "db.sqlite")
            try:
                store.create_conversation(
                    "conversation-3",
                    title="我的对话标题",
                    cwd="/tmp",
                    settings={},
                    completion_notification=True,
                    owner_account_id="admin-a",
                )
                store.bind_thread(
                    "conversation-3",
                    {"id": "thread-3", "status": "active"},
                    server_version="fixture",
                )
                store.begin_command(
                    "conversation-3", "command-3", kind="start", text="x"
                )
                store.bind_turn(
                    "conversation-3",
                    {"id": "turn-3", "status": "inProgress"},
                    command_id="command-3",
                )
                store.apply_notification({
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-3",
                        "turn": {
                            "id": "turn-3",
                            "status": "completed",
                            "items": [{
                                "id": "answer-3",
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "  这是最终   回复  ",
                            }],
                        },
                    },
                })
                store.upsert_web_push_subscription(
                    account_id="admin-a",
                    endpoint=_ENDPOINT,
                    origin="https://workbench.test",
                    **_KEYS,
                )
                service = ConversationService(store, mock.Mock(), mock.Mock())
                with (
                    mock.patch(
                        "shared.security.load_accounts",
                        return_value={
                            "admin-a": {"role": "admin", "enabled": True}
                        },
                    ),
                    mock.patch(
                        "shared.web_push.send", return_value=("sent", "http_201")
                    ) as sender,
                ):
                    await service._deliver_web_push_notification(
                        "conversation-3", "turn-3", store.snapshot("conversation-3")
                    )
                payload = json.loads(sender.call_args.args[1])
                self.assertEqual(payload["title"], "回复已完成")
                self.assertEqual(payload["final_text"], "这是最终 回复")
            finally:
                store.close()

    async def test_direct_subscribe_fails_closed_when_sender_is_unavailable(self):
        from routes import web_push as web_push_routes

        request = mock.Mock()
        with (
            mock.patch.object(
                web_push_routes, "_admin", return_value=("admin-a", None)
            ),
            mock.patch.object(
                web_push_routes,
                "_change_origin",
                return_value="https://workbench.test",
            ),
            mock.patch.object(
                web_push_routes,
                "configured_public_key_b64url",
                side_effect=web_push.WebPushError("not configured"),
            ),
        ):
            response = await web_push_routes.subscribe(request)
        self.assertEqual(response.status_code, 503)

    def test_service_worker_is_non_silent_and_navigates_exact_deep_link(self):
        import shared.html_lib as hlib
        from routes.misc import service_worker

        source = service_worker().body.decode("utf-8")
        self.assertIn("silent:false", source)
        self.assertIn("icon:i", source)
        self.assertIn(".slice(0,48)", source)
        self.assertIn("new URL('static/icon-192.png',self.registration.scope)", source)
        self.assertIn("tag:'codex-turn-'+r", source)
        self.assertIn("renotify:false", source)
        self.assertIn("w.navigate(u)", source)
        self.assertIn("clients.openWindow(u)", source)
        self.assertIn("?turn=", source)
        self.assertIn("p+'/conversation/'+c+'?turn='+t", source)
        self.assertIn("self.registration.scope", source)
        self.assertIn("[0-9a-f]{40,}", source)
        settings_source = Path("static/web-push-settings.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("var appPrefix", settings_source)
        self.assertIn("route('/sw.js')", settings_source)
        self.assertIn("route('/settings/web-push/subscribe')", settings_source)
        self.assertIn("route('/settings/web-push/unsubscribe')", settings_source)
        template = hlib.templates.get_template("notifications.html")
        self.assertNotIn(
            'id="web-push-toggle"', template.render(web_push_admin=False)
        )
        self.assertIn(
            'id="web-push-toggle"', template.render(web_push_admin=True)
        )


if __name__ == "__main__":
    unittest.main()
