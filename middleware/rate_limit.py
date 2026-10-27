"""Application-layer rate limiting (in-memory — resets on restart).

Per the implementation plan:
- TOTP / login attempts:  5 per minute per IP
- Conversation reads:   90 per minute per IP and conversation
- File and conversation writes: 20 per minute per IP
"""

import math
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


_WINDOW = 60  # seconds


class RateLimiter:
    """Sliding-window counter per key.  Not thread-safe — single async thread."""

    def __init__(self, max_requests: int):
        self._max = max_requests
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        cutoff = now - _WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """Return the conservative whole-second delay for a rejected key."""
        bucket = self._buckets[key]
        if not bucket:
            return 1
        return max(1, math.ceil(bucket[0] + _WINDOW - time.monotonic()))


class RateLimitMiddleware(BaseHTTPMiddleware):
    _login = RateLimiter(5)    # /login POST — per IP (was global "totp")
    _passkey_options = RateLimiter(10)
    _passkey_finish = RateLimiter(5)
    _auth_status = RateLimiter(30)
    _api = RateLimiter(90)     # burst margin; browser coalesces updates to <=24/min/tab
    _files = RateLimiter(20)   # file and conversation writes
    _web_push = RateLimiter(12)  # subscription mutations, per source IP

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        client_ip = request.client.host if request.client else "127.0.0.1"
        limiter: RateLimiter | None = None
        key: str = ""

        if path in ("/setup", "/login", "/login/legacy") and request.method == "POST":
            limiter = self._login
            key = client_ip
        elif path == "/login/status" and request.method == "GET":
            limiter = self._auth_status
            key = client_ip
        elif path in {
            "/auth/passkey/register/options", "/auth/passkey/login/options",
        } and request.method == "POST":
            limiter = self._passkey_options
            key = client_ip
        elif path in {
            "/auth/passkey/register/finish", "/auth/passkey/login/finish",
        } and request.method == "POST":
            limiter = self._passkey_finish
            key = client_ip
        elif (
            path.startswith("/conversation/")
            and path.endswith(("/snapshot", "/view", "/export"))
            and request.method == "GET"
        ):
            limiter = self._api
            key = f"{client_ip}:{path}"
        elif path.startswith("/settings/web-push/") and request.method == "POST":
            limiter = self._web_push
            key = client_ip
        elif path.startswith("/conversation/") and request.method == "POST":
            limiter = self._files
            key = client_ip
        elif path == "/files/upload" or any(
            path.endswith(suffix)
            for suffix in (
                "/edit/write", "/delete/execute", "/move/execute",
            )
        ):
            limiter = self._files
            key = client_ip

        if limiter is not None and not limiter.check(key):
            return JSONResponse(
                {"error": "请求过于频繁，请稍后再试。"},
                status_code=429,
                headers={"Retry-After": str(limiter.retry_after(key))},
            )

        return await call_next(request)
