import io
from datetime import datetime, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routes import files as files_route
from shared.config import _configured_paths
from shared.file_utils import safe_upload_filename
from shared.html_lib import return_with_upload_notice, upload_notice, upload_panel
from shared.upload_receipts import create_upload_receipt, load_upload_receipt


class _Upload:
    def __init__(self, name, body=b"hello"):
        self.filename = name
        self.file = io.BytesIO(body)


class _Signer:
    def __init__(self, payload=None):
        self.payload = payload

    def dumps(self, payload):
        self.payload = payload
        return "signed-upload"

    def loads(self, _token, max_age=None, return_timestamp=False):
        if return_timestamp:
            return self.payload, datetime.now(timezone.utc)
        return self.payload


class FileUploadPathTests(unittest.TestCase):
    def test_browser_file_roots_are_environment_configurable(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            with patch.dict(os.environ, {
                "CODEX_WEB_TEST_PATHS": str(first) + os.pathsep + str(second),
            }):
                self.assertEqual(
                    _configured_paths("CODEX_WEB_TEST_PATHS", ("/unused",)),
                    (first.resolve(), second.resolve()),
                )
        with patch.dict(os.environ, {"CODEX_WEB_TEST_PATHS": ""}):
            self.assertEqual(
                _configured_paths(
                    "CODEX_WEB_TEST_PATHS", ("/unused",), allow_empty=True
                ),
                (),
            )

    def test_upload_panel_names_target_and_explains_full_access_boundary(self):
        body = upload_panel("/tmp/Projects/demo", "/return")
        self.assertIn("上传目标", body)
        self.assertIn("/tmp/Projects/demo", body)
        self.assertIn("不是 Full Access 对话的权限边界", body)

    def test_batch_receipt_keeps_redirect_short_and_escapes_all_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "uploads"
            names = [("文件" + str(index) + "-" + ("x" * 180)) for index in range(20)]
            receipt_id = create_upload_receipt(names, directory=staging)
            redirect = return_with_upload_notice("/project/demo?existing=1", "", receipt_id)
            self.assertLess(len(redirect), 100)
            self.assertIn("existing=1", redirect)
            self.assertIn("uploaded_batch=" + receipt_id, redirect)
            self.assertEqual(load_upload_receipt(receipt_id, directory=staging), names)
            self.assertEqual((staging / ("receipt-" + receipt_id + ".json")).stat().st_mode & 0o777, 0o600)

    def test_batch_receipt_invalid_or_unavailable_is_safe_and_uses_expiry_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "uploads"
            with patch("shared.upload_receipts.UPLOADS", staging):
                receipt_id = create_upload_receipt(["<script>alert(1)</script>"])
                body = upload_notice("", receipt_id)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
                self.assertNotIn("<script>alert(1)</script>", body)
                (staging / ("receipt-" + receipt_id + ".json")).write_text("not json")
                self.assertIn("清单已过期", upload_notice("", receipt_id))
                with patch("shared.upload_receipts.Path.open") as open_receipt:
                    self.assertIn("清单已过期", upload_notice("", "../../secret"))
                open_receipt.assert_not_called()
                with patch("shared.upload_receipts.Path.is_file", side_effect=PermissionError):
                    self.assertIn("清单已过期", upload_notice("", receipt_id))

    def test_batch_receipt_expires_without_server_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            receipt_id = create_upload_receipt(["one", "two"], directory=staging)
            created_at = (staging / ("receipt-" + receipt_id + ".json")).stat().st_mtime
            with patch("shared.upload_receipts.UPLOADS", staging), patch(
                "shared.upload_receipts.time.time", return_value=created_at + 86401
            ):
                self.assertIsNone(load_upload_receipt(receipt_id))
                self.assertIn("清单已过期", upload_notice("", receipt_id))

    def test_file_shortcuts_follow_configured_roots(self):
        configured = (Path("/srv/custom-root"),)
        with patch.object(files_route, "FILE_ROOTS", configured):
            body = files_route._file_shortcuts()
        self.assertIn("/srv/custom-root", body)
        self.assertNotIn("/home/workbench/Inbox", body)

    def test_file_page_defaults_to_first_configured_root(self):
        configured = (Path("/srv/custom-root"),)
        with (
            patch.object(files_route, "FILE_ROOTS", configured),
            patch.object(files_route, "require_auth", return_value=None),
            patch.object(files_route, "allowed_path", side_effect=ValueError("checked")) as allowed,
        ):
            files_route.files(object())
        allowed.assert_called_once_with("/srv/custom-root", require_directory=True)

    def test_upload_name_is_a_filename_not_a_path(self):
        self.assertEqual(safe_upload_filename("报告.xlsx"), "报告.xlsx")
        for value in ("../outside.txt", "folder/file.txt", r"C:\\outside.txt", ".", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_upload_filename(value)

    def test_staged_payload_keeps_directory_and_plain_name_separate(self):
        signer = _Signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            destination = root / "destination"
            destination.mkdir()
            (destination / "报告.md").write_text("old")
            with (
                patch.object(files_route, "require_auth", return_value=None),
                patch.object(files_route, "allowed_path", return_value=destination),
                patch.object(files_route, "confirmation_signer", return_value=signer),
                patch.object(files_route, "UPLOADS", staging),
                patch("shared.file_utils.FILE_ROOTS", (root,)),
            ):
                response = files_route.upload(
                    object(), str(destination), [_Upload("报告.md")], "/quick"
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(signer.payload["directory"], str(destination))
        self.assertEqual(signer.payload["files"][0]["name"], "报告.md")
        self.assertNotIn("destination", signer.payload["files"][0])

    def test_confirm_rebuilds_destination_inside_signed_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            destination.mkdir()
            signer = _Signer({
                "directory": str(destination),
                "files": [{
                    "name": "报告.pdf",
                    "upload_id": "abc",
                    "destination": str(root / "outside.pdf"),
                }],
                "return_to": "/files",
            })
            installed = []
            with (
                patch.object(files_route, "require_auth", return_value=None),
                patch.object(files_route, "allowed_path", return_value=destination),
                patch.object(files_route, "confirmation_signer", return_value=signer),
                patch.object(files_route, "staged_upload", return_value=root / "staged"),
                patch.object(files_route, "install_upload",
                             side_effect=lambda source, dest: installed.append((source, dest))),
                patch.object(files_route, "audit"),
                patch("shared.file_utils.FILE_ROOTS", (root,)),
            ):
                response = files_route.upload_confirm(object(), "signed-upload")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(installed[0][1], destination / "报告.pdf")

    def test_confirm_rejects_a_path_name_even_in_a_signed_payload(self):
        signer = _Signer({
            "directory": "/allowed",
            "files": [{"name": "../outside.txt", "upload_id": "abc"}],
            "return_to": "/files",
        })
        with (
            patch.object(files_route, "require_auth", return_value=None),
            patch.object(files_route, "allowed_path", return_value=Path("/allowed")),
            patch.object(files_route, "confirmation_signer", return_value=signer),
            patch.object(files_route, "install_upload") as install,
        ):
            response = files_route.upload_confirm(object(), "signed-upload")
        self.assertEqual(response.status_code, 400)
        install.assert_not_called()
        self.assertIn("上传文件名不能包含路径", response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
