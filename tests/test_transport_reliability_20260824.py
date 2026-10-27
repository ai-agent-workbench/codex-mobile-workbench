import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from middleware.rate_limit import RateLimitMiddleware, RateLimiter
from routes.ws import ConnectionManager
from starlette.websockets import WebSocketDisconnect, WebSocketState
from starlette.responses import Response


class _Socket:
    def __init__(self, *, blocked: bool = False):
        self.blocked = blocked
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._release = asyncio.Event()
        self.sending = False

    async def accept(self):
        pass

    async def send_text(self, value: str) -> None:
        if self.sending:
            raise AssertionError("overlapping socket writes")
        self.sending = True
        try:
            if self.blocked:
                await self._release.wait()
            self.sent.append(value)
        finally:
            self.sending = False

    async def close(self, code: int) -> None:
        self.close_code = code


class WebSocketBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_socket_times_out_without_holding_other_clients(self):
        manager = ConnectionManager()
        slow = _Socket(blocked=True)
        fast = _Socket()
        await manager.connect(slow)
        await manager.connect(fast)

        from routes import ws
        previous = ws._SEND_TIMEOUT_SECONDS
        ws._SEND_TIMEOUT_SECONDS = 0.01
        try:
            await manager.broadcast("status", {"sequence": 1})
            async with asyncio.timeout(1):
                while slow.close_code is None:
                    await asyncio.sleep(0.001)
        finally:
            ws._SEND_TIMEOUT_SECONDS = previous
            manager.disconnect(fast)

        self.assertEqual(len(fast.sent), 1)
        self.assertNotIn(slow, manager._connections)
        self.assertEqual(slow.close_code, 1013)

    async def test_concurrent_broadcasts_serialize_each_socket(self):
        manager = ConnectionManager()
        socket = _Socket()
        await manager.connect(socket)

        await asyncio.gather(
            manager.broadcast("status", {"sequence": 1}),
            manager.broadcast("status", {"sequence": 2}),
        )

        async with asyncio.timeout(1):
            while len(socket.sent) < 2:
                await asyncio.sleep(0.001)
        self.assertEqual(len(socket.sent), 2)
        manager.disconnect(socket)


class _EndpointSocket:
    def __init__(self, actions=(), *, close_after_accept=False, send_error=False):
        self.actions = list(actions)
        self.close_after_accept = close_after_accept
        self.send_error = send_error
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.accepted = False
        self.receive_calls = 0
        self.close_code = None
        self._wait = asyncio.Event()

    async def accept(self):
        self.accepted = True
        if self.close_after_accept:
            self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        self.receive_calls += 1
        action = self.actions.pop(0) if self.actions else "wait"
        if action == "wait":
            await self._wait.wait()
        if action == "disconnect":
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(code=1000)
        if action == "disconnected_runtime":
            self.application_state = WebSocketState.DISCONNECTED
            raise RuntimeError("receive after server close")
        if isinstance(action, BaseException):
            raise action
        return action

    async def send_text(self, _value):
        if self.send_error:
            raise RuntimeError("send failed")

    async def close(self, code):
        self.close_code = code
        self.application_state = WebSocketState.DISCONNECTED


class WebSocketEndpointLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from routes import ws
        self.ws = ws
        self.manager = ConnectionManager()
        self.patches = [
            mock.patch.object(ws, "manager", self.manager),
            mock.patch.object(ws, "logged_in", return_value=True),
            mock.patch.object(ws, "ENABLED_FILE", SimpleNamespace(exists=lambda: True)),
            mock.patch.object(ws, "ACCOUNTS_FILE", SimpleNamespace(exists=lambda: True)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    async def test_server_closed_before_receive_ends_and_cleans_manager(self):
        socket = _EndpointSocket(close_after_accept=True)
        await self.ws.websocket_endpoint(socket)
        self.assertTrue(socket.accepted)
        self.assertEqual(socket.receive_calls, 0)
        self.assertNotIn(socket, self.manager._connections)

    async def test_disconnected_receive_runtime_error_ends_and_cleans_manager(self):
        socket = _EndpointSocket(["disconnected_runtime"])
        await self.ws.websocket_endpoint(socket)
        self.assertEqual(socket.receive_calls, 1)
        self.assertNotIn(socket, self.manager._connections)

    async def test_send_failure_then_receive_loop_exits_and_cleans_manager(self):
        socket = _EndpointSocket(["ping"], send_error=True)
        await self.ws.websocket_endpoint(socket)
        self.assertEqual(socket.close_code, 1013)
        self.assertNotIn(socket, self.manager._connections)

    async def test_normal_client_disconnect_is_still_handled(self):
        socket = _EndpointSocket(["disconnect"])
        await self.ws.websocket_endpoint(socket)
        self.assertNotIn(socket, self.manager._connections)

    async def test_other_runtime_error_propagates_and_cleans_manager(self):
        socket = _EndpointSocket([RuntimeError("unexpected receive failure")])
        with self.assertRaisesRegex(RuntimeError, "unexpected receive failure"):
            await self.ws.websocket_endpoint(socket)
        self.assertNotIn(socket, self.manager._connections)

    async def test_cancellation_propagates_and_cleans_manager(self):
        socket = _EndpointSocket(["wait"])
        task = asyncio.create_task(self.ws.websocket_endpoint(socket))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(socket, self.manager._connections)


class RateLimitResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_has_retry_after_header(self):
        limiter = RateLimiter(1)
        previous = RateLimitMiddleware._files
        RateLimitMiddleware._files = limiter
        middleware = RateLimitMiddleware(lambda _scope, _receive, _send: None)

        class Request:
            method = "POST"
            client = type("Client", (), {"host": "127.0.0.1"})()
            url = type("URL", (), {"path": "/conversation/c1/messages"})()

        async def next_response(_request):
            return Response()

        try:
            initial = await middleware.dispatch(Request(), next_response)
            self.assertEqual(initial.status_code, 200)
            response = await middleware.dispatch(Request(), next_response)
        finally:
            RateLimitMiddleware._files = previous

        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)

    async def test_snapshot_limits_are_independent_per_conversation(self):
        limiter = RateLimiter(1)
        previous = RateLimitMiddleware._api
        RateLimitMiddleware._api = limiter
        middleware = RateLimitMiddleware(lambda _scope, _receive, _send: None)

        class Request:
            method = "GET"
            client = type("Client", (), {"host": "127.0.0.1"})()

            def __init__(self, conversation_id):
                self.url = type(
                    "URL", (), {"path": f"/conversation/{conversation_id}/snapshot"}
                )()

        async def next_response(_request):
            return Response()

        try:
            first = await middleware.dispatch(Request("c1"), next_response)
            blocked = await middleware.dispatch(Request("c1"), next_response)
            independent = await middleware.dispatch(Request("c2"), next_response)
        finally:
            RateLimitMiddleware._api = previous

        self.assertEqual(first.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(independent.status_code, 200)

    async def test_export_limits_are_independent_from_snapshot_and_conversation(self):
        limiter = RateLimiter(1)
        previous = RateLimitMiddleware._api
        RateLimitMiddleware._api = limiter
        middleware = RateLimitMiddleware(lambda _scope, _receive, _send: None)

        class Request:
            method = "GET"
            client = type("Client", (), {"host": "127.0.0.1"})()

            def __init__(self, conversation_id, endpoint):
                self.url = type(
                    "URL", (), {"path": f"/conversation/{conversation_id}/{endpoint}"}
                )()

        async def next_response(_request):
            return Response()

        try:
            first_export = await middleware.dispatch(Request("c1", "export"), next_response)
            blocked_export = await middleware.dispatch(Request("c1", "export"), next_response)
            snapshot_is_independent = await middleware.dispatch(Request("c1", "snapshot"), next_response)
            other_conversation_is_independent = await middleware.dispatch(Request("c2", "export"), next_response)
        finally:
            RateLimitMiddleware._api = previous

        self.assertEqual(first_export.status_code, 200)
        self.assertEqual(blocked_export.status_code, 429)
        self.assertEqual(snapshot_is_independent.status_code, 200)
        self.assertEqual(other_conversation_is_independent.status_code, 200)
