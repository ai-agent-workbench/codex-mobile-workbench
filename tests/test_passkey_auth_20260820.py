"""Passkey phase-B security and compatibility tests.

The WebAuthn cryptographic adapter is mocked here because the production
dependency is intentionally not installed into the running venv.  A separate
isolated browser ceremony verifies the pinned library and virtual authenticator.
"""
import base64
import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.websockets import WebSocketState


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class FakeRequest:
    def __init__(
        self, cookies=None, path="/home", origin="https://passkey.example.test",
        query_params=None,
    ):
        self.cookies = cookies or {}
        self.client = SimpleNamespace(host="192.0.2.10")
        self.url = SimpleNamespace(path=path)
        self.query_params = query_params or {}
        self.headers = {"origin": origin, "user-agent": "test-browser"}


class FakeJSONRequest(FakeRequest):
    def __init__(self, payload=None, **kwargs):
        super().__init__(**kwargs)
        self._raw = json.dumps(payload or {}).encode("utf-8")
        self.headers["content-length"] = str(len(self._raw))

    async def body(self):
        return self._raw


class PasskeyStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in (
                "CODEX_WEB_STATE_DIR", "CODEX_WEB_PASSKEY_ENABLED",
                "CODEX_WEB_PASSKEY_RP_ID", "CODEX_WEB_PASSKEY_ORIGIN",
            )
        }
        # The desktop app points TMPDIR at a Windows-mounted directory where
        # POSIX chmod is not meaningful; use WSL /tmp to verify 0600 properly.
        cls.state = Path(tempfile.mkdtemp(prefix="codex-passkey-test-", dir="/tmp"))
        os.environ["CODEX_WEB_STATE_DIR"] = str(cls.state)
        os.environ["CODEX_WEB_PASSKEY_ENABLED"] = "1"
        os.environ["CODEX_WEB_PASSKEY_RP_ID"] = "passkey.example.test"
        os.environ["CODEX_WEB_PASSKEY_ORIGIN"] = "https://passkey.example.test"
        accounts = {
            "admin": {"role": "admin", "enabled": True},
            "store": {"role": "uploader", "enabled": True},
        }
        (cls.state / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
        (cls.state / "totp_enabled").touch()

        import shared.config as config
        importlib.reload(config)
        import shared.security as security
        importlib.reload(security)
        import shared.passkey_auth as passkey
        importlib.reload(passkey)
        importlib.reload(security)
        import routes.auth as auth
        importlib.reload(auth)
        import routes.passkey as passkey_routes
        importlib.reload(passkey_routes)
        # routes.ws binds the config paths and logged_in helper at import time.
        # Full-suite discovery may import it before this class switches to the
        # temporary state directory, so reload it here as part of isolation.
        import routes.ws as ws_routes
        importlib.reload(ws_routes)
        cls.config = config
        cls.security = security
        cls.passkey = passkey
        cls.auth = auth
        cls.passkey_routes = passkey_routes
        cls.ws_routes = ws_routes
        cls._notify = mock.patch("shared.security.notify_message")
        cls._notify.start()

    @classmethod
    def tearDownClass(cls):
        cls._notify.stop()
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shared.config as config
        import shared.passkey_auth as passkey
        import shared.security as security
        import routes.auth as auth
        import routes.passkey as passkey_routes
        import routes.ws as ws_routes
        importlib.reload(config)
        importlib.reload(passkey)
        importlib.reload(security)
        importlib.reload(auth)
        importlib.reload(passkey_routes)
        importlib.reload(ws_routes)

    def setUp(self):
        self.now = int(time.time())
        self.passkey._FINISH_RATE_BUCKETS.clear()
        self.config.PASSKEY_DB.unlink(missing_ok=True)
        for suffix in ("-journal", "-shm", "-wal"):
            Path(str(self.config.PASSKEY_DB) + suffix).unlink(missing_ok=True)

    def _verified_registration(self, credential_id=b"credential-1", sign_count=0):
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"public-key",
            sign_count=sign_count,
            credential_device_type=SimpleNamespace(value="singleDevice"),
            credential_backed_up=False,
            aaguid="00000000-0000-0000-0000-000000000000",
        )

    def _verified_authentication(self, credential_id=b"credential-1", sign_count=0):
        return SimpleNamespace(
            credential_id=credential_id,
            new_sign_count=sign_count,
            credential_device_type=SimpleNamespace(value="singleDevice"),
            credential_backed_up=False,
        )

    def _register(self, now=None):
        now = self.now if now is None else now
        p = self.passkey
        with mock.patch.object(p, "_now", return_value=now), \
             mock.patch.object(p, "_registration_options", return_value={"challenge": "test"}), \
             mock.patch.object(p, "_verified_registration", return_value=self._verified_registration()):
            start = p.begin_registration("admin", "binding")
            result = p.finish_registration(
                start.selector, {"response": {"transports": ["internal"]}},
                "binding", "source",
            )
        return result

    def _assertion_payload(self, user_handle: bytes, credential_id=b"credential-1"):
        return {
            "id": _b64(credential_id),
            "rawId": _b64(credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(b"client"),
                "authenticatorData": _b64(b"auth"),
                "signature": _b64(b"signature"),
                "userHandle": _b64(user_handle),
            },
        }

    def _handle(self):
        conn = self.passkey._connect()
        try:
            row = conn.execute("SELECT user_handle FROM passkey_users WHERE account='admin'").fetchone()
            return bytes(row["user_handle"])
        finally:
            conn.close()

    def test_registration_creates_private_db_and_revocable_session(self):
        result = self._register()
        self.assertEqual(result.account, "admin")
        session = self.passkey.session_from_token(result.token)
        self.assertIsNotNone(session)
        self.assertEqual(session.account, "admin")
        self.assertEqual(self.config.PASSKEY_DB.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.passkey.active_credential_exists("admin"))

    def test_registration_flow_is_one_time_and_bound_to_legacy_cookie(self):
        p = self.passkey
        with mock.patch.object(p, "_now", return_value=self.now), \
             mock.patch.object(p, "_registration_options", return_value={}):
            start = p.begin_registration("admin", "correct")
        with self.assertRaisesRegex(p.PasskeyError, "flow_binding_mismatch"):
            p.finish_registration(start.selector, {}, "wrong", "source")
        with mock.patch.object(p, "_now", return_value=self.now + 1), \
             mock.patch.object(p, "_verified_registration", return_value=self._verified_registration()):
            p.finish_registration(start.selector, {"response": {}}, "correct", "source")
        with self.assertRaisesRegex(p.PasskeyError, "flow_"):
            p.finish_registration(start.selector, {"response": {}}, "correct", "source")

    def test_failed_signature_still_consumes_flow(self):
        p = self.passkey
        with mock.patch.object(p, "_now", return_value=self.now), \
             mock.patch.object(p, "_registration_options", return_value={}):
            start = p.begin_registration("admin", "binding")
        with mock.patch.object(
            p, "_verified_registration", side_effect=p.PasskeyError("registration_rejected"),
        ):
            with self.assertRaisesRegex(p.PasskeyError, "registration_rejected"):
                p.finish_registration(start.selector, {}, "binding", "source")
        with self.assertRaisesRegex(p.PasskeyError, "flow_"):
            p.finish_registration(start.selector, {}, "binding", "source")

    def test_expired_flow_fails_closed(self):
        p = self.passkey
        with mock.patch.object(p, "_now", return_value=self.now), \
             mock.patch.object(p, "_registration_options", return_value={}):
            start = p.begin_registration("admin", "binding")
        with mock.patch.object(
            p, "_now", return_value=self.now + self.config.PASSKEY_CHALLENGE_MAX_AGE + 1,
        ):
            with self.assertRaisesRegex(p.PasskeyError, "flow_expired"):
                p.finish_registration(start.selector, {}, "binding", "source")

    def test_only_one_active_credential(self):
        self._register()
        with self.assertRaisesRegex(self.passkey.PasskeyError, "credential_exists"):
            self.passkey.begin_registration("admin", "binding")

    def test_authentication_replaces_old_session_and_checks_user_handle(self):
        first = self._register()
        p = self.passkey
        handle = self._handle()
        with mock.patch.object(p, "_now", return_value=self.now + 100), \
             mock.patch.object(p, "_authentication_options", return_value={}):
            start = p.begin_authentication()
        wrong = self._assertion_payload(b"wrong-handle")
        with self.assertRaisesRegex(p.PasskeyError, "identity_mismatch"):
            p.finish_authentication(start.selector, wrong, "source")

        with mock.patch.object(p, "_now", return_value=self.now + 101), \
             mock.patch.object(p, "_authentication_options", return_value={}):
            start = p.begin_authentication()
        with mock.patch.object(p, "_now", return_value=self.now + 102), \
             mock.patch.object(p, "_verified_authentication", return_value=self._verified_authentication()):
            second = p.finish_authentication(
                start.selector, self._assertion_payload(handle), "source-2",
            )
        self.assertIn(first.session_id, second.revoked_session_ids)
        self.assertIsNone(p.session_from_token(first.token))
        self.assertIsNotNone(p.session_from_token(second.token))
        self.assertEqual(p.maintenance_status("admin")["active_sessions"], 1)

    def test_positive_counter_change_race_is_rejected(self):
        self._register()
        p = self.passkey
        handle = self._handle()
        with mock.patch.object(p, "_now", return_value=self.now + 100), \
             mock.patch.object(p, "_authentication_options", return_value={}):
            start = p.begin_authentication()

        def race(*args, **kwargs):
            with p._transaction() as conn:
                conn.execute(
                    "UPDATE passkey_credentials SET sign_count=2 WHERE credential_id=?",
                    (b"credential-1",),
                )
            return self._verified_authentication(sign_count=2)

        with mock.patch.object(p, "_verified_authentication", side_effect=race):
            with self.assertRaisesRegex(p.PasskeyError, "counter_changed"):
                p.finish_authentication(start.selector, self._assertion_payload(handle), "source")

    def test_positive_counter_rollback_is_rejected_explicitly(self):
        self._register()
        p = self.passkey
        handle = self._handle()
        with p._transaction() as conn:
            conn.execute(
                "UPDATE passkey_credentials SET sign_count=5 WHERE credential_id=?",
                (b"credential-1",),
            )
        with mock.patch.object(p, "_authentication_options", return_value={}):
            start = p.begin_authentication()
        with mock.patch.object(
            p, "_verified_authentication",
            return_value=self._verified_authentication(sign_count=4),
        ):
            with self.assertRaisesRegex(p.PasskeyError, "counter_rollback"):
                p.finish_authentication(start.selector, self._assertion_payload(handle), "source")

    def test_multiple_enabled_admins_invalidate_passkey_fail_closed(self):
        result = self._register()
        accounts_path = self.config.ACCOUNTS_FILE
        original = accounts_path.read_text(encoding="utf-8")
        accounts = json.loads(original)
        accounts["admin-2"] = {"role": "admin", "enabled": True}
        accounts_path.write_text(json.dumps(accounts), encoding="utf-8")
        try:
            self.assertIsNone(self.passkey.session_from_token(result.token))
            with self.assertRaisesRegex(self.passkey.PasskeyError, "account_unavailable"):
                self.passkey.begin_authentication()
        finally:
            accounts_path.write_text(original, encoding="utf-8")

    def test_schema_enforces_one_active_passkey_globally(self):
        self._register()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.passkey._transaction() as conn:
                conn.execute(
                    """INSERT INTO passkey_credentials(
                           account,user_handle,credential_id,public_key,sign_count,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("admin-2", b"other-handle", b"other-credential", b"key", 0, self.now),
                )

    def test_idle_and_absolute_session_expiry_are_server_side(self):
        result = self._register(now=self.now)
        with mock.patch.object(
            self.passkey, "_now",
            return_value=self.now + self.config.PASSKEY_SESSION_IDLE_AGE + 1,
        ):
            self.assertIsNone(self.passkey.session_from_token(result.token))
        conn = self.passkey._connect()
        try:
            row = conn.execute(
                "SELECT revoked_reason FROM admin_sessions WHERE token_hash=?",
                (result.session_id,),
            ).fetchone()
            self.assertEqual(row["revoked_reason"], "expired")
        finally:
            conn.close()

    def test_recovery_code_is_one_time_and_replacement_revokes_everything(self):
        result = self._register()
        code, session_ids = self.passkey.issue_registration_authorization(
            "admin", replace=True,
        )
        self.assertIn(result.session_id, session_ids)
        self.assertFalse(self.passkey.active_credential_exists("admin"))
        self.assertEqual(self.passkey.consume_registration_authorization(code), "admin")
        with self.assertRaisesRegex(self.passkey.PasskeyError, "authorization_invalid"):
            self.passkey.consume_registration_authorization(code)

    def test_security_guard_accepts_passkey_but_rejects_archived_employee_cookies(self):
        result = self._register()
        request = FakeRequest({self.config.PASSKEY_SESSION_COOKIE: result.token})
        self.assertEqual(self.security.logged_in(request), "admin")
        self.assertIsNone(self.security.require_auth(request))
        for payload in (
            {"n": "store", "r": "uploader", "ts": int(time.time())},
            {"n": "store", "r": "admin", "ts": int(time.time())},
            "store",
        ):
            token = self.security.signer().dumps(payload)
            for path in ("/home", "/files", "/upload", "/operations", "/ws"):
                employee = FakeRequest({"codex_web_session": token}, path=path)
                self.assertIsNone(self.security.logged_in(employee))
                self.assertEqual(self.security.require_auth(employee).status_code, 403)

    def test_passkey_logout_requires_same_origin_and_revokes_server_session(self):
        result = self._register()
        cookies = {self.config.PASSKEY_SESSION_COOKIE: result.token}
        bad = FakeRequest(cookies, path="/logout", origin="https://evil.example")
        self.assertEqual(self.auth.logout(bad).status_code, 403)
        self.assertIsNotNone(self.passkey.session_from_token(result.token))

        good = FakeRequest(cookies, path="/logout")
        with mock.patch("routes.ws.close_passkey_sessions"):
            response = self.auth.logout(good)
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.passkey.session_from_token(result.token))

    def test_legacy_logout_needs_no_new_origin_header(self):
        token = self.security.signer().dumps({
            "n": "store", "r": "uploader", "ts": int(time.time()),
        })
        request = FakeRequest({"codex_web_session": token}, path="/logout", origin="")
        request.headers = {}
        self.assertEqual(self.auth.logout(request).status_code, 303)

    def test_admin_logout_uses_same_origin_fetch(self):
        script = (Path(__file__).parents[1] / "static" / "behavior.js").read_text()
        self.assertIn("pathname === '/logout'", script)
        self.assertIn("credentials: 'same-origin'", script)
        self.assertIn("method: 'POST'", script)

    def test_configuration_requires_exact_https_origin(self):
        self.assertEqual(self.passkey.configuration_error(), "")
        with mock.patch.object(self.passkey, "PASSKEY_ORIGIN", "http://passkey.example.test"):
            self.assertEqual(self.passkey.configuration_error(), "invalid_origin")
        with mock.patch.object(self.passkey, "PASSKEY_ORIGIN", "https://sub.passkey.example.test"):
            self.assertEqual(self.passkey.configuration_error(), "invalid_origin")

    def test_candidate_page_matches_approved_compact_copy(self):
        response = self.passkey_routes.passkey_page(FakeRequest(path="/auth/passkey"))
        html = response.body.decode("utf-8")
        self.assertIn("管理员登录", html)
        self.assertIn("轻触验证", html)
        self.assertIn("fingerprint-icon", html)
        for rejected in (
            "管理员手机", "网关密码", "动态验证码", "候选入口", "旧登录仍保留",
            "首次绑定需", "换手机或丢失手机", "高风险 Windows", "单独要求 PIN",
        ):
            self.assertNotIn(rejected, html)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_candidate_page_offers_separate_admin_pwa(self):
        response = self.passkey_routes.passkey_page(FakeRequest(path="/auth/passkey"))
        html = response.body.decode("utf-8")
        self.assertIn('id="admin-pwa-manifest" rel="manifest"', html)
        self.assertIn('encodeURIComponent(p)', html)
        # Actual shell registration (including updateViaCache) is covered by
        # check_pwa_startup_playwright.js, not a fixed JavaScript token here.

    def test_admin_pwa_manifest_uses_only_trusted_candidate_entry(self):
        request = FakeRequest(
            path="/auth/passkey", query_params={
                "pwa": "manifest", "entry": "/candidate-test-123456789-Fn",
            },
        )
        request.client.host = "127.0.0.1"
        response = self.passkey_routes.passkey_page(request)
        content = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(content["id"], "/auth/passkey-pwa")
        self.assertEqual(content["start_url"], "/candidate-test-123456789-Fn")
        self.assertEqual(content["scope"], "/")
        self.assertEqual(response.headers["cache-control"], "no-store")

        request.query_params["entry"] += "/recover"
        recovered = json.loads(self.passkey_routes.passkey_page(request).body)
        self.assertEqual(recovered["start_url"], "/candidate-test-123456789-Fn")

        request.client.host = "192.0.2.10"
        self.assertEqual(self.passkey_routes.passkey_page(request).status_code, 200)
        for rejected in ("/auth/passkey", "https://evil.example/path", "/short"):
            request.query_params["entry"] = rejected
            self.assertEqual(self.passkey_routes.passkey_page(request).status_code, 404)

    def test_admin_pwa_install_page_does_not_redirect_existing_session(self):
        result = self._register()
        cookies = {self.config.PASSKEY_SESSION_COOKIE: result.token}
        normal = self.passkey_routes.passkey_page(FakeRequest(cookies, path="/auth/passkey"))
        self.assertEqual(normal.status_code, 303)
        install = self.passkey_routes.passkey_page(FakeRequest(
            cookies, path="/auth/passkey", query_params={"pwa": "install"},
        ))
        self.assertEqual(install.status_code, 200)
        self.assertIn("admin-pwa-manifest", install.body.decode("utf-8"))

    def test_route_options_sets_strict_one_time_flow_cookie(self):
        request = FakeJSONRequest(path="/auth/passkey/login/options")
        start = self.passkey.CeremonyStart("selector-secret", {"challenge": "public"})
        with mock.patch.object(self.passkey_routes, "begin_authentication", return_value=start), \
             mock.patch.object(self.passkey_routes, "audit"):
            response = asyncio.run(self.passkey_routes.authentication_options(request))
        self.assertEqual(response.status_code, 200)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(self.config.PASSKEY_FLOW_COOKIE + "=selector-secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_admin_session_cookie_supports_android_pwa_cold_launch(self):
        from starlette.responses import Response

        response = Response()
        self.passkey_routes._set_session_cookie(response, "session-token")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(self.config.PASSKEY_SESSION_COOKIE + "=session-token", cookie)
        self.assertIn("Max-Age=" + str(self.config.PASSKEY_SESSION_ABSOLUTE_AGE), cookie)
        self.assertIn("expires=", cookie.lower())
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)

    def test_route_rejects_cross_origin_before_starting_ceremony(self):
        request = FakeJSONRequest(
            path="/auth/passkey/login/options", origin="https://evil.example",
        )
        with mock.patch.object(self.passkey_routes, "begin_authentication") as begin, \
             mock.patch.object(self.passkey_routes, "audit"):
            response = asyncio.run(self.passkey_routes.authentication_options(request))
        self.assertEqual(response.status_code, 400)
        begin.assert_not_called()

    def test_internal_session_check_is_loopback_and_passkey_only(self):
        result = self._register()
        request = FakeRequest({self.config.PASSKEY_SESSION_COOKIE: result.token})
        request.client.host = "127.0.0.1"
        response = self.passkey_routes.internal_admin_session(request)
        self.assertEqual(response.status_code, 204)
        request.client.host = "192.0.2.10"
        self.assertEqual(
            self.passkey_routes.internal_admin_session(request).status_code, 403,
        )

    def test_websocket_manager_closes_only_matching_revoked_session(self):
        from routes.ws import ConnectionManager

        class Socket:
            def __init__(self):
                self.accepted = False
                self.closed = None
            async def accept(self):
                self.accepted = True
            async def close(self, code):
                self.closed = code

        async def exercise():
            manager = ConnectionManager()
            first, second = Socket(), Socket()
            await manager.connect(first, "session-1")
            await manager.connect(second, "session-2")
            await manager.close_sessions({"session-1"})
            return manager, first, second

        manager, first, second = asyncio.run(exercise())
        self.assertEqual(first.closed, 4401)
        self.assertIsNone(second.closed)
        self.assertNotIn(first, manager._connections)
        self.assertIn(second, manager._connections)

    def test_websocket_rechecks_revocation_during_continuous_ping(self):
        ws_routes = self.ws_routes

        result = self._register()
        passkey = self.passkey

        class Socket(FakeRequest):
            def __init__(self):
                super().__init__({self_cookie: result.token}, path="/ws")
                self.accepted = False
                self.closed = None
                self.client_state = WebSocketState.CONNECTING
                self.application_state = WebSocketState.CONNECTING
                self.pongs = 0
                self.receives = 0

            async def accept(self):
                self.accepted = True
                self.client_state = WebSocketState.CONNECTED
                self.application_state = WebSocketState.CONNECTED

            async def receive_text(self):
                self.receives += 1
                # Establish that an active client can exchange a pong before
                # the separate maintenance process revokes its session.  The
                # SQLite revoke may legitimately take longer than the patched
                # 10 ms recheck interval on the production dependency set.
                if self.receives == 2:
                    passkey.revoke_token(result.token, "external_maintenance")
                await asyncio.sleep(0.002)
                return "ping"

            async def send_text(self, value):
                self.pongs += 1

            async def close(self, code):
                self.closed = code
                self.application_state = WebSocketState.DISCONNECTED

        self_cookie = self.config.PASSKEY_SESSION_COOKIE
        socket = Socket()
        with mock.patch.object(ws_routes, "_AUTH_RECHECK_SECONDS", 0.01):
            asyncio.run(ws_routes.websocket_endpoint(socket))
        self.assertTrue(socket.accepted)
        self.assertEqual(socket.closed, 4401)
        self.assertGreaterEqual(socket.pongs, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
