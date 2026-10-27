"""Synthetic high-volume ingress and mixed-speed browser regressions."""
import asyncio
import copy
import json
import unittest
from unittest import mock

from routes.ws import ConnectionManager
from tests import test_conversation_runtime as runtime_fixture


async def eventually(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


class RuntimeStatusPressureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime_fixture.ConversationRuntimeTests.asyncSetUp
    asyncTearDown = runtime_fixture.ConversationRuntimeTests.asyncTearDown
    start = runtime_fixture.ConversationRuntimeTests.start
    complete_turn = runtime_fixture.ConversationRuntimeTests.complete_turn

    async def delta(self, text="x"):
        await self.runtime.handle_notification({
            "method": "item/agentMessage/delta", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "stream", "delta": text,
            },
        })

    async def test_6351_items_stream_never_reads_full_history_for_status(self):
        await self.start()
        thread = copy.deepcopy(self.factory.threads["thread-1"])
        thread["turns"][0]["items"] = [
            {"id": f"history-{i}", "type": "agentMessage", "text": f"fixture {i}"}
            for i in range(6350)
        ] + [{"id": "stream", "type": "agentMessage", "text": ""}]
        self.store.reconcile_thread("conversation-1", {"thread": thread})
        self.statuses.clear()
        queries = []
        self.store._db.set_trace_callback(queries.append)
        try:
            with mock.patch.object(self.store, "snapshot", wraps=self.store.snapshot) as snapshot:
                for _ in range(100):
                    await self.delta()
                await eventually(lambda: bool(self.statuses))
                snapshot.assert_not_called()
            full_reads = [sql for sql in queries if
                "SELECT * FROM turns WHERE conversation_id" in sql
                or ("FROM items i LEFT JOIN" in sql and "WHERE i.conversation_id=" in sql)]
            self.assertEqual(full_reads, [])
        finally:
            self.store._db.set_trace_callback(None)
        self.assertEqual(self.statuses[-1][1], self.store.get_conversation("conversation-1")["revision"])
        current = self.store.snapshot("conversation-1")
        self.assertEqual(len(current["turns"][0]["items"]), 6351)
        stream = next(item for item in current["turns"][0]["items"] if item["native_item_id"] == "stream")
        self.assertEqual(stream["text"], "x" * 100)

    async def test_pending_status_is_single_flight_and_terminal_is_immediate(self):
        await self.start()
        self.statuses.clear()
        # A terminal must not wait for even a deliberately huge merge window.
        with mock.patch("engine.conversation_runtime._STATUS_COALESCE_SECONDS", 30):
            for _ in range(200):
                await self.delta()
            statuses = [task for task in self.runtime._background if "status" in task.get_name()]
            self.assertLessEqual(len(statuses), 1)
            terminals = []
            self.runtime._terminal_handler = lambda cid, tid, snapshot: terminals.append(snapshot)
            await self.complete_turn()
            await eventually(lambda: bool(terminals), timeout=0.5)
        self.assertEqual(terminals[0]["turns"][0]["status"], "completed")
        self.assertIn("items", terminals[0]["turns"][0])
        self.assertEqual(self.statuses[-1][1], self.store.get_conversation("conversation-1")["revision"])
        await self.runtime.close()
        self.assertFalse(self.runtime._background)
        self.assertFalse(self.runtime._pending_status)

    async def test_changes_while_status_handler_waits_use_same_worker_then_latest_revision(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        delivered = []

        async def slow_status(cid, snapshot):
            entered.set()
            await release.wait()
            delivered.append(snapshot["conversation"]["revision"])

        self.runtime._status_handler = slow_status
        await self.delta()
        await asyncio.wait_for(entered.wait(), timeout=2)
        worker = self.runtime._pending_status["conversation-1"]
        for _ in range(30):
            await self.delta()
            self.assertIs(self.runtime._pending_status["conversation-1"], worker)
        latest = self.store.get_conversation("conversation-1")["revision"]
        release.set()
        await eventually(lambda: delivered and delivered[-1] == latest)
        self.assertEqual(len(delivered), 2)
        await eventually(lambda: not self.runtime._pending_status)

    async def test_nonblocking_question_arrival_and_close_keep_wire_boundaries(self):
        from engine.conversation_service import _broadcast_status
        await self.start()
        manager = ConnectionManager()
        socket = Socket(blocked=True)
        await manager.connect(socket)
        emitted = []

        async def status(cid, snapshot):
            emitted.append(snapshot)
            await _broadcast_status(cid, snapshot)

        self.runtime._status_handler = status
        pending = None
        try:
            with mock.patch("routes.ws.manager", manager):
                await self.delta()
                await asyncio.wait_for(socket.started.wait(), timeout=2)
                pending = asyncio.create_task(self.runtime.handle_server_request(
                    "item/tool/requestUserInput", {
                        "threadId": "thread-1", "turnId": "turn-1", "itemId": "ask-1",
                        "isBlocking": False, "autoResolutionMs": None,
                        "questions": [{"id": "why", "header": "Why", "question": "Why?",
                                       "options": None, "isOther": False, "isSecret": False}],
                    }, request_id=0, connection_id="test-instance:1",
                ))
                await eventually(lambda: len(emitted) == 2)
                arrival_revision = emitted[-1]["conversation"]["revision"]
                self.assertTrue(emitted[-1]["status_boundary"])
                await self.delta()
                await self.runtime.handle_notification({
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread-1", "requestId": 0},
                }, connection_id="test-instance:1")
                closed_revision = emitted[-1]["conversation"]["revision"]
                self.assertTrue(emitted[-1]["status_boundary"])
                self.assertEqual(self.store.snapshot("conversation-1")["questions"][0]["state"], "closed")
                await self.delta()
                await eventually(lambda: len(emitted) == 4)
                socket.release.set()
                await eventually(lambda: len(socket.sent) == 4)
                boundaries = [event["data"]["revision"] for event in socket.sent if event["data"]["boundary"]]
                self.assertEqual(boundaries, [arrival_revision, closed_revision])
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            manager.disconnect(socket)


class Socket:
    def __init__(self, blocked=False):
        self.sent = []
        self.closed = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked = blocked
        self.sending = False

    async def accept(self):
        pass

    async def send_text(self, payload):
        if self.sending:
            raise AssertionError("overlapping frames")
        self.sending = True
        self.started.set()
        try:
            if self.blocked:
                await self.release.wait()
            self.sent.append(json.loads(payload))
        finally:
            self.sending = False

    async def close(self, code):
        self.closed = code


class SocketPressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ConnectionManager()
        self.sockets = []

    async def connect(self, blocked=False):
        socket = Socket(blocked)
        self.sockets.append(socket)
        await self.manager.connect(socket)
        return socket

    async def asyncTearDown(self):
        for socket in self.sockets:
            self.manager.disconnect(socket)
        await asyncio.sleep(0)

    async def updated(self, revision, conversation="c1"):
        await self.manager.broadcast("conversation.updated", {
            "conversation_id": conversation, "revision": revision,
            "state": "running", "active_turn_id": "turn-1", "boundary": False,
        })

    async def test_slow_browser_does_not_hold_fast_terminal_and_updates_coalesce(self):
        slow, fast = await self.connect(True), await self.connect()
        # Queueing is independent from actual socket delivery.
        await asyncio.wait_for(self.updated(1), timeout=0.2)
        await slow.started.wait()
        writer = self.manager._outboxes[slow].writer
        for revision in range(2, 202):
            await asyncio.wait_for(self.updated(revision), timeout=0.2)
            self.assertIs(self.manager._outboxes[slow].writer, writer)
            self.assertLessEqual(len(self.manager._outboxes[slow].pending), 64)
        await self.manager.broadcast("conversation.terminal", {"conversation_id": "c2", "turn_id": "t2"})
        await eventually(lambda: any(event["type"] == "conversation.terminal" for event in fast.sent))
        self.assertIsNone(fast.closed)
        self.assertIsNone(slow.closed)
        self.assertLessEqual(len(self.manager._outboxes[slow].pending), 64)
        await self.updated(200)  # Out-of-order revision must not go backwards.
        slow.release.set()
        await eventually(lambda: any(event["type"] == "conversation.terminal" for event in slow.sent))
        self.assertEqual([e["data"]["revision"] for e in slow.sent if e["type"] == "conversation.updated"], [1, 201])

    async def test_fifo_terminal_boundaries_and_direct_ping_share_one_writer(self):
        socket = await self.connect(True)
        await asyncio.wait_for(self.updated(1), timeout=0.2)
        await socket.started.wait()
        await self.updated(2)
        await self.manager.broadcast("conversation.terminal", {"conversation_id": "other", "turn_id": "done"})
        await self.updated(3)
        ping = asyncio.create_task(self.manager.send(socket, '{"type":"pong"}'))
        await asyncio.sleep(0)
        await self.updated(4)
        socket.release.set()
        self.assertTrue(await ping)
        await eventually(lambda: len(socket.sent) == 6)
        self.assertEqual([e["type"] for e in socket.sent], [
            "conversation.updated", "conversation.updated", "conversation.terminal",
            "conversation.updated", "pong", "conversation.updated",
        ])
        self.assertEqual([e["data"]["revision"] for e in socket.sent if e["type"] == "conversation.updated"], [1, 2, 3, 4])

    async def test_overflow_disconnects_only_slow_browser_and_cleans_writer(self):
        slow, fast = await self.connect(True), await self.connect()
        await asyncio.wait_for(self.updated(1), timeout=0.2)
        await slow.started.wait()
        for number in range(100):
            await self.manager.broadcast("conversation.terminal", {"conversation_id": str(number), "turn_id": "done"})
        await eventually(lambda: slow.closed == 1013)
        await eventually(lambda: len(fast.sent) == 101)
        self.assertIsNone(fast.closed)
        self.assertNotIn(slow, self.manager._connections)
        self.assertNotIn(slow, self.manager._outboxes)

    async def test_revocation_unblocks_direct_replies_and_does_not_close_other_sessions(self):
        slow, fast = Socket(blocked=True), Socket()
        self.sockets.extend([slow, fast])
        await self.manager.connect(slow, "revoked")
        await self.manager.connect(fast, "healthy")
        first = asyncio.create_task(self.manager.send(slow, '{"type":"pong"}'))
        await slow.started.wait()
        second = asyncio.create_task(self.manager.send(slow, '{"type":"pong"}'))
        await asyncio.sleep(0)
        writer = self.manager._outboxes[slow].writer
        await self.manager.close_sessions({"revoked"})
        self.assertEqual(await asyncio.gather(first, second), [False, False])
        self.assertEqual(slow.closed, 4401)
        self.assertTrue(writer.done())
        self.assertNotIn(slow, self.manager._outboxes)
        self.assertIsNone(fast.closed)
        await self.manager.broadcast("conversation.terminal", {"conversation_id": "other", "turn_id": "t"})
        await eventually(lambda: bool(fast.sent))
        self.assertEqual(fast.sent[-1]["type"], "conversation.terminal")

    async def test_concurrent_terminal_burst_does_not_misclassify_unscheduled_healthy_writer(self):
        socket = await self.connect()
        await asyncio.gather(*(
            self.manager.broadcast("conversation.terminal", {"conversation_id": str(i), "turn_id": "done"})
            for i in range(200)
        ))
        self.assertIsNone(socket.closed)
        await eventually(lambda: len(socket.sent) == 200)
        self.assertEqual([e["data"]["conversation_id"] for e in socket.sent], [str(i) for i in range(200)])

    async def test_direct_reply_deadline_includes_time_behind_earlier_frames(self):
        socket = await self.connect(True)
        # Let an earlier write have a long deadline, then give the queued pong
        # a short one. It must not inherit the whole outbox's possible delay.
        await self.updated(1)
        await socket.started.wait()
        with mock.patch("routes.ws._SEND_TIMEOUT_SECONDS", 0.02):
            self.assertFalse(await self.manager.send(socket, '{"type":"pong"}'))
            await eventually(lambda: socket.closed == 1013)
        self.assertNotIn(socket, self.manager._outboxes)
