"""Regression coverage for the retired employee business portal."""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from fastapi import FastAPI
from starlette.routing import Match

from middleware.rate_limit import RateLimitMiddleware, RateLimiter
from routes.legacy_employee import router as legacy_employee_router
from routes.files import router as files_router


async def asgi_request(app, method, path, *, headers=(), body=b""):
    """Run a small ASGI request and report whether the handler read its body."""
    received = 0
    sent = []

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": list(headers), "client": ("192.0.2.44", 12345),
        "server": ("example.test", 9443),
    }
    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in sent
        if message["type"] == "http.response.body"
    )
    return start, response_body, received


class LegacyEmployeeRetirementTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(legacy_employee_router)

    def request(self, method, path, *, headers=(), body=b""):
        return asyncio.run(asgi_request(
            self.app, method, path, headers=headers, body=body))

    def test_all_common_legacy_methods_return_stable_json_410_without_reading_body(self):
        for path in ("/upload", "/upload/history", "/operations", "/operations/revise"):
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    start, body, received = self.request(
                        method, path,
                        headers=[(b"accept", b"application/json")],
                        body=b"this retired upload must not be read",
                    )
                    headers = dict(start["headers"])
                    self.assertEqual(start["status"], 410)
                    self.assertEqual(headers[b"content-type"].split(b";", 1)[0], b"application/json")
                    self.assertNotIn(b"set-cookie", headers)
                    self.assertEqual(received, 0)
                    if method != "HEAD":
                        self.assertEqual(json.loads(body), {
                            "reason_code": "legacy_employee_portal_retired",
                        })

    def test_browser_receives_short_generic_html_without_business_data(self):
        start, body, received = self.request(
            "POST", "/upload", headers=[(b"accept", b"text/html")], body=b"ignored")
        self.assertEqual(start["status"], 410)
        self.assertEqual(received, 0)
        page = body.decode("utf-8")
        self.assertIn("员工业务入口已迁移", page)
        self.assertIn("钉钉", page)
        self.assertNotIn("毓秀路", page)
        self.assertNotIn("http", page)

    def test_general_files_upload_route_is_still_registered(self):
        self.assertIn("/files/upload", {route.path for route in files_router.routes})

    def test_legacy_login_uses_the_login_limiter(self):
        original_login = RateLimitMiddleware._login
        RateLimitMiddleware._login = RateLimiter(5)
        try:
            async def downstream(scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"passed"})
            middleware = RateLimitMiddleware(downstream)
            for _ in range(5):
                start, _body, _read = asyncio.run(asgi_request(middleware, "POST", "/login/legacy"))
                self.assertEqual(start["status"], 200)
            start, _body, _read = asyncio.run(asgi_request(middleware, "POST", "/login/legacy"))
            self.assertEqual(start["status"], 429)
        finally:
            RateLimitMiddleware._login = original_login

    def test_general_file_upload_limit_remains_but_retired_upload_is_not_rate_limited(self):
        original = RateLimitMiddleware._files
        RateLimitMiddleware._files = RateLimiter(1)
        try:
            async def downstream(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})
            middleware = RateLimitMiddleware(downstream)
            self.assertEqual(asyncio.run(asgi_request(middleware, "POST", "/files/upload"))[0]["status"], 200)
            self.assertEqual(asyncio.run(asgi_request(middleware, "POST", "/files/upload"))[0]["status"], 429)
            retired = RateLimitMiddleware(self.app)
            for _ in range(3):
                start, body, _ = asyncio.run(asgi_request(retired, "POST", "/upload"))
                self.assertEqual(start["status"], 410)
                self.assertEqual(json.loads(body)["reason_code"], "legacy_employee_portal_retired")
        finally:
            RateLimitMiddleware._files = original


if __name__ == "__main__":
    unittest.main()
