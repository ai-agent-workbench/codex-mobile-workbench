"""Isolated administrator-only login, canonical PWA entry and recovery contracts."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from starlette.requests import Request


class AdministratorLoginRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="admin-recovery-test-", dir="/tmp")
        cls.state = Path(cls.tmp.name)
        cls.environment = mock.patch.dict(os.environ, {
            "CODEX_WEB_STATE_DIR": str(cls.state),
            "CODEX_WEB_PASSKEY_ENABLED": "1",
            "CODEX_WEB_PASSKEY_RP_ID": "workbench.example.test",
            "CODEX_WEB_PASSKEY_ORIGIN": "https://workbench.example.test:9443",
        })
        cls.environment.start()
        cls.accounts = {
            "admin": {"role": "admin", "enabled": True},
            "retired": {"role": "uploader", "enabled": True, "landing": str(cls.state / "originals")},
        }
        (cls.state / "accounts.json").write_text(json.dumps(cls.accounts))
        (cls.state / "totp_enabled").touch()
        (cls.state / "totp_secret").write_text("JBSWY3DPEHPK3PXP")
        cls.modules = [importlib.import_module(name) for name in (
            "shared.config", "shared.passkey_auth", "shared.security",
            "shared.login_recovery", "routes.passkey", "routes.auth",
        )]
        for module in cls.modules:
            importlib.reload(module)
        cls.config, cls.passkey, cls.security, cls.recovery, cls.passkey_routes, cls.auth = cls.modules
        cls.notify = mock.patch.object(cls.security, "notify_message")
        cls.notify.start()

    @classmethod
    def tearDownClass(cls):
        cls.notify.stop()
        cls.environment.stop()
        for module in cls.modules:
            importlib.reload(module)
        cls.tmp.cleanup()

    def setUp(self):
        self.security._login_fails.clear()

    def request(self, path="/login", *, method="GET", host="workbench.example.test:9443", headers=None, cookie=""):
        url = urlsplit(path)
        values = {"host": host, "origin": "https://workbench.example.test:9443"}
        values.update(headers or {})
        if cookie:
            values["cookie"] = "codex_web_session=" + cookie
        return Request({
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "https", "path": url.path, "query_string": url.query.encode(),
            "headers": [(key.lower().encode(), value.encode()) for key, value in values.items()],
            "client": ("192.0.2.10", 12345), "server": ("workbench.example.test", 9443),
        })

    def token(self, name="admin", role="admin"):
        return self.security.signer().dumps({"n": name, "r": role, "ts": int(time.time())})

    def test_default_login_is_passkey_even_with_a_legacy_cookie(self):
        with mock.patch.object(self.passkey_routes, "active_credential_exists", return_value=True):
            result = self.auth.login(self.request(cookie=self.token()))
        self.assertEqual(result.status_code, 200)
        self.assertIn('data-mode="login"', result.body.decode())
        self.assertNotIn('action="/login/legacy"', result.body.decode())
        self.assertNotIn("门店密码", result.body.decode())
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_noncanonical_host_uses_only_configured_origin_and_safe_return_target(self):
        target = "/conversation/conversation-1?turn=turn-1"
        result = self.auth.login(self.request(host="192.0.2.55:9443"), target)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"],
            "https://workbench.example.test:9443/login?return_to=%2Fconversation%2Fconversation-1%3Fturn%3Dturn-1")
        result = self.auth.login(self.request(host="attacker.example"), "https://attacker.example/")
        self.assertEqual(result.headers["location"], "https://workbench.example.test:9443/login")
        self.assertEqual(self.auth.login(self.request(host="127.0.0.1:18080")).status_code, 200)

    def test_default_login_manifest_works_and_keeps_existing_admin_pwa_identity(self):
        result = self.auth.login(self.request("/login?pwa=manifest&entry=%2Flogin"))
        payload = json.loads(result.body)
        self.assertEqual(payload["start_url"], "/login")
        self.assertEqual(payload["id"], "/auth/passkey-pwa")
        self.assertEqual(payload["scope"], "/")

    def test_disabled_passkey_never_silently_downgrades_daily_login(self):
        with mock.patch.object(self.auth, "PASSKEY_ENABLED", False):
            self.assertEqual(self.auth.login(self.request()).status_code, 503)
            result = self.auth.finish_legacy_login(self.request("/login/legacy", method="POST", headers={"origin": "https://evil.test"}), self.security.totp().now(), "")
            self.assertEqual(result.status_code, 403)
            self.assertNotIn("set-cookie", result.headers)

    def test_real_app_guard_preserves_typed_denial_with_basic_but_no_app_cookie(self):
        from routes import conversation_api
        request = self.request("/conversation/c-1", method="POST", headers={
            "authorization": "Basic Zml4dHVyZTpmaXh0dXJl", "accept": "text/html",
            "x-codex-original-method": "GET", "x-codex-original-uri": "https://evil.test/",
        })
        result = self.security.require_auth(request)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(json.loads(result.body)["reason_code"], "authentication_required")
        with mock.patch.object(conversation_api, "require_auth", return_value=result):
            self.assertIs(conversation_api._auth(request), result)

    def test_gateway_recovery_preserves_only_safe_navigation_targets(self):
        target = "/conversation/conversation-1?turn=turn-1"
        headers = {"accept": "text/html", "x-codex-original-method": "GET", "x-codex-original-uri": target}
        result = self.auth.login_required(self.request("/login/required", headers=headers))
        self.assertEqual(result.status_code, 303)
        self.assertIn("return_to=%2Fconversation", result.headers["location"])
        for uri in ("https://evil.test/", "//evil.test", "/files?path=/secret", "/conversation/%2e%2e"):
            result = self.auth.login_required(self.request("/login/required", headers={**headers, "x-codex-original-uri": uri}))
            self.assertEqual(result.headers["location"], "/login")

    def test_mutations_api_ws_and_head_never_become_login_html_or_401(self):
        for method, headers in (
            ("POST", {"accept": "text/html"}), ("HEAD", {"accept": "text/html"}),
            ("PUT", {}), ("DELETE", {}), ("OPTIONS", {}),
            ("GET", {"accept": "application/json"}),
            ("GET", {"accept": "text/html", "upgrade": "websocket"}),
            ("GET", {"accept": "text/html", "sec-fetch-mode": "cors"}),
        ):
            with self.subTest(method=method, headers=headers):
                result = self.auth.login_required(self.request("/login/required", headers={
                    **headers, "x-codex-original-method": method,
                    "x-codex-original-uri": "/conversation/c-1",
                }))
                self.assertEqual(result.status_code, 403)
                self.assertEqual(json.loads(result.body)["reason_code"], "authentication_required")
                self.assertNotIn("location", result.headers)
                self.assertNotIn("set-cookie", result.headers)
                self.assertEqual(result.headers["cache-control"], "no-store")

    def test_employee_login_is_gone_and_cannot_create_cookie_even_with_totp(self):
        result = self.auth.finish_login(self.request(method="POST"), account="retired", password="unused", remember="on", code=self.security.totp().now(), return_to="")
        self.assertEqual(result.status_code, 410)
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(json.loads(result.body)["reason_code"], "legacy_employee_portal_retired")
        result = self.auth.finish_login(self.request(method="POST"), account="", password="", remember="", code=self.security.totp().now(), return_to="")
        self.assertEqual(result.status_code, 303)
        self.assertNotIn("set-cookie", result.headers)

    def test_explicit_legacy_login_checks_origin_totp_and_admin_enabled(self):
        request = self.request("/login/legacy", method="POST")
        bad_origin = self.auth.finish_legacy_login(self.request("/login/legacy", method="POST", headers={"origin": "https://evil.test"}), self.security.totp().now(), "")
        self.assertEqual(bad_origin.status_code, 403)
        self.assertNotIn("set-cookie", bad_origin.headers)
        with mock.patch.object(self.auth, "totp") as totp:
            totp.return_value.verify.return_value = False
            denied = self.auth.finish_legacy_login(request, "000000", "")
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("set-cookie", denied.headers)
        allowed = self.auth.finish_legacy_login(request, self.security.totp().now(), "/conversation/c-1?turn=t-1")
        self.assertEqual(allowed.status_code, 303)
        self.assertEqual(allowed.headers["location"], "/conversation/c-1?turn=t-1")
        self.assertIn("HttpOnly", allowed.headers["set-cookie"])
        self.assertIn("Secure", allowed.headers["set-cookie"])
        disabled = {"admin": {"role": "admin", "enabled": False}}
        with mock.patch.object(self.auth, "load_accounts", return_value=disabled):
            denied = self.auth.finish_legacy_login(request, self.security.totp().now(), "")
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("set-cookie", denied.headers)

    def test_status_is_no_store_and_contains_no_identity_or_session_values(self):
        for cookie, expected in (("", "none"), (self.token(), "legacy"), (self.token("retired", "uploader"), "none")):
            result = self.auth.login_status(self.request("/login/status", cookie=cookie))
            self.assertEqual(json.loads(result.body), {"authenticated": expected != "none", "method": expected, "login_url": "/login", "passkey_enabled": True})
            self.assertEqual(result.headers["cache-control"], "no-store")
        with mock.patch.object(self.passkey, "session_from_request", return_value=SimpleNamespace(account="admin")):
            result = self.auth.login_status(self.request("/login/status"))
        self.assertEqual(json.loads(result.body)["method"], "passkey")

    def test_retired_provisioning_cannot_touch_account_store_or_originals(self):
        before = (self.state / "accounts.json").read_bytes()
        script = Path(__file__).parents[1] / "scripts/accounts.py"
        for command in (("add", "new", "store", str(self.state / "new-originals")), ("enable", "retired"), ("reset-password", "retired"), ("set-quota", "retired", "10")):
            result = subprocess.run([sys.executable, str(script), *command], capture_output=True, env=os.environ.copy())
            self.assertEqual(result.returncode, 2)
        self.assertEqual((self.state / "accounts.json").read_bytes(), before)
        self.assertFalse((self.state / "new-originals").exists())


if __name__ == "__main__":
    unittest.main()
