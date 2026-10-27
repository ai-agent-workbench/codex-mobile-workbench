import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import websockets

from engine.codex_app_server import (
    PROTOCOL_FIXTURE,
    PROTOCOL_FIXTURES,
    PROTOCOL_FIXTURE_0_151,
    PROTOCOL_FIXTURE_0_152_1,
    PROTOCOL_FIXTURE_0_153_1,
    PROTOCOL_FIXTURE_0_154_0,
    PROTOCOL_FIXTURE_0_156_0,
    PROTOCOL_FIXTURE_0_160_0,
    AppServerDisconnected,
    AppServerError,
    AppServerProtocolError,
    AppServerRPCError,
    CodexAppServerClient,
)


INITIALIZE_RESULT = {
    "codexHome": "/tmp/codex-home",
    "platformFamily": "unix",
    "platformOs": "linux",
    "userAgent": "codex_app_server/0.151.0-alpha.7.2",
}
MODEL = {
    "defaultReasoningEffort": "medium",
    "description": "fixture model",
    "displayName": "Fixture",
    "hidden": False,
    "id": "fixture",
    "isDefault": True,
    "model": "fixture",
    "supportedReasoningEfforts": [{
        "reasoningEffort": "medium",
        "description": "fixture effort",
    }],
    "serviceTiers": [{
        "id": "priority", "name": "Fast", "description": "1.5x speed",
    }],
}
SKILL = {
    "description": "fixture skill",
    "enabled": True,
    "name": "fixture",
    "path": "/tmp/SKILL.md",
    "scope": "repo",
}


class FakeUnixAppServer:
    def __init__(self, responder=None):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = str(Path(self.temporary.name) / "app-server.sock")
        self.responder = responder or self._default_responder
        self.received = []
        self.server = None

    async def __aenter__(self):
        self.server = await websockets.unix_serve(self._handler, self.path)
        os.chmod(self.path, 0o600)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def _handler(self, websocket):
        async for raw in websocket:
            message = json.loads(raw)
            self.received.append(message)
            response = await self.responder(websocket, message)
            if response is not None:
                await websocket.send(json.dumps(response))

    async def _default_responder(self, websocket, message):
        if message.get("method") == "initialize":
            return {"id": message["id"], "result": INITIALIZE_RESULT}
        if message.get("method") == "model/list":
            return {"id": message["id"], "result": {"data": [MODEL]}}
        if message.get("method") == "skills/list":
            return {
                "id": message["id"],
                "result": {
                    "data": [{
                        "cwd": "/tmp", "errors": [], "skills": [SKILL],
                    }],
                },
            }
        return None


class CodexAppServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def fake_server(self, responder=None):
        return FakeUnixAppServer(responder)

    async def test_native_approval_policy_follows_user_sandbox_not_deny_all(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._model_catalogs[False] = [MODEL]
        for sandbox, review, expected in (("read-only", False, "on-request"),
                                         ("workspace-write", False, "on-request"),
                                         ("danger-full-access", False, "never"),
                                         ("read-only", True, "never")):
            client.request = mock.AsyncMock(return_value={"thread": {"id": "thread-policy"}})
            await client.start_thread(cwd="/tmp", model="fixture", sandbox=sandbox, review_mode=review)
            self.assertEqual(client.request.call_args.args[1]["approvalPolicy"], expected)
        client.request = mock.AsyncMock(return_value={"turn": {"id": "turn-policy", "status": "inProgress"}})
        await client.start_turn("thread-policy", "test", options={"sandbox": "workspace-write"})
        self.assertEqual(client.request.call_args.args[1]["approvalPolicy"], "on-request")
        await client.start_turn("thread-policy", "test", options={"sandbox": "workspace-write", "approvalPolicy": "never"})
        self.assertEqual(client.request.call_args.args[1]["approvalPolicy"], "never")
        with self.assertRaises(AppServerProtocolError):
            await client.start_turn("thread-policy", "test", options={"approvalPolicy": "auto-approve"})

    async def test_review_thread_passes_model_and_effort_without_tool_isolation(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._model_catalogs[False] = [dict(MODEL, supportedReasoningEfforts=[
            {"reasoningEffort": "high", "description": "high"},
        ])]
        client.request = mock.AsyncMock(return_value={"thread": {"id": "review-thread"}})
        await client.start_thread(
            cwd="/tmp", model="fixture", sandbox="read-only", review_mode=True,
            review_reasoning_effort="high", review_instructions="Review only",
        )
        params = client.request.call_args.args[1]
        self.assertEqual(params["config"]["review_model"], "fixture")
        self.assertEqual(params["config"]["model_reasoning_effort"], "high")
        self.assertIs(params["config"]["features.hooks"], False)
        self.assertFalse(any(key.startswith(("mcp_servers.", "apps.", "plugins."))
                             for key in params["config"]))
        with self.assertRaisesRegex(AppServerProtocolError, "effort is unavailable"):
            await client.start_thread(cwd="/tmp", model="fixture", review_mode=True,
                                      review_reasoning_effort="low")

    async def test_direct_unix_websocket_initialize_and_basic_lists(self):
        async with self.fake_server() as server:
            client = CodexAppServerClient(server.path)
            result = await client.connect()
            models = await client.list_models(limit=5)
            skills = await client.list_skills(cwds=["/tmp"])
            await client.close()

        self.assertEqual(result, INITIALIZE_RESULT)
        self.assertEqual(models["data"], [MODEL])
        self.assertEqual(skills["data"][0]["skills"], [SKILL])
        methods = [message.get("method") for message in server.received]
        self.assertEqual(
            methods,
            ["initialize", "initialized", "model/list", "skills/list"],
        )
        self.assertEqual(
            server.received[0]["params"]["capabilities"]["experimentalApi"],
            True,
        )
        self.assertNotIn("jsonrpc", server.received[0])

    async def test_current_0_152_fixture_validates_basic_lists(self):
        current_initialize = dict(
            INITIALIZE_RESULT,
            userAgent="Codex Desktop/0.152.1 (fixture platform)",
        )

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": current_initialize}
            if message.get("method") == "model/list":
                return {"id": message["id"], "result": {"data": [MODEL]}}
            if message.get("method") == "skills/list":
                return {
                    "id": message["id"],
                    "result": {
                        "data": [{
                            "cwd": "/tmp", "errors": [], "skills": [SKILL],
                        }],
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertEqual((await client.list_models())["data"], [MODEL])
            self.assertEqual(
                (await client.list_skills(cwds=["/tmp"]))["data"][0]["skills"],
                [SKILL],
            )
            self.assertEqual(client.server_version, "0.152.1")
            await client.close()
    async def test_current_0_154_fixture_validates_astra_and_basic_lists(self):
        astra = dict(
            MODEL, id="gpt-6-astra", model="gpt-6-astra", displayName="GPT-6 Astra",
            supportedReasoningEfforts=[
                {"reasoningEffort": value, "description": value}
                for value in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
        )
        current_initialize = dict(
            INITIALIZE_RESULT,
            userAgent="Codex Desktop/0.154.0 (fixture platform)",
        )

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": current_initialize}
            if message.get("method") == "model/list":
                return {"id": message["id"], "result": {"data": [astra]}}
            if message.get("method") == "skills/list":
                return {
                    "id": message["id"],
                    "result": {
                        "data": [{
                            "cwd": "/tmp", "errors": [], "skills": [SKILL],
                        }],
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertEqual((await client.list_models())["data"], [astra])
            self.assertEqual(
                (await client.list_skills(cwds=["/tmp"]))["data"][0]["skills"],
                [SKILL],
            )
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
                selected = await client.resolve_model_selection("gpt-6-astra", effort)
                self.assertEqual(selected["model"], "gpt-6-astra")
                self.assertEqual(selected["reasoning_effort"], effort)
            with self.assertRaises(AppServerProtocolError):
                await client.resolve_model_selection("gpt-6-astra", "invented")
            self.assertEqual(client.server_version, "0.154.0")
            await client.close()

    async def test_0_154_scopes_request_user_input_feature_to_start_and_resume(self):
        current_initialize = dict(
            INITIALIZE_RESULT, userAgent="Codex Desktop/0.154.0 (fixture platform)"
        )

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": current_initialize}
            if message.get("method") == "model/list":
                return {"id": message["id"], "result": {"data": [MODEL]}}
            if message.get("method") in {"thread/start", "thread/resume"}:
                return {"id": message["id"], "result": {"thread": {
                    "id": "thread-feature", "cwd": "/tmp",
                }}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            selected = await client.resolve_model_selection("fixture", "medium")
            await client.start_thread(cwd="/tmp", model=selected["model"])
            await client.resume_thread("thread-feature")
            await client.close()
        feature = {"features.default_mode_request_user_input": True}
        start = next(item for item in server.received if item.get("method") == "thread/start")
        resume = next(item for item in server.received if item.get("method") == "thread/resume")
        self.assertEqual(start["params"]["config"], feature)
        self.assertEqual(resume["params"]["config"], feature)
        self.assertEqual(start["params"]["approvalPolicy"], "never")
        self.assertEqual(start["params"]["sandbox"], "danger-full-access")

    async def test_weekly_rate_limit_uses_primary_codex_window_not_spark(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "account/rateLimits/read":
                return {
                    "id": message["id"],
                    "result": {
                        "rateLimits": {
                            "limitId": "codex",
                            "planType": "pro",
                            "primary": {
                                "usedPercent": 28,
                                "windowDurationMins": 10080,
                                "resetsAt": 1788748344,
                            },
                            "secondary": None,
                        },
                        "rateLimitsByLimitId": {
                            "codex_bengalfox": {
                                "limitName": "GPT-5.3-Codex-Spark",
                                "primary": {
                                    "usedPercent": 0,
                                    "windowDurationMins": 300,
                                    "resetsAt": 1788256185,
                                },
                            },
                        },
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            quota = await client.read_weekly_rate_limit()
            await client.close()

        self.assertEqual(quota, {
            "remaining_percent": 72,
            "resets_at": 1788748344,
            "plan_type": "pro",
        })
        self.assertEqual(
            [item.get("method") for item in server.received],
            ["initialize", "initialized", "account/rateLimits/read"],
        )

    async def test_weekly_rate_limit_rejects_missing_or_invalid_week(self):
        with self.assertRaisesRegex(AppServerProtocolError, "weekly rate limit"):
            CodexAppServerClient._normalize_weekly_rate_limit({
                "rateLimits": {
                    "primary": {
                        "usedPercent": 20,
                        "windowDurationMins": 300,
                        "resetsAt": 1788256185,
                    }
                }
            })
        with self.assertRaisesRegex(AppServerProtocolError, "percentage"):
            CodexAppServerClient._normalize_weekly_rate_limit({
                "rateLimits": {
                    "primary": {
                        "usedPercent": 101,
                        "windowDurationMins": 10080,
                        "resetsAt": 1788748344,
                    }
                }
            })

    async def test_thread_resume_accepts_snapshot_larger_than_default_limit(self):
        large_text = "x" * (2 * 1024 * 1024)

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "thread/resume":
                return {
                    "id": message["id"],
                    "result": {
                        "thread": {
                            "id": "thread-large",
                            "cwd": "/tmp",
                            "status": {"type": "idle"},
                            "turns": [{
                                "id": "turn-large",
                                "status": "completed",
                                "items": [{"type": "agentMessage", "text": large_text}],
                            }],
                        }
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            resumed = await client.resume_thread("thread-large")
            self.assertTrue(client.connected)
            await client.close()

        received = resumed["thread"]["turns"][0]["items"][0]["text"]
        self.assertEqual(len(received), len(large_text))

    async def test_model_catalog_paginates_and_validates_model_effort_pair(self):
        second = {
            **MODEL,
            "id": "fixture-two",
            "model": "fixture-two",
            "displayName": "Fixture Two",
            "isDefault": False,
            "defaultReasoningEffort": "high",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "high", "description": "deep"},
                {"reasoningEffort": "xhigh", "description": "deeper"},
            ],
            "serviceTiers": [],
        }

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "model/list":
                cursor = message["params"].get("cursor")
                if cursor is None:
                    return {
                        "id": message["id"],
                        "result": {"data": [MODEL], "nextCursor": "page-two"},
                    }
                self.assertEqual(cursor, "page-two")
                return {
                    "id": message["id"],
                    "result": {"data": [second], "nextCursor": None},
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            selected = await client.resolve_model_selection(
                "fixture-two", "xhigh", force_refresh=True
            )
            cached = await client.list_all_models()
            with self.assertRaisesRegex(AppServerProtocolError, "not supported"):
                await client.resolve_model_selection(
                    "fixture-two", "medium", force_refresh=False
                )
            await client.close()

        self.assertEqual(selected, {
            "catalog_id": "fixture-two",
            "model": "fixture-two",
            "reasoning_effort": "xhigh",
        })
        self.assertEqual([item["id"] for item in cached], ["fixture", "fixture-two"])
        model_calls = [
            item for item in server.received if item.get("method") == "model/list"
        ]
        # The unsupported cached effort performs one synchronous validation
        # refresh before it is rejected; each full catalog remains paginated.
        self.assertEqual(len(model_calls), 4)
        self.assertEqual(model_calls[1]["params"]["cursor"], "page-two")
        self.assertEqual(model_calls[3]["params"]["cursor"], "page-two")

    async def test_model_catalog_single_flight_and_stale_refresh(self):
        release = asyncio.Event()
        started = asyncio.Event()
        release.set()

        async def responder(_websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "model/list":
                started.set()
                await release.wait()
                return {"id": message["id"], "result": {"data": [MODEL]}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            release.clear()
            first = asyncio.create_task(client.list_all_models(force_refresh=True))
            second = asyncio.create_task(client.list_all_models(force_refresh=True))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual(
                    len([m for m in server.received if m.get("method") == "model/list"]), 1
                )
                release.set()
                self.assertEqual(await first, await second)
                self.assertEqual(
                    len([m for m in server.received if m.get("method") == "model/list"]), 1
                )

                client._model_catalog_cached_at[False] = __import__("time").monotonic() - 61
                started.clear()
                release.clear()
                stale = await client.list_all_models()
                self.assertEqual(stale, [MODEL])
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual(
                    len([m for m in server.received if m.get("method") == "model/list"]), 2
                )
            finally:
                release.set()
            await asyncio.sleep(0)
            await client.close()

    async def test_model_catalog_cancelled_waiter_keeps_shared_refresh(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(**_kwargs):
            entered.set()
            await release.wait()
            return {"data": [MODEL]}

        client.list_models = delayed
        leader = asyncio.create_task(client.list_all_models(force_refresh=True))
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancelled = asyncio.create_task(client.list_all_models(force_refresh=True))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(client._model_catalog_refresh_tasks[False].done())
        release.set()
        self.assertEqual(await leader, [MODEL])

    async def test_account_invalidated_catalog_retries_once_for_shared_waiters(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        entered = asyncio.Event()
        calls = 0

        async def delayed(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return {"data": [MODEL]}

        client.list_models = delayed
        first = asyncio.create_task(client.list_all_models(force_refresh=True))
        second = asyncio.create_task(client.list_all_models(force_refresh=True))
        await entered.wait()
        await client._handle_notification({"method": "account/updated"}, connection_epoch=1)
        for waiter in (first, second):
            self.assertEqual(await waiter, [MODEL])
            self.assertEqual(waiter.cancelling(), 0)
        self.assertEqual(calls, 2)
        await client.close()

    async def test_account_cancelled_catalog_late_wire_reply_retries_same_connection(self):
        for late_error in (False, True):
            with self.subTest(late_error=late_error):
                entered, release = asyncio.Event(), asyncio.Event()
                calls = 0
                old_model = dict(MODEL, id="old", model="old")
                new_model = dict(MODEL, id="new", model="new")

                async def responder(websocket, message):
                    nonlocal calls
                    if message.get("method") == "initialize":
                        return {"id": message["id"], "result": INITIALIZE_RESULT}
                    if message.get("method") == "model/list":
                        calls += 1
                        if calls == 1:
                            await websocket.send(json.dumps({"method": "account/updated", "params": {}}))
                            entered.set()
                            await release.wait()
                            if late_error:
                                return {"id": message["id"], "error": {"code": -32000, "message": "late fixture error"}}
                            return {"id": message["id"], "result": {"data": [old_model]}}
                        return {"id": message["id"], "result": {"data": [new_model]}}
                async with self.fake_server(responder) as server:
                    client = CodexAppServerClient(server.path)
                    await client.connect()
                    pending = asyncio.create_task(client.list_all_models(force_refresh=True))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        release.set()
                        self.assertEqual(await asyncio.wait_for(pending, timeout=2), [new_model])
                        self.assertEqual(calls, 2)
                        self.assertEqual(await client.list_all_models(), [new_model])
                        self.assertFalse(client._pending)
                        self.assertTrue(client.connected)
                    finally:
                        release.set()
                        await client.close()

    async def test_cached_unknown_or_effort_refreshes_once_but_cold_failure_does_not_retry(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        client._model_catalogs[False] = (dict(MODEL),)
        client._model_catalog_cached_at[False] = __import__("time").monotonic()
        client._model_catalog_versions[False] = 1
        newer = dict(MODEL, id="new", model="new", isDefault=False,
                     supportedReasoningEfforts=[{"reasoningEffort": "high"}],
                     defaultReasoningEffort="high")
        calls = 0

        async def refreshed(**_kwargs):
            nonlocal calls
            calls += 1
            return {"data": [MODEL, newer]}

        client.list_models = refreshed
        self.assertEqual(
            (await client.resolve_model_selection("new", "high"))["catalog_id"], "new"
        )
        self.assertEqual(calls, 1)
        client._clear_model_catalogs()

        async def failed(**_kwargs):
            nonlocal calls
            calls += 1
            raise AppServerError("cold catalog failed")

        client.list_models = failed
        with self.assertRaises(AppServerError):
            await client.resolve_model_selection("new", "high")
        self.assertEqual(calls, 2)

    async def test_stale_catalog_failure_keeps_last_good_and_cools_down(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        client._model_catalogs[False] = (dict(MODEL),)
        client._model_catalog_cached_at[False] = __import__("time").monotonic() - 61
        calls = 0

        async def failed(**_kwargs):
            nonlocal calls
            calls += 1
            raise AppServerError("refresh failed")

        client.list_models = failed
        self.assertEqual(await client.list_all_models(), [MODEL])
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertEqual(await client.list_all_models(), [MODEL])
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertIn(False, client._model_catalogs)

    async def test_close_joins_hanging_catalog_refresh_and_old_account_cannot_return_it(self):
        async def cancellation_resistant_client():
            client = CodexAppServerClient("/tmp/unused.sock")
            client._connected = client._initialized = True
            client._connection_epoch = 1
            entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def delayed(**_kwargs):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return {"data": [MODEL]}

            client.list_models = delayed
            pending = asyncio.create_task(client.list_all_models(force_refresh=True))
            await asyncio.wait_for(entered.wait(), timeout=1)
            return client, pending, cancelled, release

        client, pending, cancelled, release = await cancellation_resistant_client()
        closing = asyncio.create_task(client.close())
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        self.assertFalse(closing.done())
        release.set()
        await closing
        with self.assertRaisesRegex(AppServerDisconnected, "close"):
            await pending
        self.assertFalse(client._retired_model_catalog_refresh_tasks)

        client, pending, cancelled, release = await cancellation_resistant_client()
        await client._handle_notification({"method": "account/updated"}, connection_epoch=1)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        release.set()
        self.assertEqual(await pending, [MODEL])
        self.assertEqual(await client.list_all_models(), [MODEL])

        client, pending, cancelled, release = await cancellation_resistant_client()
        # connect() performs the same generation invalidation before it accepts
        # a new epoch; model/list completion from this old epoch is rejected.
        client._clear_model_catalogs(reason="connect")
        client._connection_epoch = 2
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        release.set()
        with self.assertRaisesRegex(AppServerDisconnected, "connect"):
            await pending
        self.assertNotIn(False, client._model_catalogs)

    async def test_two_account_invalidations_fail_without_a_third_catalog_request(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        started = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def delayed(**_kwargs):
            nonlocal calls
            started[calls].set()
            calls += 1
            await asyncio.Event().wait()

        client.list_models = delayed
        pending = asyncio.create_task(client.list_all_models(force_refresh=True))
        await asyncio.wait_for(started[0].wait(), timeout=1)
        await client._handle_notification({"method": "account/updated"}, connection_epoch=1)
        await asyncio.wait_for(started[1].wait(), timeout=1)
        await client._handle_notification({"method": "account/updated"}, connection_epoch=1)
        with self.assertRaisesRegex(AppServerDisconnected, "account update"):
            await pending
        await asyncio.sleep(0)
        self.assertEqual(calls, 2)

    async def test_account_notification_burst_retries_one_fresh_catalog(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        entered = asyncio.Event()
        calls = 0
        fresh_model = dict(MODEL, id="fresh", model="fresh")

        async def delayed(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return {"data": [fresh_model]}

        client.list_models = delayed
        pending = asyncio.create_task(client.list_all_models(force_refresh=True))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await client._handle_notification({"method": "account/updated"}, connection_epoch=1)
        await client._handle_notification(
            {"method": "account/login/completed"}, connection_epoch=1
        )
        self.assertEqual(await pending, [fresh_model])
        self.assertEqual(calls, 2)
        self.assertEqual(await client.list_all_models(), [fresh_model])

    async def test_account_cancel_followed_by_close_or_disconnect_does_not_retry(self):
        for lifecycle in ("close", "disconnect"):
            with self.subTest(lifecycle=lifecycle):
                client = CodexAppServerClient("/tmp/unused.sock")
                client._connected = client._initialized = True
                client._connection_epoch = 1
                entered = asyncio.Event()
                calls = 0

                async def delayed(**_kwargs):
                    nonlocal calls
                    calls += 1
                    entered.set()
                    await asyncio.Event().wait()

                client.list_models = delayed
                pending = asyncio.create_task(client.list_all_models(force_refresh=True))
                await asyncio.wait_for(entered.wait(), timeout=1)
                await client._handle_notification(
                    {"method": "account/updated"}, connection_epoch=1
                )
                if lifecycle == "close":
                    await client.close()
                else:
                    client._mark_disconnected(
                        None, connection_epoch=1, source="fixture"
                    )
                with self.assertRaisesRegex(AppServerDisconnected, lifecycle):
                    await pending
                self.assertEqual(calls, 1)

    async def test_non_account_catalog_failures_do_not_retry(self):
        for failure in (AppServerError("catalog timed out"),
                        AppServerProtocolError("catalog schema failed")):
            with self.subTest(failure=type(failure).__name__):
                client = CodexAppServerClient("/tmp/unused.sock")
                client._connected = client._initialized = True
                client._connection_epoch = 1
                calls = 0

                async def failed(**_kwargs):
                    nonlocal calls
                    calls += 1
                    raise failure

                client.list_models = failed
                with self.assertRaises(type(failure)):
                    await client.list_all_models(force_refresh=True)
                self.assertEqual(calls, 1)

        client = CodexAppServerClient("/tmp/unused.sock")
        client._connected = client._initialized = True
        client._connection_epoch = 1
        entered = asyncio.Event()
        calls = 0

        async def delayed(**_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()

        client.list_models = delayed
        pending = asyncio.create_task(client.list_all_models(force_refresh=True))
        await asyncio.wait_for(entered.wait(), timeout=1)
        client._mark_disconnected(None, connection_epoch=1, source="fixture")
        with self.assertRaisesRegex(AppServerDisconnected, "disconnect"):
            await pending
        self.assertEqual(calls, 1)

    async def test_stale_account_notification_cannot_clear_new_catalog(self):
        client = CodexAppServerClient("/tmp/unused.sock")
        client._connection_epoch = 2
        client._model_catalogs[False] = (dict(MODEL),)
        await client._handle_notification(
            {"method": "account/updated"}, connection_epoch=1
        )
        self.assertIn(False, client._model_catalogs)
        await client._handle_notification(
            {"method": "account/login/completed"}, connection_epoch=2
        )
        self.assertNotIn(False, client._model_catalogs)

    async def test_thread_existence_uses_exact_paginated_native_list(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "thread/list":
                params = message["params"]
                if not params["archived"] and params.get("cursor") is None:
                    return {
                        "id": message["id"],
                        "result": {
                            "data": [{"id": "thread-other"}],
                            "nextCursor": "page-two",
                        },
                    }
                if not params["archived"]:
                    return {
                        "id": message["id"],
                        "result": {"data": [], "nextCursor": None},
                    }
                return {
                    "id": message["id"],
                    "result": {
                        "data": [{"id": "thread-target"}],
                        "nextCursor": None,
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            exists = await client.thread_exists(
                "thread-target", cwd="/tmp", page_limit=1
            )
            await client.close()

        self.assertTrue(exists)
        calls = [
            item for item in server.received
            if item.get("method") == "thread/list"
        ]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["params"]["cwd"], "/tmp")
        self.assertNotIn("sourceKinds", calls[0]["params"])
        self.assertEqual(calls[1]["params"]["cursor"], "page-two")
        self.assertTrue(calls[2]["params"]["archived"])

    async def test_skills_are_cwd_scoped_force_reloaded_and_changed_only_invalidates(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "skills/list":
                return {
                    "id": message["id"],
                    "result": {"data": [{
                        "cwd": "/tmp", "errors": [], "skills": [SKILL],
                    }]},
                }
            if message.get("method") == "emit-change":
                await websocket.send(json.dumps({
                    "method": "skills/changed", "params": {},
                }))
                return {"id": message["id"], "result": {}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            first = await client.skills_for_cwd("/tmp", force_reload=True)
            cached = await client.skills_for_cwd("/tmp")
            await client.request("emit-change", {})
            for _ in range(20):
                if "/tmp" in client._dirty_skill_cwds:
                    break
                await asyncio.sleep(0.01)
            refreshed = await client.skills_for_cwd("/tmp")
            structured = await client.resolve_skill_inputs("/tmp", ["fixture"])
            await client.close()

        self.assertEqual(first, cached)
        self.assertEqual(refreshed["skills"], [SKILL])
        self.assertEqual(structured, [{
            "type": "skill", "name": "fixture", "path": "/tmp/SKILL.md",
        }])
        calls = [item for item in server.received if item.get("method") == "skills/list"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["params"]["forceReload"])
        self.assertTrue(calls[1]["params"]["forceReload"])
        self.assertEqual(calls[1]["params"]["cwds"], ["/tmp"])

    async def test_thread_start_defaults_to_no_tools_and_turns_use_structured_skill(self):
        async def responder(websocket, message):
            method = message.get("method")
            if method == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if method == "model/list":
                return {
                    "id": message["id"],
                    "result": {"data": [MODEL], "nextCursor": None},
                }
            if method == "skills/list":
                return {
                    "id": message["id"],
                    "result": {"data": [{
                        "cwd": "/tmp", "errors": [], "skills": [SKILL],
                    }]},
                }
            if method == "thread/start":
                return {
                    "id": message["id"],
                    "result": {"thread": {"id": "thread-dynamic"}},
                }
            if method == "turn/start":
                return {
                    "id": message["id"],
                    "result": {"turn": {"id": "turn-dynamic", "status": "inProgress"}},
                }
            if method == "turn/steer":
                return {
                    "id": message["id"],
                    "result": {"turnId": message["params"]["expectedTurnId"]},
                }
            if method == "thread/compact/start":
                return {"id": message["id"], "result": {}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            selected = await client.resolve_model_selection("fixture", "medium")
            skills = await client.resolve_skill_inputs(
                "/tmp", ["fixture"], force_reload=True
            )
            thread = await client.start_thread(
                cwd="/tmp", model=selected["model"],
            )
            await client.start_turn(
                thread["id"], "$fixture do it", skill_inputs=skills,
                leading_context="mobile upload context",
                options={
                    "effort": selected["reasoning_effort"],
                    "serviceTier": "priority",
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {
                            "model": selected["model"],
                            "reasoning_effort": selected["reasoning_effort"],
                            "developer_instructions": None,
                        },
                    },
                },
            )
            await client.start_turn(
                thread["id"], "$fixture user first", skill_inputs=skills,
                leading_context="mobile upload context",
                leading_context_after_text=True,
            )
            with self.assertRaisesRegex(AppServerProtocolError, "ordering"):
                await client.start_turn(
                    thread["id"], "invalid order",
                    leading_context_after_text="true",
                )
            await client.steer_turn(
                thread["id"], "turn-dynamic", "$fixture refine",
                skill_inputs=skills, leading_context="legacy steer context",
            )
            client._thread_cwds[thread["id"]] = "/other"
            with self.assertRaisesRegex(
                AppServerProtocolError, "thread cwd"
            ):
                await client.steer_turn(
                    thread["id"], "turn-dynamic", "$fixture cross cwd",
                    skill_inputs=skills,
                )
            await client.compact_thread(thread["id"])
            await client.close()

        thread_start = next(
            item for item in server.received if item.get("method") == "thread/start"
        )["params"]
        self.assertEqual(thread_start["approvalPolicy"], "never")
        self.assertEqual(thread_start["sandbox"], "danger-full-access")
        self.assertTrue(thread_start["experimentalRawEvents"])
        self.assertNotIn("developerInstructions", thread_start)
        self.assertEqual(thread_start["dynamicTools"], [])
        turn_messages = [
            item for item in server.received
            if item.get("method") in {"turn/start", "turn/steer"}
        ]
        self.assertEqual(turn_messages[0]["params"]["collaborationMode"], {
            "mode": "default",
            "settings": {
                "model": "fixture",
                "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        })
        self.assertEqual(turn_messages[0]["params"]["input"], [
            {"type": "text", "text": "mobile upload context"},
            {"type": "text", "text": "$fixture do it"},
            {"type": "skill", "name": "fixture", "path": "/tmp/SKILL.md"},
        ])
        self.assertEqual(turn_messages[1]["params"]["input"], [
            {"type": "text", "text": "$fixture user first"},
            {"type": "text", "text": "mobile upload context"},
            {"type": "skill", "name": "fixture", "path": "/tmp/SKILL.md"},
        ])
        self.assertEqual(turn_messages[2]["params"]["input"], [
            {"type": "text", "text": "legacy steer context"},
            {"type": "text", "text": "$fixture refine"},
            {"type": "skill", "name": "fixture", "path": "/tmp/SKILL.md"},
        ])
        self.assertNotIn("model", turn_messages[2]["params"])
        self.assertNotIn("effort", turn_messages[2]["params"])
        self.assertEqual(turn_messages[0]["params"]["serviceTier"], "priority")
        compact = next(
            item for item in server.received
            if item.get("method") == "thread/compact/start"
        )
        self.assertEqual(compact["params"], {"threadId": "thread-dynamic"})

    async def test_raw_new_context_is_projected_without_enqueuing_raw_payload(self):
        client = CodexAppServerClient("/tmp/unused.sock", notification_handler=lambda _message: None)
        client._connected = True
        client._initialized = True
        client._connection_epoch = 1
        call = {
            "method": "rawResponseItem/completed",
            "params": {
                "threadId": "thread-raw", "turnId": "turn-raw",
                "item": {
                    "type": "function_call", "name": "new_context",
                    "call_id": "call-secret", "arguments": "private arguments",
                },
            },
        }
        await client._handle_raw(json.dumps(call), 1)
        _epoch, projected_call = client._notifications.get_nowait()
        self.assertEqual(projected_call["method"], "internal/context-switch/observed")
        self.assertEqual(projected_call["params"]["state"], "call_seen")
        self.assertNotIn("call-secret", json.dumps(projected_call))
        self.assertNotIn("private arguments", json.dumps(projected_call))
        self.assertNotIn("item", projected_call["params"])

        await client._handle_raw(json.dumps({
            "method": "rawResponseItem/completed",
            "params": {
                "threadId": "thread-raw", "turnId": "turn-raw",
                "item": {
                    "type": "function_call_output", "call_id": "call-secret",
                    "output": "private output",
                },
            },
        }), 1)
        _epoch, projected_output = client._notifications.get_nowait()
        self.assertEqual(projected_output["params"]["state"], "output_seen")
        self.assertEqual(
            projected_output["params"]["callKeySha256"],
            projected_call["params"]["callKeySha256"],
        )
        self.assertNotIn("call-secret", json.dumps(projected_output))
        self.assertNotIn("private output", json.dumps(projected_output))

        await client._handle_raw(json.dumps({
            "method": "rawResponseItem/completed",
            "params": {
                "threadId": "thread-raw", "turnId": "turn-raw",
                "item": {
                    "type": "function_call_output", "call_id": "unmatched",
                    "output": "discard me",
                },
            },
        }), 1)
        self.assertTrue(client._notifications.empty())

    async def test_resumed_thread_restores_cwd_for_structured_skill_input(self):
        async def responder(websocket, message):
            method = message.get("method")
            if method == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if method == "skills/list":
                return {
                    "id": message["id"],
                    "result": {"data": [{
                        "cwd": "/tmp", "errors": [], "skills": [SKILL],
                    }]},
                }
            if method == "thread/resume":
                return {
                    "id": message["id"],
                    "result": {"thread": {
                        "id": "thread-resumed", "cwd": "/tmp",
                    }},
                }
            if method == "turn/steer":
                return {
                    "id": message["id"],
                    "result": {"turnId": "turn-resumed"},
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            skills = await client.resolve_skill_inputs(
                "/tmp", ["fixture"], force_reload=True
            )
            await client.resume_thread("thread-resumed")
            await client.steer_turn(
                "thread-resumed", "turn-resumed", "$fixture continue",
                skill_inputs=skills,
            )
            await client.close()

        steer = next(
            item for item in server.received if item.get("method") == "turn/steer"
        )
        resumed = next(
            item for item in server.received if item.get("method") == "thread/resume"
        )
        self.assertNotIn("experimentalRawEvents", resumed["params"])
        self.assertEqual(steer["params"]["input"][1], {
            "type": "skill", "name": "fixture", "path": "/tmp/SKILL.md",
        })

    async def test_concurrent_requests_use_futures_and_one_writer(self):
        waiting = []

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "echo":
                waiting.append((websocket, message))
                if len(waiting) == 2:
                    for target, request in reversed(waiting):
                        await target.send(json.dumps({
                            "id": request["id"],
                            "result": request["params"]["value"],
                        }))
                return None
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            results = await asyncio.gather(
                client.request("echo", {"value": "first"}),
                client.request("echo", {"value": "second"}),
            )
            await client.close()
        self.assertEqual(results, ["first", "second"])

    async def test_server_request_does_not_block_reader(self):
        release = asyncio.Event()
        handled = []

        async def handler(method, params):
            handled.append((method, params))
            await release.wait()
            return {"decision": "decline"}

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "probe":
                await websocket.send(json.dumps({
                    "id": "server-1", "method": "item/requestApproval",
                    "params": {"reason": "fixture"},
                }))
                return {"id": message["id"], "result": "reader-free"}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(
                server.path, server_request_handler=handler
            )
            await client.connect()
            result = await client.request("probe", {})
            self.assertEqual(result, "reader-free")
            release.set()
            for _ in range(20):
                if any(item.get("id") == "server-1" for item in server.received):
                    break
                await asyncio.sleep(0.01)
            await client.close()
        response = next(item for item in server.received if item.get("id") == "server-1")
        self.assertEqual(response["result"], {"decision": "decline"})
        self.assertEqual(handled[0][0], "item/requestApproval")

    async def test_notifications_keep_wire_order_without_blocking_rpc_responses(self):
        release = asyncio.Event()
        handled = []

        async def notification(message):
            sequence = message["params"]["sequence"]
            handled.append(("start", sequence))
            if sequence == 1:
                await release.wait()
            handled.append(("end", sequence))

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "probe-order":
                for sequence in (1, 2):
                    await websocket.send(json.dumps({
                        "method": "fixture/notification",
                        "params": {"sequence": sequence},
                    }))
                return {"id": message["id"], "result": "reader-free"}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(
                server.path, notification_handler=notification
            )
            await client.connect()
            result = await asyncio.wait_for(
                client.request("probe-order", {}), timeout=1
            )
            self.assertEqual(result, "reader-free")
            self.assertEqual(handled, [("start", 1)])
            release.set()
            for _ in range(50):
                if handled == [
                    ("start", 1), ("end", 1),
                    ("start", 2), ("end", 2),
                ]:
                    break
                await asyncio.sleep(0.01)
            await client.close()

        self.assertEqual(handled, [
            ("start", 1), ("end", 1),
            ("start", 2), ("end", 2),
        ])

    async def test_notification_queue_full_disconnects_instead_of_blocking_reader(self):
        disconnected = []
        client = CodexAppServerClient(
            "/tmp/unused.sock",
            notification_handler=lambda _message: None,
            disconnect_handler=lambda *args: disconnected.append(args),
        )
        client._connected = True
        client._initialized = True
        client._connection_epoch = 1
        client._socket = mock.AsyncMock()
        for sequence in range(client._notifications.maxsize):
            client._notifications.put_nowait((1, {"method": "fixture/full", "params": {"sequence": sequence}}))

        await client._handle_raw(json.dumps({
            "method": "fixture/notification", "params": {"sequence": "overflow"},
        }), 1)

        self.assertFalse(client.connected)
        client._socket.close.assert_awaited_once()
        self.assertEqual(disconnected[0][1], "notification_queue_full")

    async def test_native_request_prefix_full_queue_disconnects_without_blocking_reader(self):
        client=CodexAppServerClient('/tmp/unused.sock',notification_handler=lambda _message:None)
        client._connected=True;client._connection_epoch=1;client._socket=mock.AsyncMock()
        for _ in range(client._notifications.maxsize):
            client._notifications.put_nowait((1,{'method':'fixture/full','params':{}}))
        await client._handle_raw(json.dumps({'id':1,'method':'item/commandExecution/requestApproval','params':{}}),1)
        self.assertFalse(client.connected)
        client._socket.close.assert_awaited_once()
        self.assertFalse(client._notification_barriers)
        self.assertFalse(client._callback_tasks)

    async def test_close_before_native_callback_starts_cleans_reader_owned_prefix(self):
        client=CodexAppServerClient('/tmp/unused.sock',notification_handler=lambda _message:None)
        client._connected=True;client._connection_epoch=1
        await client._handle_raw(json.dumps({'id':1,'method':'item/fileChange/requestApproval','params':{}}),1)
        prefix=next(iter(client._notification_barriers))
        await client.close()
        self.assertFalse(client._callback_tasks)
        self.assertFalse(client._notification_barriers)
        self.assertTrue(prefix.done())
        self.assertFalse(prefix._log_traceback)
        await client.close()

    async def test_server_request_without_handler_is_rejected(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "probe":
                await websocket.send(json.dumps({
                    "id": "server-2", "method": "unknown/request", "params": {},
                }))
                return {"id": message["id"], "result": None}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            await client.request("probe", {})
            for _ in range(20):
                if any(item.get("id") == "server-2" for item in server.received):
                    break
                await asyncio.sleep(0.01)
            await client.close()
        response = next(item for item in server.received if item.get("id") == "server-2")
        self.assertEqual(response["error"]["code"], -32601)

    async def test_disconnect_fails_all_pending_requests(self):
        disconnects = []

        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "drop":
                await websocket.close()
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(
                server.path,
                request_timeout=1,
                disconnect_handler=lambda epoch, source, error: disconnects.append(
                    (epoch, source, error)
                ),
            )
            await client.connect()
            with self.assertRaises(AppServerDisconnected):
                await client.request("drop", {})
            self.assertFalse(client.connected)
            await client.close()
        self.assertEqual(len(disconnects), 1)
        self.assertEqual(disconnects[0][0], 1)
        self.assertIn(disconnects[0][1], {"reader_eof", "reader_error"})
        async with self.fake_server() as replacement:
            client.socket_path = Path(replacement.path)
            await client.connect()
            self.assertTrue(client.initialized)
            self.assertEqual(client.connection_epoch, 2)
            await client.close()
        self.assertEqual(len(disconnects), 1)

    async def test_turn_thread_and_interrupt_helpers_preserve_exact_boundaries(self):
        async def responder(websocket, message):
            method = message.get("method")
            if method == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if method == "thread/resume":
                return {
                    "id": message["id"],
                    "result": {"thread": {"id": message["params"]["threadId"]}},
                }
            if method == "thread/read":
                return {
                    "id": message["id"],
                    "result": {
                        "thread": {
                            "id": message["params"]["threadId"],
                            "turns": [],
                        },
                    },
                }
            if method == "thread/turns/list":
                return {
                    "id": message["id"],
                    "result": {
                        "data": [{
                            "id": "turn-old",
                            "items": [],
                            "itemsView": "full",
                            "status": "completed",
                        }],
                    },
                }
            if method == "turn/start":
                return {
                    "id": message["id"],
                    "result": {"turn": {"id": "turn-new", "status": "inProgress"}},
                }
            if method == "turn/steer":
                return {
                    "id": message["id"],
                    "result": {"turnId": message["params"]["expectedTurnId"]},
                }
            if method == "turn/interrupt":
                return {"id": message["id"], "result": {}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            await client.resume_thread("thread-one")
            read = await client.read_thread("thread-one")
            started = await client.start_turn(
                "thread-one", "new turn", client_user_message_id="message-one",
                options={"cwd": "/tmp", "sandbox": "danger-full-access"},
            )
            steered = await client.steer_turn(
                "thread-one", "turn-new", "more detail",
                client_user_message_id="message-two",
            )
            await client.interrupt_turn("thread-one", "turn-new")
            await client.close()

        self.assertEqual(started["id"], "turn-new")
        self.assertEqual(steered, "turn-new")
        self.assertEqual(read["thread"]["turns"][0]["id"], "turn-old")
        methods = [item.get("method") for item in server.received]
        self.assertEqual(methods, [
            "initialize", "initialized", "thread/resume", "thread/read",
            "thread/turns/list", "turn/start", "turn/steer", "turn/interrupt",
        ])
        self.assertEqual(server.received[3]["params"]["includeTurns"], False)
        self.assertEqual(server.received[4]["params"], {
            "threadId": "thread-one",
            "limit": 100,
            "sortDirection": "asc",
            "itemsView": "full",
        })
        start = server.received[5]["params"]
        steer = server.received[6]["params"]
        self.assertEqual(start["input"], [{"type": "text", "text": "new turn"}])
        self.assertEqual(start["clientUserMessageId"], "message-one")
        self.assertEqual(start["cwd"], "/tmp")
        self.assertEqual(start["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(steer["expectedTurnId"], "turn-new")
        self.assertEqual(steer["clientUserMessageId"], "message-two")
        self.assertEqual(set(steer), {
            "threadId", "expectedTurnId", "input", "clientUserMessageId"
        })

    async def test_thread_turn_pagination_is_bounded_and_full(self):
        async def responder(websocket, message):
            method = message.get("method")
            if method == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if method == "thread/read":
                return {
                    "id": message["id"],
                    "result": {"thread": {"id": "thread-one", "turns": []}},
                }
            if method == "thread/turns/list":
                cursor = message["params"].get("cursor")
                if cursor is None:
                    result = {
                        "data": [{
                            "id": "turn-one", "items": [],
                            "itemsView": "full", "status": "completed",
                        }],
                        "nextCursor": "cursor-two",
                    }
                else:
                    self.assertEqual(cursor, "cursor-two")
                    result = {
                        "data": [{
                            "id": "turn-two", "items": [{"type": "agentMessage"}],
                            "itemsView": "full", "status": "completed",
                        }],
                        "nextCursor": None,
                    }
                return {"id": message["id"], "result": result}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            value = await client.read_thread("thread-one")
            metadata_only = await client.read_thread(
                "thread-one", include_turns=False
            )
            with self.assertRaisesRegex(
                AppServerProtocolError, "turn page limit"
            ):
                await client.list_all_thread_turns("thread-one", page_limit=0)
            await client.close()

        self.assertEqual(
            [turn["id"] for turn in value["thread"]["turns"]],
            ["turn-one", "turn-two"],
        )
        self.assertEqual(metadata_only["thread"]["turns"], [])
        pages = [
            message["params"] for message in server.received
            if message.get("method") == "thread/turns/list"
        ]
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["limit"], 100)
        self.assertEqual(pages[1]["cursor"], "cursor-two")

    async def test_unmaterialized_thread_has_no_persisted_turns(self):
        async def responder(websocket, message):
            method = message.get("method")
            if method == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if method == "thread/read":
                return {
                    "id": message["id"],
                    "result": {"thread": {"id": "thread-new", "turns": []}},
                }
            if method == "thread/turns/list":
                return {
                    "id": message["id"],
                    "error": {
                        "code": -32600,
                        "message": (
                            "thread thread-new is not materialized yet; "
                            "thread/turns/list is unavailable before first user message"
                        ),
                    },
                }
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            value = await client.read_thread("thread-new")
            await client.close()

        self.assertEqual(value["thread"]["turns"], [])

    async def test_conversation_tree_stop_reads_exact_descendants_and_metadata(self):
        client = CodexAppServerClient("/tmp/not-used.sock")
        client.request = mock.AsyncMock(side_effect=[
            {
                "data": [
                    {
                        "id": "thread-child",
                        "parentThreadId": "thread-root",
                        "cwd": "/tmp/one",
                    },
                ],
                "nextCursor": "page-two",
            },
            {
                "data": [
                    {
                        "id": "thread-grandchild",
                        "parentThreadId": "thread-child",
                        "cwd": "/different/cwd",
                    },
                ],
                "nextCursor": None,
            },
            {"data": [], "nextCursor": None},
        ])

        self.assertTrue(client.supports_conversation_tree_stop)
        descendants = await client.list_descendant_threads("thread-root")

        self.assertEqual([item["id"] for item in descendants], [
            "thread-child", "thread-grandchild",
        ])
        calls = client.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["thread/list"] * 3)
        self.assertEqual(calls[0].args[1], {
            "ancestorThreadId": "thread-root",
            "archived": False,
            "useStateDbOnly": True,
            "limit": 100,
            "sourceKinds": [
                "cli", "vscode", "exec", "appServer", "subAgent",
                "subAgentReview", "subAgentCompact", "subAgentThreadSpawn",
                "subAgentOther", "unknown",
            ],
        })
        self.assertNotIn("cwd", calls[0].args[1])
        self.assertEqual(calls[1].args[1]["cursor"], "page-two")
        self.assertTrue(calls[2].args[1]['archived'])
        self.assertNotIn('cursor', calls[2].args[1])

        client.request = mock.AsyncMock(return_value={
            "data": [{
                "id": "turn-metadata", "status": "completed",
                "items": [], "itemsView": "notLoaded",
            }],
            "nextCursor": None,
        })
        metadata = await client.read_thread_turn_metadata("thread-child")
        self.assertEqual(metadata[0]["itemsView"], "notLoaded")
        self.assertEqual(metadata[0]["items"], [])
        self.assertEqual(client.request.call_args.args, (
            "thread/turns/list",
            {
                "threadId": "thread-child", "limit": 100,
                "sortDirection": "asc", "itemsView": "notLoaded",
            },
        ))

    async def test_conversation_tree_stop_rejects_malformed_descendant_pages(self):
        cases = (
            (
                "cross-tree",
                [{"data": [{"id": "thread-child", "parentThreadId": "thread-other"}]}],
                "leaves the ancestor tree",
            ),
            (
                "missing-parent",
                [{"data": [{"id": "thread-child"}]}],
                "parentThreadId",
            ),
            (
                "cycle",
                [{"data": [
                    {"id": "thread-child", "parentThreadId": "thread-grandchild"},
                    {"id": "thread-grandchild", "parentThreadId": "thread-child"},
                ]}],
                "cycle",
            ),
            (
                "ancestor-itself",
                [{"data": [{"id": "thread-root", "parentThreadId": "thread-root"}]}],
                "ancestor itself",
            ),
            (
                "repeated-id",
                [
                    {
                        "data": [{
                            "id": "thread-child",
                            "parentThreadId": "thread-root",
                        }],
                        "nextCursor": "next",
                    },
                    {"data": [{
                        "id": "thread-child",
                        "parentThreadId": "thread-root",
                    }]},
                ],
                "repeated a thread",
            ),
            (
                "repeated-cursor",
                [
                    {"data": [], "nextCursor": "next"},
                    {"data": [], "nextCursor": "next"},
                ],
                "repeated a cursor",
            ),
        )
        for name, responses, expected in cases:
            with self.subTest(name=name):
                client = CodexAppServerClient("/tmp/not-used.sock")
                client.request = mock.AsyncMock(side_effect=responses + [{"data":[], "nextCursor":None}])
                with self.assertRaisesRegex(AppServerProtocolError, expected):
                    await client.list_descendant_threads("thread-root")

        client = CodexAppServerClient("/tmp/not-used.sock")
        client.request = mock.AsyncMock(return_value={
            "data": [], "nextCursor": "more",
        })
        with mock.patch("engine.codex_app_server._MAX_THREAD_PAGES", 1):
            with self.assertRaisesRegex(AppServerProtocolError, "pagination exceeded"):
                await client.list_descendant_threads("thread-root")

    async def test_tree_stop_includes_archived_ancestors_of_live_grandchildren(self):
        client = CodexAppServerClient('/tmp/not-used.sock')
        client.request = mock.AsyncMock(side_effect=[
            {'data':[{'id':'grandchild','parentThreadId':'archived-parent'}]},
            {'data':[{'id':'archived-parent','parentThreadId':'root'}]},
        ])
        result = await client.list_descendant_threads('root')
        self.assertEqual({t['id'] for t in result}, {'grandchild','archived-parent'})
        self.assertEqual([call.args[1]['archived'] for call in client.request.call_args_list],[False,True])
        client.request = mock.AsyncMock(side_effect=[
            {'data':[{'id':'child','parentThreadId':'root'}]},
            {'data':[{'id':'child','parentThreadId':'root'}]},
        ])
        with self.assertRaisesRegex(AppServerProtocolError,'repeated a thread'):
            await client.list_descendant_threads('root')

    async def test_conversation_tree_stop_is_not_available_on_old_fixture(self):
        client = CodexAppServerClient(
            "/tmp/not-used.sock", fixture_path=PROTOCOL_FIXTURE_0_156_0
        )
        self.assertFalse(client.supports_conversation_tree_stop)
        for operation in (
            client.list_descendant_threads("thread-root"),
            client.read_thread_turn_metadata("thread-root"),
            client.list_background_terminals("thread-root"),
            client.clean_background_terminals("thread-root"),
        ):
            with self.assertRaisesRegex(AppServerProtocolError, "unsupported"):
                await operation

    async def test_conversation_tree_stop_pages_terminals_and_requires_clean_empty_response(self):
        terminal_one = {
            "command": "first", "cwd": "/tmp", "itemId": "item-one",
            "processId": "process-one",
        }
        terminal_two = {
            "command": "second", "cwd": "/other", "itemId": "item-two",
            "processId": "process-two",
        }
        client = CodexAppServerClient("/tmp/not-used.sock")
        client.request = mock.AsyncMock(side_effect=[
            {"data": [terminal_one], "nextCursor": "page-two"},
            {"data": [terminal_two], "nextCursor": None},
        ])
        terminals = await client.list_background_terminals("thread-child")
        self.assertEqual(terminals, [terminal_one, terminal_two])
        self.assertEqual(client.request.call_args_list[0].args, (
            "thread/backgroundTerminals/list",
            {"threadId": "thread-child", "limit": 100},
        ))
        self.assertEqual(
            client.request.call_args_list[1].args[1]["cursor"], "page-two"
        )

        client.request = mock.AsyncMock(return_value={
            "data": [{
                "command": "missing process ID", "cwd": "/tmp",
                "itemId": "item-bad", "processId": "",
            }],
        })
        with self.assertRaisesRegex(AppServerProtocolError, "processId is invalid"):
            await client.list_background_terminals("thread-child")

        client.request = mock.AsyncMock(side_effect=[
            {"data": [], "nextCursor": "again"},
            {"data": [], "nextCursor": "again"},
        ])
        with self.assertRaisesRegex(AppServerProtocolError, "repeated a cursor"):
            await client.list_background_terminals("thread-child")

        client.request = mock.AsyncMock(side_effect=AppServerRPCError(
            -32600, "thread not found: thread-child"
        ))
        with self.assertRaisesRegex(AppServerRPCError, "thread not found"):
            await client.list_background_terminals("thread-child")

        client.request = mock.AsyncMock(return_value={})
        await client.clean_background_terminals("thread-child")
        self.assertEqual(client.request.call_args.args, (
            "thread/backgroundTerminals/clean", {"threadId": "thread-child"}
        ))
        client.request = mock.AsyncMock(return_value={"unexpected": True})
        with self.assertRaisesRegex(AppServerProtocolError, "empty object"):
            await client.clean_background_terminals("thread-child")

    async def test_thread_name_set_has_exact_wire_and_empty_result(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": dict(
                    INITIALIZE_RESULT, userAgent="codex_app_server/0.160.0"
                )}
            if message.get("method") == "thread/name/set":
                return {"id": message["id"], "result": {}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertTrue(client.supports_thread_name_set)
            await client.set_thread_name("thread-one", "中文原生标题")
            with self.assertRaisesRegex(AppServerProtocolError, "thread name"):
                await client.set_thread_name("thread-one", " ")
            await client.close()

        updates = [
            message for message in server.received
            if message.get("method") == "thread/name/set"
        ]
        self.assertEqual([message["params"] for message in updates], [{
            "threadId": "thread-one", "name": "中文原生标题",
        }])

    async def test_thread_name_set_rejects_nonempty_response_and_old_fixture(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": dict(
                    INITIALIZE_RESULT, userAgent="codex_app_server/0.160.0"
                )}
            if message.get("method") == "thread/name/set":
                return {"id": message["id"], "result": {"unexpected": True}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with self.assertRaisesRegex(AppServerProtocolError, "empty object"):
                await client.set_thread_name("thread-one", "title")
            await client.close()

        legacy = CodexAppServerClient(
            "/tmp/not-used.sock", fixture_path=PROTOCOL_FIXTURE_0_156_0
        )
        self.assertFalse(legacy.supports_thread_name_set)
        with self.assertRaisesRegex(AppServerProtocolError, "unsupported"):
            await legacy.set_thread_name("thread-one", "title")

    async def test_experimental_thread_settings_update_has_exact_wire_and_empty_result(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "thread/settings/update":
                return {"id": message["id"], "result": {}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            await client.update_thread_settings("thread-one", "priority")
            await client.update_thread_settings("thread-one", None)
            await client.close()

        updates = [
            message for message in server.received
            if message.get("method") == "thread/settings/update"
        ]
        self.assertEqual([message["params"] for message in updates], [
            {"threadId": "thread-one", "serviceTier": "priority"},
            {"threadId": "thread-one", "serviceTier": None},
        ])

    async def test_thread_settings_update_rejects_nonempty_result_and_invalid_tier(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if message.get("method") == "thread/settings/update":
                return {"id": message["id"], "result": {"unexpected": True}}
            return None

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with self.assertRaisesRegex(AppServerProtocolError, "empty object"):
                await client.update_thread_settings("thread-one", "priority")
            with self.assertRaisesRegex(AppServerProtocolError, "service tier"):
                await client.update_thread_settings("thread-one", "")
            await client.close()

    async def test_rpc_error_preserves_code_message_and_data(self):
        async def responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if "id" not in message:
                return None
            return {
                "id": message["id"],
                "error": {"code": -32001, "message": "nope", "data": {"x": 1}},
            }

        async with self.fake_server(responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with self.assertRaises(AppServerRPCError) as raised:
                await client.request("fail", {})
            await client.close()
        self.assertEqual(raised.exception.code, -32001)
        self.assertEqual(raised.exception.data, {"x": 1})

    async def test_version_and_schema_gates_fail_closed(self):
        for user_agent in (
            "codex_app_server/0.149.0",
            "codex_app_server/0.153.2",
            "codex_app_server/0.153.1.99",
            "codex_app_server/0.154.1",
            "codex_app_server/0.154.0.99",
            "codex_app_server/0.152.1.99",
            "codex_app_server/0.152.1/0.151.0-alpha.7.2",
        ):
            bad_version = dict(INITIALIZE_RESULT, userAgent=user_agent)

            async def responder(websocket, message):
                if message.get("method") == "initialize":
                    return {"id": message["id"], "result": bad_version}
                return {"id": message["id"], "result": {"data": [{}]}}

            with self.subTest(user_agent=user_agent):
                async with self.fake_server(responder) as server:
                    client = CodexAppServerClient(server.path)
                    with self.assertRaises(AppServerProtocolError):
                        await client.connect()

        supported_previous = dict(
            INITIALIZE_RESULT,
            userAgent="codex_app_server/0.151.0-alpha.7.2",
        )

        async def previous_responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": supported_previous}
            return None

        async with self.fake_server(previous_responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertEqual(client.server_version, "0.151.0-alpha.7.2")
            await client.close()

        supported_current = dict(
            INITIALIZE_RESULT,
            userAgent="Codex Desktop/0.152.1 (fixture platform)",
        )

        async def current_responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": supported_current}
            return None

        async with self.fake_server(current_responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertEqual(client.server_version, "0.152.1")
            await client.close()

        supported_latest = dict(
            INITIALIZE_RESULT,
            userAgent="Codex Desktop/0.154.0 (fixture platform)",
        )

        async def latest_responder(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": supported_latest}
            return None

        async with self.fake_server(latest_responder) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            self.assertEqual(client.server_version, "0.154.0")
            await client.close()

        async def malformed_models(websocket, message):
            if message.get("method") == "initialize":
                return {"id": message["id"], "result": INITIALIZE_RESULT}
            if "id" not in message:
                return None
            return {"id": message["id"], "result": {"data": [{}]}}

        async with self.fake_server(malformed_models) as server:
            client = CodexAppServerClient(server.path)
            await client.connect()
            with self.assertRaises(AppServerProtocolError):
                await client.list_models()
            await client.close()

    async def test_socket_must_be_owner_only(self):
        async with self.fake_server() as server:
            os.chmod(server.path, 0o660)
            client = CodexAppServerClient(server.path)
            with self.assertRaisesRegex(AppServerError, "owner-only"):
                await client.connect()

    def test_current_and_rollback_fixtures_are_locked(self):
        current = json.loads(PROTOCOL_FIXTURE_0_160_0.read_text(encoding="utf-8"))
        self.assertIn(PROTOCOL_FIXTURE_0_160_0, PROTOCOL_FIXTURES)
        self.assertEqual(current["cliVersion"], "codex-cli 0.160.0")
        self.assertTrue(current["conversationTreeStop"])
        self.assertEqual(current["methods"]["thread/list"], {
            "params": [
                "ancestorThreadId", "archived", "cursor", "cwd", "limit",
                "modelProviders", "originators", "parentThreadId", "projectId",
                "searchTerm", "sectionId", "sortDirection", "sortKey",
                "sourceKinds", "useStateDbOnly",
            ],
            "responseRequired": ["data"],
            "entryRequired": ["id", "parentThreadId"],
            "paginationField": "nextCursor",
        })
        self.assertEqual(current["methods"]["thread/backgroundTerminals/list"], {
            "params": ["cursor", "limit", "threadId"],
            "responseRequired": ["data"],
            "entryRequired": ["command", "cwd", "itemId", "processId"],
            "paginationField": "nextCursor",
        })
        self.assertEqual(current["methods"]["thread/backgroundTerminals/clean"], {
            "params": ["threadId"], "responseEquals": {},
        })

        latest = json.loads(PROTOCOL_FIXTURE_0_154_0.read_text(encoding="utf-8"))
        self.assertIn(PROTOCOL_FIXTURE_0_154_0, PROTOCOL_FIXTURES)
        self.assertEqual(latest["cliVersion"], "codex-cli 0.154.0")
        self.assertEqual(
            latest["schemaSha256"],
            "d71ddf3bf5484f8de2799f7a4793c2e66808a9ec1a330e2307accb088ab5948a",
        )
        self.assertEqual(
            latest["experimentalSchemaSha256"],
            "24df528acec2952e6b96c1c2b061f98e60177d059e12c90cf318621380c9de9e",
        )
        self.assertEqual(
            latest["codexSha256"],
            "3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022",
        )
        self.assertEqual(
            latest["codeModeHostSha256"],
            "0c57be435e73b70d9106c850d751cd259a7f04da958a453d7ef59090d82b70f1",
        )
        self.assertEqual(
            latest["sourcePackage"],
            "OpenAI standalone release codex-package-x86_64-unknown-linux-musl 0.154.0",
        )
        self.assertEqual(
            latest["sourceOrigin"],
            "releases.openai.com official latest package, archive SHA-256 verified",
        )
        previous = json.loads(
            PROTOCOL_FIXTURE_0_153_1.read_text(encoding="utf-8")
        )
        self.assertEqual(latest["methods"], previous["methods"])
        self.assertTrue(latest["requestUserInputDefaultModeConfig"])
        self.assertEqual(latest["initialize"], previous["initialize"])
        self.assertEqual(previous["cliVersion"], "codex-cli 0.153.1")
        self.assertEqual(
            previous["schemaSha256"],
            "e8284c5cb8157554a3dd1e035aadbd4325aea501af56887e9c2e12eb1b9b9448",
        )
        self.assertEqual(
            previous["experimentalSchemaSha256"],
            "b06f77062369d481a59cc70720c12b89cb9dd49c385863923262102d3ad6c978",
        )
        self.assertEqual(
            previous["codexSha256"],
            "b9315df68cb0e2827c940ffacb66f7524e820d9744060c755fbf938724ad2b76",
        )
        self.assertEqual(
            previous["codeModeHostSha256"],
            "c215117917037afd6b7621ba11ca18054ebdb890c314454f485b27574a73e1b7",
        )
        fixture = json.loads(PROTOCOL_FIXTURE_0_152_1.read_text(encoding="utf-8"))
        self.assertEqual(fixture["cliVersion"], "codex-cli 0.152.1")
        self.assertEqual(
            fixture["schemaSha256"],
            "d8faa38d5f00aa7ddfe635a2d374ee5f871ffd217d4d175c72fbe7f009f4f669",
        )
        self.assertEqual(
            fixture["experimentalSchemaSha256"],
            "65ce2db95109ebedc995d0ff74cc8bcefaa69fc3270f65b4fc69c1877c651f4e",
        )
        self.assertEqual(
            fixture["sourcePackage"], "@openai/codex@0.152.1-linux-x64"
        )
        self.assertEqual(
            fixture["codexSha256"],
            "b82018241214a4a7c6b97b198585192d1dbc3aab1ddcdc640f04d8dee8c606f9",
        )
        self.assertEqual(
            fixture["codeModeHostSha256"],
            "681833a32394f5aa64eaf3f2fff0f1aede28bf9a93e92b9783d3486a06429f65",
        )
        self.assertEqual(set(fixture["methods"]), {
            "model/list", "skills/list", "thread/start", "item/tool/call",
            "turn/input/skill", "thread/settings/update", "thread/turns/list",
        })
        rollback = json.loads(
            PROTOCOL_FIXTURE_0_151.read_text(encoding="utf-8")
        )
        self.assertEqual(
            rollback["cliVersion"], "codex-cli 0.151.0-alpha.7.2"
        )
        self.assertEqual(
            rollback["schemaSha256"],
            "424b204943b18e5ffa52667a2aa397c9950730ec1e49ad767e2a016743990541",
        )
        self.assertEqual(
            rollback["experimentalSchemaSha256"],
            "31ae67beb2c94cc9509f6a71968600062dc8c6d7fe45437ed3a9129838f4d2d9",
        )


if __name__ == "__main__":
    unittest.main()
