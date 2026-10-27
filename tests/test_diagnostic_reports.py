import asyncio
import json
import os
import tempfile
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

import routes.diagnostic_reports as route
import shared.diagnostic_reports as storage


def multipart(fields, image=None, content_type="image/png"):
    boundary = "fault-report-boundary"
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    if image is not None:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"screenshot\"; filename=\"private.png\"\r\nContent-Type: {content_type}\r\n\r\n").encode() + image + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(parts)


class FaultReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.fields = {"report_id": "a" * 32, "category": "open_failed", "fault_time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                       "client_page_id": "page-1", "source_path": "/settings", "last_phase": "boot", "client_time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "client_version": "v1"}

    async def call(self, body, boundary, *, origin="https://testserver", declared=None, accept="application/json"):
        sent = False
        async def receive():
            nonlocal sent
            if sent: return {"type":"http.disconnect"}
            sent = True; return {"type":"http.request", "body":body, "more_body":False}
        headers = [(b"host", b"testserver"), (b"origin", origin.encode()), (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                   (b"content-length", str(len(body) if declared is None else declared).encode()), (b"accept", accept.encode())]
        request = Request({"type":"http", "method":"POST", "path":"/settings/diagnostics/report", "headers":headers}, receive)
        return await route.submit_report(request)

    def exercise(self, body, boundary, **kwargs):
        with patch.object(storage, "STATE", self.root), patch.object(route, "logged_in", lambda request: "owner-a"), patch.object(route, "emit", lambda *a, **k: True):
            return asyncio.run(self.call(body, boundary, **kwargs))

    def payload(self, response): return json.loads(response.body)

    def test_auth_origin_and_bounded_body(self):
        get_request = Request({"type":"http", "method":"GET", "path":"/settings/diagnostics/report", "headers":[]})
        with patch.object(route, "require_auth", lambda request: route.HTMLResponse("denied", status_code=401)):
            self.assertEqual(asyncio.run(route.report_form(get_request)).status_code, 401)
        b, data = multipart(self.fields)
        with patch.object(route, "logged_in", lambda request: None):
            self.assertEqual(asyncio.run(self.call(data, b)).status_code, 401)
        self.assertEqual(self.exercise(data, b, origin="https://evil.test").status_code, 403)
        self.assertEqual(self.exercise(data, b, declared=storage.MAX_REQUEST_BYTES+1).status_code, 413)
        self.assertEqual(self.exercise(data + b"x" * (storage.MAX_REQUEST_BYTES + 1), b).status_code, 413)
        oversized = b"\x89PNG\r\n\x1a\n" + b"x" * (storage.MAX_IMAGE_BYTES + 1)
        b, data = multipart(self.fields, oversized)
        self.assertEqual(self.exercise(data, b).status_code, 413)

    def test_empty_browser_file_control_is_no_image_but_named_empty_is_rejected(self):
        b, data = multipart(self.fields)
        marker = f"--{b}\r\nContent-Disposition: form-data; name=\"screenshot\"; filename=\"\"\r\nContent-Type: application/octet-stream\r\n\r\n\r\n--{b}--\r\n".encode()
        data = data[:-len(f"--{b}--\r\n".encode())] + marker
        self.assertEqual(self.exercise(data, b).status_code, 201)
        named = dict(self.fields); named["report_id"] = "e" * 32
        b, data = multipart(named, b"", "image/png")
        self.assertEqual(self.exercise(data, b).status_code, 400)

    def test_report_origin_uses_deployed_address_even_with_passkey_disabled(self):
        b, data = multipart(self.fields)
        import shared.passkey_auth as passkey
        with patch.object(route.config, "PASSKEY_ORIGIN", "https://testserver:8443"), patch.object(passkey, "PASSKEY_ENABLED", False):
            self.assertEqual(self.exercise(data, b, origin="https://testserver:8443").status_code, 201)
            for origin in ("https://testserver", "http://testserver:8443", "https://other.test:8443", "null"):
                self.assertEqual(self.exercise(data, b, origin=origin).status_code, 403)

    def test_optional_device_clock_skew_or_malformed_time_does_not_block_report(self):
        for index, value in enumerate((
            (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat(),
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat(),
            "not-a-timestamp",
        )):
            with self.subTest(client_time=value):
                fields = dict(self.fields, report_id=str(index + 1) * 32, client_time=value)
                b, data = multipart(fields)
                self.assertEqual(self.exercise(data, b).status_code, 201)
                metadata = json.loads((self.root / "diagnostics" / "reports" / fields["report_id"] / "metadata.json").read_text())
                self.assertEqual(metadata["client_time"] is None, value == "not-a-timestamp")
        fields = dict(self.fields, report_id="4" * 32, fault_time="not-a-timestamp")
        b, data = multipart(fields)
        self.assertEqual(self.exercise(data, b).status_code, 400)

    def test_initial_directory_and_lock_errors_return_retryable_html_and_json(self):
        b, data = multipart(self.fields)
        for target in ("_private_dir", "os.open"):
            for accept in ("application/json", "text/html"):
                with self.subTest(target=target, accept=accept), patch("shared.diagnostic_reports." + target, side_effect=OSError("storage unavailable")):
                    response = self.exercise(data, b, accept=accept)
                    self.assertEqual(response.status_code, 503)
                    if accept == "application/json":
                        self.assertEqual(self.payload(response), {"saved": False, "error": "report_storage_unavailable"})
                    else:
                        self.assertIn(b'value="' + b'a' * 32 + b'"', response.body)
                        self.assertIn("未确认保存", response.body.decode())
                        self.assertIn("保存故障报告", response.body.decode())

    def test_save_no_image_and_redacted_metadata_permissions(self):
        b, data = multipart(self.fields)
        response = self.exercise(data, b)
        self.assertEqual(response.status_code, 201)
        report = self.root / "diagnostics" / "reports" / ("a" * 32)
        meta = json.loads((report / "metadata.json").read_text())
        self.assertEqual(meta["owner"], "owner-a"); self.assertFalse(meta["screenshot"]["present"])
        self.assertNotIn("?", meta["source_path"])
        self.assertEqual((report.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((report / "metadata.json").stat().st_mode & 0o777), 0o600)
        self.assertNotIn(str(report), response.body.decode())

    def test_save_image_and_reject_invalid_magic_or_multiple(self):
        png = b"\x89PNG\r\n\x1a\nsmall"
        b, data = multipart(self.fields, png)
        self.assertEqual(self.exercise(data, b).status_code, 201)
        self.fields["report_id"] = "b" * 32
        b, data = multipart(self.fields, b"not-png")
        self.assertEqual(self.exercise(data, b).status_code, 400)
        self.fields["report_id"] = "c" * 32
        b, data = multipart(self.fields, png, "image/svg+xml")
        self.assertEqual(self.exercise(data, b).status_code, 400)
        self.fields["report_id"] = "d" * 32
        b, data = multipart(self.fields, png)
        data = data[:-len(b"--fault-report-boundary--\r\n")] + b"--fault-report-boundary\r\nContent-Disposition: form-data; name=\"screenshot\"; filename=\"two.png\"\r\nContent-Type: image/png\r\n\r\n" + png + b"\r\n--fault-report-boundary--\r\n"
        self.assertEqual(self.exercise(data, b).status_code, 400)

    def test_idempotency_owner_isolation_and_emit_failure_receipt(self):
        b, data = multipart(self.fields)
        with patch.object(storage, "STATE", self.root), patch.object(route, "logged_in", lambda request: "owner-a"), patch.object(route, "emit", lambda *a, **k: False):
            first = asyncio.run(self.call(data, b)); second = asyncio.run(self.call(data, b))
        self.assertEqual(first.status_code, 201); self.assertFalse(self.payload(first)["diagnostics_logged"])
        self.assertEqual(second.status_code, 200); self.assertFalse(self.payload(second)["diagnostics_logged"])
        with patch.object(storage, "STATE", self.root), patch.object(route, "logged_in", lambda request: "owner-b"), patch.object(route, "emit", lambda *a, **k: True):
            self.assertEqual(asyncio.run(self.call(data, b)).status_code, 409)

    def test_reject_bad_fields_capacity_and_storage_link(self):
        query = dict(self.fields); query["source_path"] = "/settings?token=private"
        b, data = multipart(query); self.assertEqual(self.exercise(data, b).status_code, 201)
        stored = json.loads((self.root / "diagnostics" / "reports" / ("a" * 32) / "metadata.json").read_text())
        self.assertEqual(stored["source_path"], "/settings")
        bad = dict(self.fields); bad["report_id"] = "e" * 32; bad["source_path"] = "https://evil.test/private"
        b, data = multipart(bad); self.assertEqual(self.exercise(data, b).status_code, 400)
        unknown = dict(self.fields); unknown["narrative"] = "do not store"
        b, data = multipart(unknown); self.assertEqual(self.exercise(data, b).status_code, 400)
        with patch.object(storage, "MAX_REPORTS", 0):
            capacity = dict(self.fields); capacity["report_id"] = "f" * 32
            b, data = multipart(capacity); self.assertEqual(self.exercise(data, b).status_code, 507)
        outside = self.root / "outside"; outside.mkdir()
        diag = self.root / "diagnostics"
        if diag.exists():
            import shutil; shutil.rmtree(diag)
        diag.symlink_to(outside, target_is_directory=True)
        b, data = multipart(self.fields); self.assertEqual(self.exercise(data, b).status_code, 503)


    def test_concurrent_same_id_different_content_conflicts_under_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        one = storage.validate_fields(self.fields)
        changed = dict(self.fields); changed["category"] = "other"
        two = storage.validate_fields(changed)
        image = storage.image_info(None, None)
        def save(values):
            try:
                return storage.persist_report("9" * 32, "owner-a", values, None, image)
            except storage.ReportError as error:
                return error
        with patch.object(storage, "STATE", self.root), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, (one, two)))
        self.assertEqual(sum(not isinstance(value, storage.ReportError) for value in outcomes), 1)
        errors = [value for value in outcomes if isinstance(value, storage.ReportError)]
        self.assertEqual(len(errors), 1); self.assertEqual(errors[0].status, 409)

    def test_write_failure_never_acknowledges_success(self):
        b, data = multipart(self.fields)
        with patch.object(storage, "STATE", self.root), patch.object(route, "logged_in", lambda request: "owner-a"), patch.object(storage, "_write_private", side_effect=OSError("disk full")):
            response = asyncio.run(self.call(data, b))
        # Disk errors are not reported as a fabricated successful receipt.
        self.assertNotEqual(response.status_code, 201)
        self.assertNotIn(b'"saved":true', response.body)


if __name__ == "__main__": unittest.main()
