import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from routes import files as files_route
from shared.upload_confirmation import load_upload_confirmation


class UploadConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.signer = URLSafeTimedSerializer("test-only-key", salt="codex-web-task-confirm")
        self.now = 2_000_000_000

    def token(self, payload, age):
        with patch("itsdangerous.timed.time.time", return_value=self.now - age):
            return self.signer.dumps(payload)

    def test_age_boundaries_and_existing_token_compatibility(self):
        for age in (-5, -1, 0, 1, 3600):
            with self.subTest(age=age):
                token = self.token({"ok": True}, age)
                with patch("shared.upload_confirmation.time.time", return_value=self.now):
                    self.assertEqual(load_upload_confirmation(self.signer, token), {"ok": True})
        for age in (-6, 3601, 86400):
            with self.subTest(age=age):
                token = self.token({"ok": True}, age)
                with patch("shared.upload_confirmation.time.time", return_value=self.now):
                    with self.assertRaises(SignatureExpired):
                        load_upload_confirmation(self.signer, token)

    def test_subsecond_boundary_matches_original_integer_second_semantics(self):
        token = self.token({"ok": True}, 0)
        for delta, accepted in ((-5.9, False), (-5.0, True), (3600.9, True), (3601.0, False)):
            with self.subTest(delta=delta), patch(
                "shared.upload_confirmation.time.time", return_value=self.now + delta
            ):
                if accepted:
                    self.assertEqual(load_upload_confirmation(self.signer, token), {"ok": True})
                else:
                    with self.assertRaises(SignatureExpired):
                        load_upload_confirmation(self.signer, token)

    def test_signature_and_signing_context_remain_required(self):
        token = self.token({"ok": True}, -1)
        wrong = URLSafeTimedSerializer("test-only-key", salt="different-context")
        for signer, value in ((self.signer, "broken"), (self.signer, "x" + token), (wrong, token)):
            with self.subTest(value=value), patch("shared.upload_confirmation.time.time", return_value=self.now):
                with self.assertRaises(BadSignature):
                    load_upload_confirmation(signer, value)

    def test_other_confirmation_flows_still_reject_future_tokens(self):
        token = self.token({"ok": True}, -1)
        with patch("itsdangerous.timed.time.time", return_value=self.now):
            with self.assertRaises(SignatureExpired):
                self.signer.loads(token, max_age=3600)

    def test_route_installs_file_on_small_rollback_and_keeps_staging_on_rejection(self):
        for age in (-1, -5, -6, 3601):
            with self.subTest(age=age), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                staged = root / "staged"
                staged.write_bytes(b"uploaded data")
                dest = root / "destination"
                dest.mkdir()
                token = self.token({"directory": str(dest), "files": [
                    {"name": "report.txt", "upload_id": "test"}
                ], "return_to": "/files"}, age)
                with (
                    patch.object(files_route, "require_auth", return_value=None),
                    patch.object(files_route, "confirmation_signer", return_value=self.signer),
                    patch.object(files_route, "allowed_path", return_value=dest),
                    patch.object(files_route, "staged_upload", return_value=staged),
                    patch.object(files_route, "audit"),
                    patch("shared.file_utils.FILE_ROOTS", (root,)),
                    patch("shared.upload_confirmation.time.time", return_value=self.now),
                ):
                    if age in (-6, 3601):
                        with self.assertLogs("routes.files", level="WARNING") as logs:
                            response = files_route.upload_confirm(object(), token)
                        self.assertEqual(response.status_code, 400)
                        self.assertTrue(staged.exists())
                        self.assertFalse((dest / "report.txt").exists())
                        self.assertNotIn(token, str(logs.output))
                        self.assertNotIn(str(dest), str(logs.output))
                        self.assertIn("服务器时间" if age == -6 else "1 小时", response.body.decode())
                    else:
                        response = files_route.upload_confirm(object(), token)
                        self.assertEqual(response.status_code, 303)
                        self.assertEqual((dest / "report.txt").read_bytes(), b"uploaded data")
                        self.assertFalse(staged.exists())


if __name__ == "__main__":
    unittest.main()
