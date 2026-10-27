"""Authenticated WebSocket channel for native conversation updates."""

import asyncio
import json
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from shared.diagnostics import emit
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from shared.config import ACCOUNTS_FILE, ENABLED_FILE
from shared.security import audit, logged_in

router = APIRouter()


@dataclass
class _Frame:
    payload: str
    conversation_id: str | None = None
    completion: asyncio.Future[bool] | None = None


@dataclass
class _Outbox:
    pending: deque[_Frame] = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    revisions: dict[str, int] = field(default_factory=dict)
    writer: asyncio.Task[None] | None = None
    current: _Frame | None = None
    started: bool = False
    close_code: int | None = None


class ConnectionManager:
    """One bounded outbox and one writer per authenticated connection."""

    def __init__(self):
        self._connections: dict[WebSocket, str | None] = {}
        self._outboxes: dict[WebSocket, _Outbox] = {}
        # Captured on connect; background threads use push_threadsafe.
        self.loop = None

    async def connect(self, ws: WebSocket, session_id: str | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        await ws.accept()
        self._connections[ws] = session_id
        outbox = _Outbox()
        self._outboxes[ws] = outbox

    def _start_writer(self, ws: WebSocket, outbox: _Outbox) -> None:
        if outbox.writer is None:
            outbox.writer = asyncio.create_task(self._writer(ws, outbox), name="websocket-writer")

    @staticmethod
    def _finish(frame: _Frame | None, delivered: bool) -> None:
        if frame is not None and frame.completion is not None and not frame.completion.done():
            frame.completion.set_result(delivered)

    def _discard(self, outbox: _Outbox) -> None:
        self._finish(outbox.current, False)
        for frame in outbox.pending:
            self._finish(frame, False)
        outbox.pending.clear()

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)
        outbox = self._outboxes.pop(ws, None)
        if outbox is not None:
            self._discard(outbox)
            task = outbox.writer
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    def _request_close(self, ws: WebSocket, outbox: _Outbox, code: int) -> None:
        self._connections.pop(ws, None)
        outbox.close_code = code
        self._discard(outbox)
        outbox.wake.set()
        self._start_writer(ws, outbox)
        # A not-yet-started task must enter its try/finally before cancellation.
        if outbox.started and outbox.writer is not asyncio.current_task():
            outbox.writer.cancel()

    async def close_sessions(self, session_ids: set[str]) -> None:
        writers = []
        for ws, session_id in list(self._connections.items()):
            if session_id and session_id in session_ids:
                outbox = self._outboxes[ws]
                self._request_close(ws, outbox, 4401)
                writers.append(outbox.writer)
        if writers:
            await asyncio.gather(*writers)

    def _enqueue(self, ws: WebSocket, frame: _Frame, *, revision: int | None = None,
                 revision_conversation: str | None = None) -> bool:
        outbox = self._outboxes.get(ws)
        if outbox is None or ws not in self._connections:
            return False
        if revision is not None and revision_conversation is not None:
            previous = outbox.revisions.get(revision_conversation)
            if previous is not None and revision < previous:
                return True
            outbox.revisions[revision_conversation] = revision
        if frame.conversation_id is not None:
            # Only merge inside the trailing ordinary-hint segment. Direct
            # replies, native interaction boundaries and terminals stay FIFO.
            for pending in reversed(outbox.pending):
                if pending.conversation_id is None:
                    break
                if pending.conversation_id == frame.conversation_id:
                    pending.payload = frame.payload
                    return True
        if len(outbox.pending) >= _MAX_PENDING_FRAMES:
            emit("ws_backpressure", level="warning", pending=len(outbox.pending), close_code=1013)
            self._request_close(ws, outbox, 1013)
            return False
        outbox.pending.append(frame)
        outbox.wake.set()
        self._start_writer(ws, outbox)
        return True

    async def _give_writers_turn(self, sockets) -> None:
        # A concurrent producer burst can fill the bounded queue before its
        # writer gets its first runnable turn. That is scheduler pressure, not
        # a slow browser. Yield only while no send is in flight; once a writer
        # is actually blocked, the normal queue limit closes it immediately.
        # sleep(0) adds no per-socket delivery Future or slow-send waiter.
        while any(
            ws in self._connections and (outbox := self._outboxes.get(ws)) is not None
            and len(outbox.pending) >= _MAX_PENDING_FRAMES and outbox.current is None
            for ws in sockets
        ):
            await asyncio.sleep(0)

    async def broadcast(self, event_type: str, data: Any) -> None:
        """Queue quickly; a slow browser never holds a broadcast caller."""
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        cid, revision, merge = None, None, None
        if event_type == "conversation.updated" and isinstance(data, dict):
            if isinstance(data.get("conversation_id"), str):
                cid = data["conversation_id"]
                if isinstance(data.get("revision"), int):
                    revision = data["revision"]
                # Older callers omit boundary: conservatively preserve them.
                if data.get("boundary") is False:
                    merge = cid
        targets = tuple(self._connections)
        await self._give_writers_turn(targets)
        for ws in targets:
            self._enqueue(ws, _Frame(payload, merge), revision=revision,
                          revision_conversation=cid)
        # Let ready healthy writers drain even during a sustained burst. No
        # send/lock waiter or delivery future is created for broadcasts.
        await asyncio.sleep(0)

    async def send(self, ws: WebSocket, payload: str) -> bool:
        """FIFO direct reply with a five-second total queue + send deadline."""
        try:
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                await self._give_writers_turn((ws,))
                completion = asyncio.get_running_loop().create_future()
                if not self._enqueue(ws, _Frame(payload, completion=completion)):
                    return False
                return await completion
        except TimeoutError:
            # A ping must not sit behind many individually slow frames and
            # defer the endpoint's periodic authentication recheck.
            outbox = self._outboxes.get(ws)
            if outbox is not None:
                self._request_close(ws, outbox, 1013)
            return False

    async def _writer(self, ws: WebSocket, outbox: _Outbox) -> None:
        outbox.started = True
        try:
            while ws in self._connections and outbox.close_code is None:
                await outbox.wake.wait()
                outbox.wake.clear()
                while outbox.pending and outbox.close_code is None:
                    outbox.current = outbox.pending.popleft()
                    async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                        await ws.send_text(outbox.current.payload)
                    self._finish(outbox.current, True)
                    outbox.current = None
        except asyncio.CancelledError:
            if outbox.close_code is None:
                raise
        except Exception as error:
            emit("ws_send_failed", level="error", exception_type=type(error).__name__,
                 message=str(error), stack="".join(traceback.format_exception(error)), close_code=1013)
            outbox.close_code = 1013
        finally:
            self._connections.pop(ws, None)
            self._discard(outbox)
            if outbox.close_code is not None:
                try:
                    async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                        await ws.close(code=outbox.close_code)
                except Exception as error:
                    emit("ws_close_failed", level="warning", exception_type=type(error).__name__)
            if self._outboxes.get(ws) is outbox:
                self._outboxes.pop(ws, None)

manager = ConnectionManager()
_AUTH_RECHECK_SECONDS = 60
_SEND_TIMEOUT_SECONDS = 5
_MAX_PENDING_FRAMES = 64


def push_threadsafe(coro) -> None:
    """从任意线程把广播协程调度进主循环；尚无 WS 连接（无循环）时静默丢弃。"""
    loop = manager.loop
    try:
        if loop is None or not loop.is_running():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        coro.close()


def close_passkey_sessions(session_ids) -> None:
    """Actively close in-process sockets for revoked Passkey sessions."""
    ids = {str(value) for value in session_ids if value}
    if ids:
        push_threadsafe(manager.close_sessions(ids))


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # 与 require_auth 同语义：TOTP/accounts 未启用或 codex_web_session cookie 无效即拒。
    # 红线：本鉴权未上线前，nginx 不得把 /ws 放行到任何非回环路径。
    if (not ENABLED_FILE.exists() and not ACCOUNTS_FILE.exists()) or not logged_in(ws):
        audit(ws, "ws_auth_failed")
        await ws.close(code=4401)
        return
    from shared.passkey_auth import session_id_from_request
    await manager.connect(ws, session_id_from_request(ws))
    next_auth_check = time.monotonic() + _AUTH_RECHECK_SECONDS
    try:
        while True:
            # Re-check on a fixed deadline even while the client is active.
            # This is the fail-closed fallback for revocation performed by the
            # separate local CLI, which cannot close this process's sockets.
            timeout = max(0.0, next_auth_check - time.monotonic())
            # A server-side close can leave receive_text() unable to read;
            # avoid calling it once either side is already disconnected.
            if (
                ws.client_state is WebSocketState.DISCONNECTED
                or ws.application_state is WebSocketState.DISCONNECTED
            ):
                break
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=timeout)
            except asyncio.TimeoutError:
                data = None
            except RuntimeError:
                # Starlette raises RuntimeError for receive_text() after a
                # close.  Only that actual disconnected state is normal; all
                # other runtime failures remain visible to the caller.
                if (
                    ws.client_state is WebSocketState.DISCONNECTED
                    or ws.application_state is WebSocketState.DISCONNECTED
                ):
                    break
                raise
            if time.monotonic() >= next_auth_check:
                if not logged_in(ws):
                    await ws.close(code=4401)
                    break
                next_auth_check = time.monotonic() + _AUTH_RECHECK_SECONDS
            if data is None:
                continue
            if data == "ping":
                await manager.send(ws, '{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
