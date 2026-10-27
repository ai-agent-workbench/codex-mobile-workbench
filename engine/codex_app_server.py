"""Validated JSON-RPC transport for the Codex App Server Unix socket."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import math
import os
import re
import stat
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets
from shared.diagnostics import emit


LOGGER = logging.getLogger(__name__)


_FIXTURE_DIR = Path(__file__).with_name("fixtures")
PROTOCOL_FIXTURE_0_151 = _FIXTURE_DIR / "codex_app_server_protocol_0_151.json"
PROTOCOL_FIXTURE_0_152_1 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_152_1.json"
)
PROTOCOL_FIXTURE_0_153_1 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_153_1.json"
)
PROTOCOL_FIXTURE_0_154_0 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_154_0.json"
)
PROTOCOL_FIXTURE_0_156_0 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_156_0.json"
)
PROTOCOL_FIXTURE_0_159_2 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_159_2.json"
)
PROTOCOL_FIXTURE_0_160_0 = (
    _FIXTURE_DIR / "codex_app_server_protocol_0_160_0.json"
)
PROTOCOL_FIXTURES = (
    PROTOCOL_FIXTURE_0_151,
    PROTOCOL_FIXTURE_0_152_1,
    PROTOCOL_FIXTURE_0_153_1,
    PROTOCOL_FIXTURE_0_154_0,
    PROTOCOL_FIXTURE_0_156_0,
    PROTOCOL_FIXTURE_0_159_2,
    PROTOCOL_FIXTURE_0_160_0,
)
# The newest validated fixture is the public default, while the client keeps
# the previous fixture available so App Server runtime promotion and rollback
# do not require an unsafe lock-step Web deployment.
PROTOCOL_FIXTURE = PROTOCOL_FIXTURE_0_160_0


class AppServerError(RuntimeError):
    """Base error for the dormant App Server adapter."""


class AppServerDisconnected(AppServerError):
    """The Unix WebSocket disconnected while work was pending."""


class _ModelCatalogRefreshInvalidated(AppServerDisconnected):
    """A refresh finished after its connection/catalog generation was retired."""


class AppServerProtocolError(AppServerError):
    """The peer or protocol fixture doesn't match the locked schema."""


class AppServerTurnTerminal(AppServerProtocolError):
    """A dynamic tool request arrived after its mapped turn became terminal."""


class AppServerRPCError(AppServerError):
    """A JSON-RPC error returned by the App Server."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"App Server RPC {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


ServerRequestHandler = Callable[..., Awaitable[Any]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
DisconnectHandler = Callable[[int, str, str], None]


_TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
_TOOL_ITEM_TYPES = {
    "mcpToolCall",
    "dynamicToolCall",
    "collabAgentToolCall",
    "collabToolCall",
    "webSearch",
    "imageView",
    "imageGeneration",
}
_WAITING_ITEM_TYPES = {"elicitation", "requestUserInput", "askUserQuestion"}
_SKILL_SCOPES = {"user", "repo", "system", "admin"}
_MAX_MODEL_PAGES = 100
_MODEL_CATALOG_TTL_SECONDS = 60.0
_MODEL_CATALOG_FAILURE_COOLDOWN_SECONDS = 5.0
_MAX_THREAD_PAGES = 100
_MAX_TURN_PAGES = 100
_THREAD_LIST_PAGE_LIMIT = 100
_THREAD_SOURCE_KINDS = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)
# Item pages have a separate bound because a single turn may legitimately
# contain far more items than a thread has turns.  This still prevents an
# unbounded cursor loop while preserving long persisted histories.
_MAX_ITEM_PAGES = 10_000
# Item pages are deliberately smaller than turn pages: a single persisted item
# can be much larger than ordinary turn metadata.  The WebSocket message cap
# remains the final, fail-closed bound for any individual item/page.
_THREAD_ITEM_PAGE_LIMIT = 10
_CONVERSATION_SCHEMA_VERSION = 1
_MAX_CONVERSATION_TEXT = 12_000
_MAX_CONVERSATION_DETAIL = 12_000
_MAX_CONVERSATION_ITEMS = 200
_MAX_CONVERSATION_SUMMARY_PARTS = 12
_MAX_CONVERSATION_SUMMARY_PART_TEXT = 1_000
_CONVERSATION_FLUSH_SECONDS = 0.35
_MAX_NOTIFICATION_QUEUE = 1000
# Native thread snapshots can exceed websockets' 1 MiB default.  The peer is
# an owner-only Unix socket, but keep a bounded ceiling for memory safety.
_MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024 * 1024


class ServerRequestResponse:
    """A server-request result with a post-wire acknowledgement hook.

    A JSON-RPC server request is not delivered merely because it was put into
    the local writer queue.  The hook runs only after the sole socket writer
    has completed ``send`` for the response frame.
    """

    def __init__(self, result: Any, on_sent: Callable[[], Awaitable[None] | None]):
        self.result = result
        self.on_sent = on_sent


class CodexAppServerClient:
    """One initialized JSON-RPC connection to a Unix WebSocket listener."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        request_timeout: float = 30.0,
        server_request_handler: ServerRequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
        disconnect_handler: DisconnectHandler | None = None,
        fixture_path: str | os.PathLike[str] | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.request_timeout = request_timeout
        self.server_request_handler = server_request_handler
        self.notification_handler = notification_handler
        self.disconnect_handler = disconnect_handler
        # Unix listeners speak WebSocket directly.  A Unix connection must
        # never inherit the host HTTP proxy settings.
        fixture_paths = (
            (Path(fixture_path),)
            if fixture_path is not None
            else PROTOCOL_FIXTURES
        )
        self._fixtures_by_version: dict[str, dict[str, Any]] = {}
        for path in fixture_paths:
            fixture = self._load_fixture(path)
            version = fixture["serverVersion"]
            if version in self._fixtures_by_version:
                raise AppServerProtocolError(
                    f"duplicate protocol fixture for App Server {version}"
                )
            self._fixtures_by_version[version] = fixture
        self._fixture = (
            self._fixtures_by_version[
                self._load_fixture(PROTOCOL_FIXTURE)["serverVersion"]
            ]
            if fixture_path is None
            else next(iter(self._fixtures_by_version.values()))
        )
        self._socket = None
        self._outbound: asyncio.Queue[tuple[str, asyncio.Future[None], int]] = asyncio.Queue()
        self._notifications: asyncio.Queue[
            tuple[int, dict[str, Any] | asyncio.Future[None]]
        ] = asyncio.Queue(
            maxsize=_MAX_NOTIFICATION_QUEUE
        )
        self._notification_barriers: set[asyncio.Future[None]] = set()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._connected = False
        self._initialized = False
        self._disconnect_error: AppServerDisconnected | None = None
        self._connection_epoch = 0
        self._connection_instance = uuid.uuid4().hex
        # Keep immutable catalog payloads separate from connection-local
        # freshness and single-flight bookkeeping.
        self._model_catalogs: dict[bool, tuple[dict[str, Any], ...]] = {}
        self._model_catalog_cached_at: dict[bool, float] = {}
        self._model_catalog_versions: dict[bool, int] = {}
        self._model_catalog_refresh_tasks: dict[bool, asyncio.Task[list[dict[str, Any]]]] = {}
        # Cancelled refreshes can still be inside a cancellation-resistant test
        # double or transport await. Retain them until their done callback so
        # close can join every task it retired.
        self._retired_model_catalog_refresh_tasks: set[asyncio.Task[list[dict[str, Any]]]] = set()
        self._model_catalog_refresh_cooldown_until: dict[bool, float] = {}
        self._model_catalog_generation = 0
        self._model_catalog_nonaccount_lifecycle_serial = 0
        # Keep cancellation provenance on each retired task rather than using
        # a mutable "last lifecycle event".  Foreground callers may safely
        # retry exactly the account-update invalidation that cancelled their
        # own task, but must never turn close/disconnect/reconnect into a
        # retry.  Weak keys retain this evidence until a waiter observes the
        # terminal task without retaining completed refreshes forever.
        self._model_catalog_refresh_invalidations: weakref.WeakKeyDictionary[
            asyncio.Task[list[dict[str, Any]]], tuple[str, int, int]
        ] = weakref.WeakKeyDictionary()
        self._model_catalog_refresh_origins: weakref.WeakKeyDictionary[
            asyncio.Task[list[dict[str, Any]]], tuple[int, int]
        ] = weakref.WeakKeyDictionary()
        self._skills_by_cwd: dict[str, dict[str, Any]] = {}
        self._dirty_skill_cwds: set[str] = set()
        self._thread_models: dict[str, str] = {}
        self._thread_cwds: dict[str, str] = {}
        # Raw Responses items are internal-only and can carry full prompt,
        # reasoning, and tool data. Keep only transient call identities needed
        # to recognize a matching new_context output; never enqueue them.
        self._pending_context_switch_calls: set[tuple[str, str, str]] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def server_version(self) -> str:
        return self._fixture["serverVersion"]

    @property
    def connection_epoch(self) -> int:
        return self._connection_epoch

    @property
    def connection_id(self) -> str:
        """Private process-instance plus epoch identity for server requests."""
        return self._connection_instance + ":" + str(self._connection_epoch)

    async def connect(self) -> dict[str, Any]:
        """Connect directly to the protected socket and initialize the peer."""
        if self._connected:
            raise AppServerError("App Server client is already connected")
        if (
            self._socket is not None
            or self._reader_task
            or self._writer_task
            or self._notification_task
        ):
            await self.close()
        connect_path = self._check_socket_security()
        self._outbound = asyncio.Queue()
        self._notifications = asyncio.Queue(maxsize=_MAX_NOTIFICATION_QUEUE)
        self._disconnect_error = None
        self._clear_model_catalogs(reason="connect")
        self._skills_by_cwd.clear()
        self._dirty_skill_cwds.clear()
        self._thread_models.clear()
        self._thread_cwds.clear()
        try:
            self._socket = await websockets.unix_connect(
                path=str(connect_path), uri="ws://localhost/", proxy=None,
                open_timeout=self.request_timeout, ping_interval=20, ping_timeout=20,
                compression=None, max_size=_MAX_WEBSOCKET_MESSAGE_BYTES,
            )
            self._connection_epoch += 1
            connection_epoch = self._connection_epoch
            self._connected = True
            self._writer_task = asyncio.create_task(
                self._writer(connection_epoch), name="codex-app-server-writer"
            )
            self._notification_task = asyncio.create_task(
                self._notification_worker(connection_epoch),
                name="codex-app-server-notifications",
            )
            self._reader_task = asyncio.create_task(
                self._reader(connection_epoch), name="codex-app-server-reader"
            )
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-web-workbench",
                        "title": "Codex Web Workbench",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True, "extensions": {"openai/form": {}}},
                },
            )
            self._validate_initialize(result)
            await self.notify("initialized")
            self._initialized = True
            emit("app_server_connected", connection_epoch=connection_epoch, server_version=self.server_version)
            return result
        except BaseException as error:
            emit("app_server_connect_failed", level="error", exception_type=type(error).__name__, message=str(error))
            await self.close()
            raise

    async def close(self) -> None:
        """Close this connection and fail every unresolved request."""
        self._initialized = False
        self._connected = False
        refresh_tasks = self._clear_model_catalogs(reason="close")
        self._fail_notification_barriers(
            AppServerDisconnected("App Server connection closed")
        )
        self._pending_context_switch_calls.clear()
        socket = self._socket
        self._socket = None
        if socket is not None:
            await socket.close()
        tasks = [
            task
            for task in (
                self._reader_task,
                self._writer_task,
                self._notification_task,
            )
            if task
        ]
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not current),
                return_exceptions=True,
            )
        self._reader_task = None
        self._writer_task = None
        self._notification_task = None
        # Catalog refreshes are independent of callback_tasks. Never await the
        # task closing us (or a task already done), but otherwise leave no
        # detached catalog GET alive after close returns.
        refresh_waiters = tuple(
            task for task in refresh_tasks
            if task is not current and not task.done()
        )
        if refresh_waiters:
            await asyncio.gather(*refresh_waiters, return_exceptions=True)
        for task in tuple(self._callback_tasks):
            task.cancel()
        if self._callback_tasks:
            await asyncio.gather(*self._callback_tasks, return_exceptions=True)
        self._callback_tasks.clear()
        self._fail_notification_barriers(
            AppServerDisconnected("App Server connection closed")
        )
        self._fail_pending(
            self._disconnect_error
            or AppServerDisconnected("App Server connection closed")
        )

    async def request(self, method: str, params: Any = None) -> Any:
        """Send one request and resolve its per-request Future."""
        if not self._connected:
            raise self._disconnect_error or AppServerDisconnected(
                "App Server isn't connected"
            )
        if method != "initialize" and not self._initialized:
            raise AppServerProtocolError("initialize handshake isn't complete")
        self._next_request_id += 1
        request_id = self._next_request_id
        started = __import__("time").perf_counter()
        rpc_epoch, rpc_connection = self._connection_epoch, self.connection_id
        emit("app_server_rpc_start", method=method, rpc_id=request_id, connection_epoch=rpc_epoch, connection_id=rpc_connection)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        def rpc_done(done):
            elapsed = round((__import__("time").perf_counter() - started) * 1000, 3)
            if done.cancelled():
                emit("app_server_rpc_end", level="error", method=method, rpc_id=request_id, connection_epoch=rpc_epoch, connection_id=rpc_connection, duration_ms=elapsed, failure="cancelled")
                return
            error = done.exception()
            if error is not None:
                emit("app_server_rpc_end", level="error", method=method, rpc_id=request_id, connection_epoch=rpc_epoch, connection_id=rpc_connection, duration_ms=elapsed, exception_type=type(error).__name__, rpc_error_code=getattr(error, "code", None), message=str(error))
            else:
                emit("app_server_rpc_end", method=method, rpc_id=request_id, connection_epoch=rpc_epoch, connection_id=rpc_connection, duration_ms=elapsed)
        future.add_done_callback(rpc_done)
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._enqueue(message)
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
            raise
        try:
            async with asyncio.timeout(self.request_timeout):
                return await future
        except TimeoutError as error:
            emit("app_server_rpc_timeout", level="error", method=method, rpc_id=request_id, connection_id=rpc_connection, timeout_seconds=self.request_timeout)
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise AppServerError(
                f"App Server request timed out: {method}"
            ) from error
        finally:
            # A cancelled waiter no longer owns an RPC completion. Its wire
            # reply may still arrive, but must never poison this shared socket.
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: Any = None) -> None:
        """Queue a client notification through the connection's sole writer."""
        if not self._connected:
            raise self._disconnect_error or AppServerDisconnected(
                "App Server isn't connected"
            )
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._enqueue(message)

    async def flush_notifications(self) -> None:
        """Wait for callbacks queued before this local notification barrier.

        This is deliberately not a JSON-RPC method.  It only establishes that
        the single local notification worker has applied prior wire events.
        """
        if not self._connected:
            raise self._disconnect_error or AppServerDisconnected(
                "App Server isn't connected"
            )
        future = asyncio.get_running_loop().create_future()
        epoch = self._connection_epoch
        self._notification_barriers.add(future)
        put_task = asyncio.create_task(self._notifications.put((epoch, future)))
        try:
            async with asyncio.timeout(self.request_timeout):
                done, _ = await asyncio.wait(
                    {put_task, future}, return_when=asyncio.FIRST_COMPLETED
                )
                if future in done:
                    await future
                else:
                    await put_task
                    await future
                if not self._connected or epoch != self._connection_epoch:
                    raise self._disconnect_error or AppServerDisconnected(
                        "App Server disconnected during notification flush"
                    )
        except TimeoutError as error:
            raise AppServerError("App Server notification flush timed out") from error
        finally:
            self._notification_barriers.discard(future)
            if not put_task.done():
                put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def list_models(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        include_hidden: bool | None = None,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in {
                "cursor": cursor,
                "limit": limit,
                "includeHidden": include_hidden,
            }.items()
            if value is not None
        }
        result = await self.request("model/list", params)
        self._validate_method_result("model/list", result)
        return result

    async def read_weekly_rate_limit(self) -> dict[str, Any]:
        """Read and strictly project the primary Codex seven-day quota window."""
        result = await self.request("account/rateLimits/read")
        return self._normalize_weekly_rate_limit(result)

    @staticmethod
    def _normalize_weekly_rate_limit(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise AppServerProtocolError("account rate limits result must be an object")
        limits = result.get("rateLimits")
        if not isinstance(limits, dict):
            by_id = result.get("rateLimitsByLimitId")
            limits = by_id.get("codex") if isinstance(by_id, dict) else None
        if not isinstance(limits, dict):
            raise AppServerProtocolError("Codex rate limits are unavailable")

        weekly = None
        for field in ("primary", "secondary"):
            candidate = limits.get(field)
            if (
                isinstance(candidate, dict)
                and candidate.get("windowDurationMins") == 7 * 24 * 60
            ):
                weekly = candidate
                break
        if weekly is None:
            raise AppServerProtocolError("Codex weekly rate limit is unavailable")

        used_percent = weekly.get("usedPercent")
        resets_at = weekly.get("resetsAt")
        if (
            isinstance(used_percent, bool)
            or not isinstance(used_percent, (int, float))
            or not math.isfinite(used_percent)
            or not 0 <= used_percent <= 100
        ):
            raise AppServerProtocolError("Codex weekly used percentage is invalid")
        if (
            isinstance(resets_at, bool)
            or not isinstance(resets_at, (int, float))
            or not math.isfinite(resets_at)
            or resets_at <= 0
        ):
            raise AppServerProtocolError("Codex weekly reset time is invalid")

        remaining_percent = round(100 - float(used_percent), 1)
        if remaining_percent.is_integer():
            remaining_percent = int(remaining_percent)
        plan_type = limits.get("planType")
        if plan_type is not None and (
            not isinstance(plan_type, str) or len(plan_type) > 48
        ):
            raise AppServerProtocolError("Codex plan type is invalid")
        return {
            "remaining_percent": remaining_percent,
            "resets_at": int(resets_at),
            "plan_type": plan_type or None,
        }

    def _clear_model_catalogs(
        self, *, reason: str = "lifecycle"
    ) -> set[asyncio.Task[list[dict[str, Any]]]]:
        """Invalidate payloads and retire every refresh from this connection."""
        self._model_catalog_generation += 1
        self._model_catalogs.clear()
        self._model_catalog_cached_at.clear()
        self._model_catalog_versions.clear()
        self._model_catalog_refresh_cooldown_until.clear()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        tasks = set(self._retired_model_catalog_refresh_tasks)
        tasks.update(self._model_catalog_refresh_tasks.values())
        self._model_catalog_refresh_tasks.clear()
        self._retired_model_catalog_refresh_tasks.update(tasks)
        if reason != "account update":
            self._model_catalog_nonaccount_lifecycle_serial += 1
        invalidation = (
            reason,
            self._connection_epoch,
            self._model_catalog_nonaccount_lifecycle_serial,
        )
        for task in tasks:
            # Account bursts all invalidate the same old read; a foreground
            # caller gets one fresh request from the latest generation.  Any
            # later non-account lifecycle transition advances the serial and
            # permanently vetoes that retry, even after reconnect.
            self._model_catalog_refresh_invalidations[task] = invalidation
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        return tasks

    def _model_catalog_refresh_task(
        self, include_hidden: bool, page_limit: int
    ) -> asyncio.Task[list[dict[str, Any]]]:
        existing = self._model_catalog_refresh_tasks.get(include_hidden)
        if existing is not None and not existing.done():
            return existing
        epoch = self._connection_epoch
        generation = self._model_catalog_generation
        task = asyncio.create_task(
            self._refresh_model_catalog(
                include_hidden, page_limit, epoch, generation
            ),
            name="codex-app-server-model-catalog",
        )
        self._model_catalog_refresh_tasks[include_hidden] = task
        self._model_catalog_refresh_origins[task] = (epoch, generation)

        def done(completed: asyncio.Task[list[dict[str, Any]]]) -> None:
            if self._model_catalog_refresh_tasks.get(include_hidden) is completed:
                self._model_catalog_refresh_tasks.pop(include_hidden, None)
            self._retired_model_catalog_refresh_tasks.discard(completed)
            # A stale-while-revalidate task has no foreground awaiter. Consume
            # its exception after recording cooldown, while leaving foreground
            # awaiters to receive the same task result normally.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    async def _refresh_model_catalog(
        self, include_hidden: bool, page_limit: int, epoch: int, generation: int
    ) -> list[dict[str, Any]]:
        """Fetch one full catalog and commit it only to its originating epoch."""
        try:
            models: list[dict[str, Any]] = []
            model_ids: set[str] = set()
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(_MAX_MODEL_PAGES):
                result = await self.list_models(
                    cursor=cursor, limit=page_limit, include_hidden=include_hidden
                )
                for model in result["data"]:
                    model_id = model["id"]
                    if model_id in model_ids:
                        raise AppServerProtocolError(
                            "model/list returned a duplicate model ID"
                        )
                    model_ids.add(model_id)
                    models.append(dict(model))
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise AppServerProtocolError("model/list nextCursor is invalid")
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise AppServerProtocolError("model/list cursor did not advance")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise AppServerProtocolError("model/list exceeded the page limit")
            if not models:
                raise AppServerProtocolError("model/list returned no available models")
            # Do not let a cancelled/old connection turn a last-good catalog
            # into authentication evidence for its successor.
            if not (
                self._connected
                and epoch == self._connection_epoch
                and generation == self._model_catalog_generation
            ):
                # An old connection/account is not allowed to satisfy a
                # foreground force refresh after it has been retired.
                raise _ModelCatalogRefreshInvalidated(
                    "model catalog refresh invalidated before completion"
                )
            self._model_catalogs[include_hidden] = tuple(copy.deepcopy(models))
            self._model_catalog_cached_at[include_hidden] = time.monotonic()
            self._model_catalog_versions[include_hidden] = (
                self._model_catalog_versions.get(include_hidden, 0) + 1
            )
            self._model_catalog_refresh_cooldown_until.pop(include_hidden, None)
            return copy.deepcopy(models)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed refresh never replaces last-good data.  The cooldown is
            # only for opportunistic stale refreshes; explicit/no-cache callers
            # still await one current transport attempt.
            if (
                epoch == self._connection_epoch
                and generation == self._model_catalog_generation
            ):
                self._model_catalog_refresh_cooldown_until[include_hidden] = (
                    time.monotonic()
                    + _MODEL_CATALOG_FAILURE_COOLDOWN_SECONDS
                )
            raise

    async def list_all_models(
        self,
        *,
        include_hidden: bool = False,
        page_limit: int = 100,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Read the complete paginated catalog with stale-while-revalidate."""
        if not isinstance(include_hidden, bool):
            raise AppServerProtocolError("includeHidden must be a boolean")
        if not isinstance(page_limit, int) or not 1 <= page_limit <= 1000:
            raise AppServerProtocolError("model page limit is invalid")
        cached = self._model_catalogs.get(include_hidden)
        now = time.monotonic()
        cached_at = self._model_catalog_cached_at.get(include_hidden)
        # Metadata-less manually seeded catalogs retain the previous compatible
        # behavior and are treated as fresh.
        fresh = cached is not None and (
            cached_at is None or now - cached_at < _MODEL_CATALOG_TTL_SECONDS
        )
        if cached is not None and not force_refresh and fresh:
            return copy.deepcopy(list(cached))
        if cached is not None and not force_refresh:
            if now >= self._model_catalog_refresh_cooldown_until.get(include_hidden, 0):
                self._model_catalog_refresh_task(include_hidden, page_limit)
            return copy.deepcopy(list(cached))
        account_retry_available = True
        while True:
            task = self._model_catalog_refresh_task(include_hidden, page_limit)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as error:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if account_retry_available and self._retryable_account_catalog_invalidation(task):
                    account_retry_available = False
                    continue
                invalidation = self._model_catalog_refresh_invalidations.get(task)
                source = invalidation[0] if invalidation is not None else "unknown"
                raise AppServerDisconnected(
                    f"model catalog refresh cancelled by {source}"
                ) from error
            except _ModelCatalogRefreshInvalidated as error:
                # A cancellation-resistant transport/test double can return
                # after account invalidation.  Its generation guard rejected
                # the old payload, so the same bounded fresh read is safe.
                if account_retry_available and self._retryable_account_catalog_invalidation(task):
                    account_retry_available = False
                    continue
                invalidation = self._model_catalog_refresh_invalidations.get(task)
                source = invalidation[0] if invalidation is not None else "unknown"
                raise AppServerDisconnected(
                    f"model catalog refresh invalidated by {source}"
                ) from error

    def _retryable_account_catalog_invalidation(
        self, task: asyncio.Task[list[dict[str, Any]]]
    ) -> bool:
        """Whether this exact retired refresh may receive one fresh retry."""
        invalidation = self._model_catalog_refresh_invalidations.get(task)
        origin = self._model_catalog_refresh_origins.get(task)
        return bool(
            invalidation is not None
            and invalidation[0] == "account update"
            and origin is not None
            and origin[0] == invalidation[1]
            and invalidation[1] == self._connection_epoch
            and invalidation[2] == self._model_catalog_nonaccount_lifecycle_serial
            and self._connected
            and self._initialized
        )

    @staticmethod
    def _resolve_model_from_catalog(
        models: list[dict[str, Any]], model: str | None, reasoning_effort: str | None
    ) -> dict[str, Any]:
        requested_model = model.strip() if isinstance(model, str) else ""
        if requested_model:
            matches = [
                item for item in models
                if requested_model in {item["id"], item["model"]}
            ]
        else:
            matches = [item for item in models if item["isDefault"]]
        if len(matches) != 1:
            label = requested_model or "default"
            raise AppServerProtocolError(
                f"App Server model selection is unavailable or ambiguous: {label}"
            )
        selected = matches[0]
        supported = [
            option["reasoningEffort"]
            for option in selected["supportedReasoningEfforts"]
        ]
        requested_effort = reasoning_effort.strip() if isinstance(reasoning_effort, str) else ""
        effort = requested_effort or selected["defaultReasoningEffort"]
        if effort not in supported:
            raise AppServerProtocolError(
                f"reasoning effort {effort!r} is not supported by {selected['id']}"
            )
        resolved = {
            "catalog_id": selected["id"],
            "model": selected["model"],
            "reasoning_effort": effort,
        }
        service_tiers = [
            dict(tier) for tier in selected.get("serviceTiers", [])
            if isinstance(tier, dict)
        ]
        if service_tiers:
            resolved["service_tiers"] = service_tiers
        return resolved

    async def resolve_model_selection(
        self,
        model: str | None,
        reasoning_effort: str | None,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Resolve from cache, forcing at most once for a stale selection."""
        if model is not None and not isinstance(model, str):
            raise AppServerProtocolError("model selection must be a string")
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            raise AppServerProtocolError("reasoning effort must be a string")
        before_version = self._model_catalog_versions.get(False, 0)
        models = await self.list_all_models(force_refresh=force_refresh)
        refreshed = self._model_catalog_versions.get(False, 0) != before_version
        try:
            return self._resolve_model_from_catalog(models, model, reasoning_effort)
        except AppServerProtocolError:
            # A no-cache request has already obtained a real catalog; avoid a
            # pointless second RPC.  Explicit force has likewise spent its one
            # refresh allowance.
            if force_refresh or refreshed:
                raise
        models = await self.list_all_models(force_refresh=True)
        return self._resolve_model_from_catalog(models, model, reasoning_effort)

    async def list_skills(
        self,
        *,
        cwds: list[str] | None = None,
        force_reload: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"forceReload": force_reload}
        if cwds is not None:
            if not isinstance(cwds, list) or not cwds:
                raise AppServerProtocolError("skills/list cwds must be non-empty")
            normalized = [self._normalize_cwd(cwd) for cwd in cwds]
            if len(set(normalized)) != len(normalized):
                raise AppServerProtocolError("skills/list cwds must be unique")
            params["cwds"] = normalized
        else:
            normalized = None
        if not isinstance(force_reload, bool):
            raise AppServerProtocolError("skills/list forceReload must be a boolean")
        result = await self.request("skills/list", params)
        self._validate_method_result(
            "skills/list", result, expected_cwds=normalized
        )
        return result

    async def skills_for_cwd(
        self, cwd: str, *, force_reload: bool = False
    ) -> dict[str, Any]:
        """Return one cwd-scoped catalog, refreshing independently of notifications."""
        normalized = self._normalize_cwd(cwd)
        dirty = normalized in self._dirty_skill_cwds
        if normalized not in self._skills_by_cwd or force_reload or dirty:
            result = await self.list_skills(
                cwds=[normalized], force_reload=bool(force_reload or dirty)
            )
            entry = copy.deepcopy(result["data"][0])
            self._skills_by_cwd[normalized] = entry
            self._dirty_skill_cwds.discard(normalized)
        return copy.deepcopy(self._skills_by_cwd[normalized])

    async def resolve_skill_inputs(
        self,
        cwd: str,
        selectors: list[str | dict[str, str]] | None,
        *,
        force_reload: bool = False,
    ) -> list[dict[str, str]]:
        """Resolve untrusted selections to enabled, server-discovered skill paths."""
        if selectors is None:
            return []
        if not isinstance(selectors, list):
            raise AppServerProtocolError("skill selections must be a list")
        catalog = await self.skills_for_cwd(cwd, force_reload=force_reload)
        resolved: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for selector in selectors:
            if isinstance(selector, str):
                name = selector
                path = None
            elif isinstance(selector, dict) and set(selector) == {"name", "path"}:
                name = selector.get("name")
                path = selector.get("path")
            else:
                raise AppServerProtocolError("skill selector is malformed")
            if not isinstance(name, str) or not name:
                raise AppServerProtocolError("skill name is invalid")
            matches = [
                skill for skill in catalog["skills"]
                if skill["enabled"]
                and skill["name"] == name
                and (path is None or skill["path"] == path)
            ]
            if len(matches) != 1:
                raise AppServerProtocolError(
                    f"skill selection is unavailable or ambiguous: {name}"
                )
            key = (matches[0]["name"], matches[0]["path"])
            if key in seen:
                raise AppServerProtocolError("duplicate skill selection")
            seen.add(key)
            resolved.append({"type": "skill", "name": key[0], "path": key[1]})
        return resolved

    def invalidate_skills(self) -> None:
        """Mark cached skills dirty; the next read still performs skills/list."""
        self._dirty_skill_cwds.update(self._skills_by_cwd)

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str,
        sandbox: str = "danger-full-access",
        dynamic_tools: list[dict[str, Any]] | None = None,
        review_mode: bool = False,
        review_instructions: str | None = None,
        review_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Start one thread with an explicit, minimal dynamic-tool surface."""
        normalized_cwd = self._normalize_cwd(cwd)
        if not isinstance(model, str) or not model:
            raise AppServerProtocolError("thread model must be non-empty")
        self._cached_model(model)
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise AppServerProtocolError("thread sandbox is invalid")
        if dynamic_tools is None:
            dynamic_tools = []
        if not isinstance(dynamic_tools, list) or not all(
            isinstance(tool, dict) for tool in dynamic_tools
        ):
            raise AppServerProtocolError("thread dynamic tools are invalid")
        if not isinstance(review_mode, bool):
            raise AppServerProtocolError("thread review mode is invalid")
        if review_instructions is not None and (
            not isinstance(review_instructions, str)
            or not review_instructions.strip()
        ):
            raise AppServerProtocolError("thread review instructions are invalid")
        if review_instructions is not None and not review_mode:
            raise AppServerProtocolError("review instructions require review mode")
        params: dict[str, Any] = {
            "cwd": normalized_cwd,
            "model": model,
            "approvalPolicy": "never" if review_mode or sandbox == "danger-full-access" else "on-request",
            "sandbox": sandbox,
            "dynamicTools": dynamic_tools,
            # The App Server only emits raw Responses items for threads that
            # opt in at creation. _handle_raw immediately projects the two
            # new_context signals below and discards every raw payload.
            "experimentalRawEvents": True,
        }
        config: dict[str, Any] = {}
        if review_mode:
            # Hooks execute automatically on the server and can write/send
            # independently of the reviewer's read-only tool instructions.
            config["features.hooks"] = False
        if review_reasoning_effort is not None:
            if not review_mode or review_reasoning_effort not in {
                option["reasoningEffort"]
                for option in self._cached_model(model)["supportedReasoningEfforts"]
            }:
                raise AppServerProtocolError("review reasoning effort is unavailable")
            # Native review/start consults the thread configuration, not the
            # ordinary turn/start options. Do not override external tools.
            config["review_model"] = model
            config["model_reasoning_effort"] = review_reasoning_effort
        if self._fixture.get("requestUserInputDefaultModeConfig"):
            # Thread-scoped native feature gate: no global config mutation,
            # prompt injection, instruction injection, or tool definition.
            config["features.default_mode_request_user_input"] = True
        if config:
            params["config"] = config
        if review_instructions is not None:
            params["developerInstructions"] = review_instructions
        result = await self.request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise AppServerProtocolError("thread/start result missing thread")
        self._validate_rpc_id(thread_id, "thread")
        self._thread_models[thread_id] = model
        self._thread_cwds[thread_id] = normalized_cwd
        return thread

    async def resume_thread(self, thread_id: str) -> dict[str, Any]:
        """Load one persisted thread on this connection without starting work."""
        self._validate_rpc_id(thread_id, "thread")
        params: dict[str, Any] = {"threadId": thread_id}
        if self._fixture.get("metadataResume"):
            # Paginated history must not be embedded in this snapshot.  It is
            # hydrated explicitly by read_thread, below, so a long thread
            # cannot make every resume exceed the transport message bound.
            params["excludeTurns"] = True
        if self._fixture.get("requestUserInputDefaultModeConfig"):
            params["config"] = {"features.default_mode_request_user_input": True}
        result = await self.request("thread/resume", params)
        validated = self._require_thread_result(result, thread_id, "thread/resume")
        cwd = validated["thread"].get("cwd")
        if isinstance(cwd, str) and Path(cwd).is_absolute():
            self._thread_cwds[thread_id] = os.path.normpath(cwd)
        return validated

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        """Read durable thread state and page full turns when requested.

        Current App Server releases deprecate full-history hydration through
        ``thread/read(includeTurns=true)`` for paginated threads. Fetch the
        metadata-only thread first, then hydrate authoritative full turns via
        ``thread/turns/list`` in ascending order.
        """
        self._validate_rpc_id(thread_id, "thread")
        validated = await self._read_thread_metadata(thread_id)
        if not include_turns:
            return validated
        if self._fixture.get("itemPagination"):
            return await self._read_paged_thread(thread_id)
        turns = await self.list_all_thread_turns(thread_id)
        hydrated = dict(validated)
        hydrated_thread = dict(validated["thread"])
        hydrated_thread["turns"] = turns
        hydrated["thread"] = hydrated_thread
        return hydrated

    async def read_thread_item(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        *,
        predecessor_id: str | None = None,
    ) -> dict[str, Any]:
        """Read exactly one authoritative persisted item without replaying work."""
        self._validate_rpc_id(thread_id, "thread")
        self._validate_rpc_id(turn_id, "turn")
        self._validate_rpc_id(item_id, "item")
        if predecessor_id is not None:
            self._validate_rpc_id(predecessor_id, "item")
        if not self._fixture.get("itemPagination"):
            snapshot = await self.read_thread(thread_id, include_turns=True)
            turns = snapshot["thread"].get("turns")
            if not isinstance(turns, list):
                raise AppServerProtocolError("thread/read did not return turns")
            matches = [
                item
                for turn in turns
                if isinstance(turn, dict) and turn.get("id") == turn_id
                for item in (turn.get("items") or [])
                if isinstance(item, dict) and item.get("id") == item_id
            ]
            if len(matches) != 1:
                raise AppServerProtocolError("thread/read did not return requested item")
            return matches[0]
        schema = self._fixture["methods"]["thread/items/list"]
        params: dict[str, Any] = {
            "threadId": thread_id,
            "turnId": turn_id,
            "limit": 1,
            "sortDirection": "asc",
        }
        if predecessor_id is not None:
            params["cursor"] = {"type": "item", "itemId": predecessor_id}
        result = await self.request("thread/items/list", params)
        self._require_fields(
            result, schema["responseRequired"], "thread/items/list result"
        )
        data = result["data"]
        if not isinstance(data, list) or len(data) != 1:
            raise AppServerProtocolError(
                "thread/items/list did not return exactly one requested item"
            )
        entry = data[0]
        self._require_fields(
            entry, schema["entryRequired"], "thread/items/list entry"
        )
        if entry.get("turnId") != turn_id:
            raise AppServerProtocolError(
                "thread/items/list returned an item for another turn"
            )
        item = entry["item"]
        if not isinstance(item, dict) or item.get("id") != item_id:
            raise AppServerProtocolError(
                "thread/items/list did not return requested item"
            )
        if not isinstance(item.get("type"), str) or not item["type"]:
            raise AppServerProtocolError("thread/items/list item type is invalid")
        return item

    async def _read_paged_thread(self, thread_id: str) -> dict[str, Any]:
        """Return a stable 0.159.2 snapshot, or fail rather than omit a turn.

        Live notifications are intentionally not serialized with this read
        path.  Therefore compare the metadata again after item hydration: a
        new or completed turn must never be returned as an apparently complete
        but stale history.  One retry accommodates a single concurrent update;
        continued churn fails closed instead of looping indefinitely.
        """
        metadata = await self._list_thread_turn_metadata(thread_id, page_limit=100)
        hydrated = await self._hydrate_turn_metadata(thread_id, metadata)
        latest = await self._read_thread_metadata(thread_id)
        latest_metadata = await self._list_thread_turn_metadata(
            thread_id, page_limit=100
        )
        if not self._same_turn_snapshot(metadata, latest_metadata):
            hydrated = await self._hydrate_turn_metadata(thread_id, latest_metadata)
            latest = await self._read_thread_metadata(thread_id)
            verified_metadata = await self._list_thread_turn_metadata(
                thread_id, page_limit=100
            )
            if not self._same_turn_snapshot(latest_metadata, verified_metadata):
                raise AppServerProtocolError(
                    "thread changed while paginating history"
                )
            latest_metadata = verified_metadata
        return self._merge_thread_snapshot(latest, latest_metadata, hydrated)

    async def _read_thread_metadata(self, thread_id: str) -> dict[str, Any]:
        result = await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        return self._require_thread_result(result, thread_id, "thread/read")

    @staticmethod
    def _same_turn_snapshot(
        before: list[dict[str, Any]], after: list[dict[str, Any]]
    ) -> bool:
        """Compare the fields that determine complete ordered history."""
        return [
            (turn["id"], turn["status"]) for turn in before
        ] == [
            (turn["id"], turn["status"]) for turn in after
        ]

    @staticmethod
    def _merge_thread_snapshot(
        metadata: dict[str, Any],
        latest_turns: list[dict[str, Any]],
        hydrated_turns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply the newest metadata/status without losing authoritative items."""
        items_by_turn = {
            turn["id"]: turn["items"] for turn in hydrated_turns
        }
        merged_turns: list[dict[str, Any]] = []
        for turn in latest_turns:
            merged = dict(turn)
            merged["items"] = items_by_turn[turn["id"]]
            merged["itemsView"] = "full"
            merged_turns.append(merged)
        merged_result = dict(metadata)
        merged_thread = dict(metadata["thread"])
        merged_thread["turns"] = merged_turns
        merged_result["thread"] = merged_thread
        return merged_result

    async def list_all_thread_turns(
        self,
        thread_id: str,
        *,
        page_limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Hydrate every persisted turn and item through bounded pagination."""
        self._validate_rpc_id(thread_id, "thread")
        if not isinstance(page_limit, int) or not 1 <= page_limit <= 1000:
            raise AppServerProtocolError("turn page limit is invalid")
        if self._fixture.get("itemPagination"):
            metadata = await self._list_thread_turn_metadata(
                thread_id, page_limit=page_limit
            )
            return await self._hydrate_turn_metadata(thread_id, metadata)
        return await self._list_all_thread_turns_legacy(
            thread_id, page_limit=page_limit
        )

    async def _list_all_thread_turns_legacy(
        self,
        thread_id: str,
        *,
        page_limit: int,
    ) -> list[dict[str, Any]]:
        """Hydrate full turn pages for pre-0.159.2 protocol fixtures."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_turns: set[str] = set()
        turns: list[dict[str, Any]] = []
        schema = self._fixture["methods"]["thread/turns/list"]
        for _ in range(_MAX_TURN_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": page_limit,
                "sortDirection": "asc",
                "itemsView": "full",
            }
            if cursor is not None:
                params["cursor"] = cursor
            try:
                result = await self.request("thread/turns/list", params)
            except AppServerRPCError as error:
                if (
                    error.code == -32600
                    and "not materialized yet" in error.message
                    and "thread/turns/list" in error.message
                ):
                    return []
                raise
            self._require_fields(
                result, schema["responseRequired"], "thread/turns/list result"
            )
            data = result["data"]
            if not isinstance(data, list):
                raise AppServerProtocolError(
                    "thread/turns/list data must be a list"
                )
            for turn in data:
                self._require_fields(
                    turn, schema["turnRequired"], "thread/turns/list turn"
                )
                turn_id = turn["id"]
                self._validate_rpc_id(turn_id, "turn")
                if turn_id in seen_turns:
                    raise AppServerProtocolError(
                        "thread/turns/list repeated a turn"
                    )
                if not isinstance(turn["status"], str) or not turn["status"]:
                    raise AppServerProtocolError(
                        "thread/turns/list turn status is invalid"
                    )
                if not isinstance(turn["items"], list):
                    raise AppServerProtocolError(
                        "thread/turns/list turn items must be a list"
                    )
                if turn.get("itemsView") != "full":
                    raise AppServerProtocolError(
                        "thread/turns/list did not return full items"
                    )
                seen_turns.add(turn_id)
                turns.append(turn)
            cursor = self._next_unique_cursor(
                result.get(schema["paginationField"]),
                seen_cursors,
                "thread/turns/list",
            )
            if cursor is None:
                return turns
        raise AppServerProtocolError("thread/turns/list pagination exceeded limit")

    async def _list_thread_turn_metadata(
        self,
        thread_id: str,
        *,
        page_limit: int,
    ) -> list[dict[str, Any]]:
        """Return validated 0.159.2 turn metadata with unloaded items."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_turns: set[str] = set()
        turns: list[dict[str, Any]] = []
        schema = self._fixture["methods"]["thread/turns/list"]
        for _ in range(_MAX_TURN_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": page_limit,
                "sortDirection": "asc",
                "itemsView": "notLoaded",
            }
            if cursor is not None:
                params["cursor"] = cursor
            try:
                result = await self.request("thread/turns/list", params)
            except AppServerRPCError as error:
                if (
                    error.code == -32600
                    and "not materialized yet" in error.message
                    and "thread/turns/list" in error.message
                ):
                    return []
                raise
            self._require_fields(
                result, schema["responseRequired"], "thread/turns/list result"
            )
            data = result["data"]
            if not isinstance(data, list):
                raise AppServerProtocolError(
                    "thread/turns/list data must be a list"
                )
            for turn in data:
                self._require_fields(
                    turn, schema["turnRequired"], "thread/turns/list turn"
                )
                turn_id = turn["id"]
                self._validate_rpc_id(turn_id, "turn")
                if turn_id in seen_turns:
                    raise AppServerProtocolError(
                        "thread/turns/list repeated a turn"
                    )
                if not isinstance(turn["status"], str) or not turn["status"]:
                    raise AppServerProtocolError(
                        "thread/turns/list turn status is invalid"
                    )
                if not isinstance(turn["items"], list):
                    raise AppServerProtocolError(
                        "thread/turns/list turn items must be a list"
                    )
                if turn.get("itemsView") != "notLoaded" or turn["items"]:
                    raise AppServerProtocolError(
                        "thread/turns/list did not return unloaded items"
                    )
                seen_turns.add(turn_id)
                turns.append(dict(turn))
            next_cursor = result.get(schema["paginationField"])
            cursor = self._next_unique_cursor(
                next_cursor, seen_cursors, "thread/turns/list"
            )
            if cursor is None:
                return turns
        raise AppServerProtocolError("thread/turns/list pagination exceeded limit")

    async def _hydrate_turn_metadata(
        self, thread_id: str, metadata: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fill already validated metadata with all its bounded item pages."""
        hydrated_turns: list[dict[str, Any]] = []
        for turn in metadata:
            hydrated = dict(turn)
            hydrated["items"] = await self._list_all_turn_items(
                thread_id, turn["id"]
            )
            hydrated["itemsView"] = "full"
            hydrated_turns.append(hydrated)
        return hydrated_turns

    async def _list_all_turn_items(
        self, thread_id: str, turn_id: str
    ) -> list[dict[str, Any]]:
        """Return one turn's authoritative items without accepting cross-turn data."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        # Item IDs are only required to be unique within their containing
        # turn.  The response's turnId is separately verified below.
        seen_items: set[str] = set()
        items: list[dict[str, Any]] = []
        schema = self._fixture["methods"]["thread/items/list"]
        for _ in range(_MAX_ITEM_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "turnId": turn_id,
                "limit": _THREAD_ITEM_PAGE_LIMIT,
                "sortDirection": "asc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.request("thread/items/list", params)
            self._require_fields(
                result, schema["responseRequired"], "thread/items/list result"
            )
            data = result["data"]
            if not isinstance(data, list):
                raise AppServerProtocolError("thread/items/list data must be a list")
            for entry in data:
                self._require_fields(
                    entry, schema["entryRequired"], "thread/items/list entry"
                )
                entry_turn_id = entry["turnId"]
                self._validate_rpc_id(entry_turn_id, "turn")
                if entry_turn_id != turn_id:
                    raise AppServerProtocolError(
                        "thread/items/list returned an item for another turn"
                    )
                item = entry["item"]
                if not isinstance(item, dict):
                    raise AppServerProtocolError(
                        "thread/items/list entry item must be an object"
                    )
                item_id = item.get("id")
                self._validate_rpc_id(item_id, "item")
                if not isinstance(item.get("type"), str) or not item["type"]:
                    raise AppServerProtocolError(
                        "thread/items/list entry item type is invalid"
                    )
                if item_id in seen_items:
                    raise AppServerProtocolError(
                        "thread/items/list repeated an item"
                    )
                seen_items.add(item_id)
                items.append(item)
            cursor = self._next_unique_cursor(
                result.get(schema["paginationField"]),
                seen_cursors,
                "thread/items/list",
            )
            if cursor is None:
                return items
        raise AppServerProtocolError("thread/items/list pagination exceeded limit")

    @staticmethod
    def _next_unique_cursor(
        next_cursor: Any, seen_cursors: set[str], method: str
    ) -> str | None:
        """Validate a nullable opaque pagination cursor before following it."""
        if next_cursor is None:
            return None
        if not isinstance(next_cursor, str) or not next_cursor:
            raise AppServerProtocolError(
                f"{method} nextCursor must be a string or null"
            )
        if next_cursor in seen_cursors:
            raise AppServerProtocolError(f"{method} repeated a cursor")
        seen_cursors.add(next_cursor)
        return next_cursor

    async def thread_exists(
        self,
        thread_id: str,
        *,
        cwd: str,
        page_limit: int = 100,
    ) -> bool:
        """Confirm an exact native Thread ID through the authoritative list RPC."""
        self._validate_rpc_id(thread_id, "thread")
        normalized_cwd = self._normalize_cwd(cwd)
        if not isinstance(page_limit, int) or not 1 <= page_limit <= 1000:
            raise AppServerProtocolError("thread page limit is invalid")
        for archived in (False, True):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(_MAX_THREAD_PAGES):
                params: dict[str, Any] = {
                    "archived": archived,
                    "cwd": normalized_cwd,
                    "limit": page_limit,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                result = await self.request("thread/list", params)
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, list):
                    raise AppServerProtocolError(
                        "thread/list result must contain data"
                    )
                for thread in data:
                    listed_id = thread.get("id") if isinstance(thread, dict) else None
                    self._validate_rpc_id(listed_id, "listed thread")
                    if listed_id == thread_id:
                        return True
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise AppServerProtocolError(
                        "thread/list nextCursor is invalid"
                    )
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise AppServerProtocolError(
                        "thread/list cursor did not advance"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise AppServerProtocolError(
                    "thread/list exceeded the page limit"
                )
        return False

    @property
    def supports_conversation_tree_stop(self) -> bool:
        """Whether this locked protocol fixture supports safe tree cleanup."""
        methods = self._fixture.get("methods")
        required = {
            "thread/list",
            "thread/turns/list",
            "thread/backgroundTerminals/list",
            "thread/backgroundTerminals/clean",
        }
        return (
            self._fixture.get("conversationTreeStop") is True
            and isinstance(methods, dict)
            and required.issubset(methods)
        )

    def _require_conversation_tree_stop_support(self) -> None:
        if not self.supports_conversation_tree_stop:
            raise AppServerProtocolError(
                "conversation tree stop is unsupported by this App Server"
            )

    async def list_loaded_thread_ids(self) -> list[str]:
        """Read all memory-loaded native sessions, including unprojected agents.

        This RPC does not resume, start, subscribe to or mutate any thread.
        Unavailable locked protocol or malformed pagination is not an empty list.
        """
        schema = self._fixture.get('methods', {}).get('thread/loaded/list')
        if schema is None:
            raise AppServerProtocolError('loaded thread inventory is unsupported by the locked protocol')
        cursor = None
        seen_cursors: set[str] = set()
        ids: set[str] = set()
        for _ in range(_MAX_THREAD_PAGES):
            params: dict[str, Any] = {'limit': _THREAD_LIST_PAGE_LIMIT}
            if cursor is not None:
                params['cursor'] = cursor
            result = await self.request('thread/loaded/list', params)
            self._require_fields(result, schema['responseRequired'], 'thread/loaded/list result')
            if not isinstance(result['data'], list):
                raise AppServerProtocolError('loaded thread inventory must contain an id list')
            for thread_id in result['data']:
                self._validate_rpc_id(thread_id, 'loaded thread')
                if thread_id in ids:
                    raise AppServerProtocolError('loaded thread inventory repeated a thread')
                ids.add(thread_id)
            cursor = self._next_unique_cursor(result.get(schema['paginationField']), seen_cursors,
                                              'thread/loaded/list')
            if cursor is None:
                return sorted(ids)
        raise AppServerProtocolError('loaded thread inventory exceeded page limit')

    async def list_descendant_threads(
        self, root_thread_id: str
    ) -> list[dict[str, Any]]:
        """Read every native descendant of one root without inferring ownership.

        Native ``ancestorThreadId`` is authoritative, but validate the returned
        parent chain as well so a malformed response cannot widen a later stop
        operation to another tree.
        """
        self._require_conversation_tree_stop_support()
        self._validate_rpc_id(root_thread_id, "root thread")
        schema = self._fixture["methods"]["thread/list"]
        descendants: dict[str, dict[str, Any]] = {}
        # Archiving a parent must not hide a still-live grandchild or sever
        # the ancestry proof. Native filters these populations separately.
        for archived in (False, True):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(_MAX_THREAD_PAGES):
                params: dict[str, Any] = {
                    "ancestorThreadId": root_thread_id,
                    "archived": archived,
                    "useStateDbOnly": True,
                    "limit": _THREAD_LIST_PAGE_LIMIT,
                    "sourceKinds": list(_THREAD_SOURCE_KINDS),
                }
                if cursor is not None:
                    params["cursor"] = cursor
                result = await self.request("thread/list", params)
                self._require_fields(
                    result, schema["responseRequired"], "thread/list result"
                )
                data = result["data"]
                if not isinstance(data, list):
                    raise AppServerProtocolError("thread/list data must be a list")
                for thread in data:
                    self._require_fields(
                        thread, schema["entryRequired"], "thread/list descendant"
                    )
                    thread_id = thread["id"]
                    parent_thread_id = thread["parentThreadId"]
                    self._validate_rpc_id(thread_id, "listed thread")
                    if thread_id == root_thread_id:
                        raise AppServerProtocolError(
                            "thread/list returned the ancestor itself"
                        )
                    self._validate_rpc_id(parent_thread_id, "listed thread parent")
                    if thread_id in descendants:
                        raise AppServerProtocolError("thread/list repeated a thread")
                    descendants[thread_id] = dict(thread)
                cursor = self._next_unique_cursor(
                    result.get(schema["paginationField"]),
                    seen_cursors,
                    "thread/list",
                )
                if cursor is None:
                    break
            else:
                raise AppServerProtocolError("thread/list pagination exceeded limit")
        self._validate_descendant_parent_chains(root_thread_id, descendants)
        return list(descendants.values())

    @staticmethod
    def _validate_descendant_parent_chains(
        root_thread_id: str, descendants: dict[str, dict[str, Any]]
    ) -> None:
        """Require every returned descendant's known parent chain to reach root."""
        for descendant_id in descendants:
            current_id = descendant_id
            visited: set[str] = set()
            while current_id != root_thread_id:
                if current_id in visited:
                    raise AppServerProtocolError(
                        "thread/list descendant parent chain contains a cycle"
                    )
                visited.add(current_id)
                current = descendants.get(current_id)
                if current is None:
                    raise AppServerProtocolError(
                        "thread/list descendant parent chain leaves the ancestor tree"
                    )
                parent_thread_id = current.get("parentThreadId")
                if not isinstance(parent_thread_id, str) or not parent_thread_id:
                    raise AppServerProtocolError(
                        "thread/list descendant is missing parentThreadId"
                    )
                current_id = parent_thread_id

    async def read_thread_turn_metadata(
        self, thread_id: str
    ) -> list[dict[str, Any]]:
        """Read turn metadata only; never hydrate user content or reasoning items."""
        self._require_conversation_tree_stop_support()
        self._validate_rpc_id(thread_id, "thread")
        return await self._list_thread_turn_metadata(
            thread_id, page_limit=_THREAD_LIST_PAGE_LIMIT
        )

    async def list_background_terminals(
        self, thread_id: str
    ) -> list[dict[str, Any]]:
        """Read native background terminals for one thread through bounded pages."""
        self._require_conversation_tree_stop_support()
        self._validate_rpc_id(thread_id, "thread")
        schema = self._fixture["methods"]["thread/backgroundTerminals/list"]
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_process_ids: set[str] = set()
        terminals: list[dict[str, Any]] = []
        for _ in range(_MAX_THREAD_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": _THREAD_LIST_PAGE_LIMIT,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.request("thread/backgroundTerminals/list", params)
            self._require_fields(
                result,
                schema["responseRequired"],
                "thread/backgroundTerminals/list result",
            )
            data = result["data"]
            if not isinstance(data, list):
                raise AppServerProtocolError(
                    "thread/backgroundTerminals/list data must be a list"
                )
            for terminal in data:
                self._require_fields(
                    terminal,
                    schema["entryRequired"],
                    "thread/backgroundTerminals/list terminal",
                )
                process_id = terminal["processId"]
                if not isinstance(process_id, str) or not process_id:
                    raise AppServerProtocolError(
                        "thread/backgroundTerminals/list processId is invalid"
                    )
                if process_id in seen_process_ids:
                    raise AppServerProtocolError(
                        "thread/backgroundTerminals/list repeated a processId"
                    )
                seen_process_ids.add(process_id)
                terminals.append(dict(terminal))
            cursor = self._next_unique_cursor(
                result.get(schema["paginationField"]),
                seen_cursors,
                "thread/backgroundTerminals/list",
            )
            if cursor is None:
                return terminals
        raise AppServerProtocolError(
            "thread/backgroundTerminals/list pagination exceeded limit"
        )

    async def clean_background_terminals(self, thread_id: str) -> None:
        """Ask native App Server to clean only this thread's terminals."""
        self._require_conversation_tree_stop_support()
        self._validate_rpc_id(thread_id, "thread")
        result = await self.request(
            "thread/backgroundTerminals/clean", {"threadId": thread_id}
        )
        if result != {}:
            raise AppServerProtocolError(
                "thread/backgroundTerminals/clean result must be an empty object"
            )

    @property
    def supports_thread_name_set(self) -> bool:
        methods = self._fixture.get("methods")
        return isinstance(methods, dict) and "thread/name/set" in methods

    async def set_thread_name(self, thread_id: str, name: str) -> None:
        """Set a native Thread name only when this exact fixture supports it."""
        if not self.supports_thread_name_set:
            raise AppServerProtocolError("thread/name/set is unsupported by this App Server")
        self._validate_rpc_id(thread_id, "thread")
        if not isinstance(name, str) or not name.strip():
            raise AppServerProtocolError("thread name must be non-empty")
        result = await self.request("thread/name/set", {"threadId": thread_id, "name": name})
        if result != {}:
            raise AppServerProtocolError(
                "thread/name/set result must be an empty object"
            )

    async def update_thread_settings(
        self, thread_id: str, service_tier: str | None
    ) -> None:
        """Use the experimental Thread settings RPC for later turns only."""
        self._validate_rpc_id(thread_id, "thread")
        if service_tier is not None and (
            not isinstance(service_tier, str) or not service_tier.strip()
        ):
            raise AppServerProtocolError("thread service tier is invalid")
        result = await self.request(
            "thread/settings/update",
            {"threadId": thread_id, "serviceTier": service_tier},
        )
        if result != {}:
            raise AppServerProtocolError(
                "thread/settings/update result must be an empty object"
            )

    async def get_thread_goal(self, thread_id: str) -> dict[str, Any] | None:
        from engine.conversation_goals import validate_goal
        self._validate_rpc_id(thread_id, "thread")
        result = await self.request("thread/goal/get", {"threadId": thread_id})
        if not isinstance(result, dict) or "goal" not in result:
            raise AppServerProtocolError("thread/goal/get result is invalid")
        return validate_goal(result["goal"], thread_id)

    async def set_thread_goal(self, thread_id: str, **updates: Any) -> dict[str, Any]:
        from engine.conversation_goals import validate_goal, validate_intent
        self._validate_rpc_id(thread_id, "thread")
        if not updates or not set(updates).issubset({"objective", "status", "tokenBudget"}):
            raise AppServerProtocolError("native goal update is invalid")
        if "objective" in updates:
            validate_intent(updates["objective"], updates.get("status", "active"))
        if "status" in updates and updates["status"] not in {"active", "paused", "complete"}:
            raise AppServerProtocolError("native goal status is invalid")
        if "tokenBudget" in updates:
            value = updates["tokenBudget"]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AppServerProtocolError("native goal budget is invalid")
        result = await self.request("thread/goal/set", {"threadId": thread_id, **updates})
        if not isinstance(result, dict) or result.get("goal") is None:
            raise AppServerProtocolError("thread/goal/set result is invalid")
        return validate_goal(result["goal"], thread_id)

    async def clear_thread_goal(self, thread_id: str) -> bool:
        self._validate_rpc_id(thread_id, "thread")
        result = await self.request("thread/goal/clear", {"threadId": thread_id})
        if not isinstance(result, dict) or not isinstance(result.get("cleared"), bool):
            raise AppServerProtocolError("thread/goal/clear result is invalid")
        return result["cleared"]

    async def update_goal_thread_settings(self, thread_id: str, *, cwd: str,
            model: str, effort: str, permission: str, collaboration_mode: str,
            service_tier: str | None) -> None:
        # Goal continuations are native-owned; configure their next-turn
        # settings before activation rather than manufacturing turn/start.
        self._validate_rpc_id(thread_id, "thread")
        policies = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                    "danger-full-access": "dangerFullAccess"}
        if permission not in policies or collaboration_mode not in {"default", "plan"}:
            raise AppServerProtocolError("goal thread settings are invalid")
        params = {"threadId": thread_id, "cwd": self._normalize_cwd(cwd),
            "model": model, "effort": effort, "serviceTier": service_tier,
            "approvalPolicy": "never" if permission == "danger-full-access" else "on-request",
            "sandboxPolicy": {"type": policies[permission]},
            "collaborationMode": {"mode": collaboration_mode, "settings": {
                "model": model, "reasoning_effort": effort, "developer_instructions": None}}}
        result = await self.request("thread/settings/update", params)
        if result != {}:
            raise AppServerProtocolError("thread/settings/update result must be an empty object")

    async def delete_thread(self, thread_id: str) -> None:
        """Delete one native Thread after the application has proved it idle."""
        self._validate_rpc_id(thread_id, "thread")
        result = await self.request("thread/delete", {"threadId": thread_id})
        if result != {}:
            raise AppServerProtocolError("thread/delete result must be an empty object")
        self._thread_models.pop(thread_id, None)
        self._thread_cwds.pop(thread_id, None)

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        client_user_message_id: str | None = None,
        skill_inputs: list[dict[str, str]] | None = None,
        leading_context: str | None = None,
        leading_context_after_text: bool = False,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start exactly one new turn with validated structured input.

        ``options`` is intentionally explicit and only used by the future
        router when a new turn is known safe.  Recovery code never calls this
        method to recreate a previously submitted request.
        """
        self._validate_rpc_id(thread_id, "thread")
        params = self._turn_params(
            thread_id, text, client_user_message_id, skill_inputs, leading_context,
            leading_context_after_text,
        )
        params["threadId"] = thread_id
        if options is not None:
            forbidden = {"threadId", "input", "clientUserMessageId"}
            allowed = {
                "model", "effort", "cwd", "sandbox", "serviceTier",
                "collaborationMode", "approvalPolicy",
            }
            if (
                not isinstance(options, dict)
                or forbidden.intersection(options)
                or not set(options).issubset(allowed)
            ):
                raise AppServerProtocolError("invalid turn/start options")
            selected_model = options.get("model") or self._thread_models.get(thread_id)
            effort = options.get("effort")
            if "model" in options or "effort" in options:
                if not isinstance(selected_model, str):
                    raise AppServerProtocolError(
                        "turn model is unavailable for effort validation"
                    )
                catalog_model = self._cached_model(selected_model)
                if "model" in options:
                    if not isinstance(options["model"], str) or not options["model"]:
                        raise AppServerProtocolError("turn model is invalid")
                    options = dict(options)
                    options["model"] = catalog_model["model"]
                if "effort" in options:
                    supported = {
                        item["reasoningEffort"]
                        for item in catalog_model["supportedReasoningEfforts"]
                    }
                    if not isinstance(effort, str) or effort not in supported:
                        raise AppServerProtocolError(
                            "turn reasoning effort is not supported by the model"
                        )
            if "cwd" in options:
                options = dict(options)
                options["cwd"] = self._normalize_cwd(options["cwd"])
            if "sandbox" in options:
                sandbox = options.get("sandbox")
                policies = {
                    "read-only": {"type": "readOnly"},
                    "workspace-write": {"type": "workspaceWrite"},
                    "danger-full-access": {"type": "dangerFullAccess"},
                }
                if sandbox not in policies:
                    raise AppServerProtocolError("turn sandbox is invalid")
                options = dict(options)
                options.pop("sandbox")
                options["sandboxPolicy"] = policies[sandbox]
                options.setdefault("approvalPolicy", "never" if sandbox == "danger-full-access" else "on-request")
            if "approvalPolicy" in options and options["approvalPolicy"] not in {"never", "on-request"}:
                raise AppServerProtocolError("turn approval policy is invalid")
            if "serviceTier" in options:
                service_tier = options.get("serviceTier")
                if service_tier is not None and (
                    not isinstance(service_tier, str) or not service_tier
                ):
                    raise AppServerProtocolError("turn service tier is invalid")
                if service_tier is not None:
                    if not isinstance(selected_model, str):
                        raise AppServerProtocolError(
                            "turn model is unavailable for service tier validation"
                        )
                    catalog_model = self._cached_model(selected_model)
                    supported_tiers = {
                        tier.get("id") for tier in catalog_model.get("serviceTiers", [])
                        if isinstance(tier, dict) and isinstance(tier.get("id"), str)
                    }
                    if service_tier not in supported_tiers:
                        raise AppServerProtocolError(
                            "turn service tier is not supported by the model"
                        )
            if "collaborationMode" in options:
                collaboration = options.get("collaborationMode")
                settings = (
                    collaboration.get("settings")
                    if isinstance(collaboration, dict) else None
                )
                if (
                    not isinstance(collaboration, dict)
                    or set(collaboration) != {"mode", "settings"}
                    or collaboration.get("mode") not in {"default", "plan"}
                    or not isinstance(settings, dict)
                    or set(settings) != {
                        "model", "reasoning_effort", "developer_instructions"
                    }
                    or settings.get("developer_instructions") is not None
                    or not isinstance(settings.get("model"), str)
                    or not settings.get("model")
                    or not isinstance(settings.get("reasoning_effort"), str)
                    or not settings.get("reasoning_effort")
                ):
                    raise AppServerProtocolError(
                        "turn collaboration mode is invalid"
                    )
                collaboration_model = self._cached_model(settings["model"])
                supported = {
                    item["reasoningEffort"]
                    for item in collaboration_model["supportedReasoningEfforts"]
                }
                if settings["reasoning_effort"] not in supported:
                    raise AppServerProtocolError(
                        "turn collaboration mode effort is unsupported"
                    )
                options = dict(options)
                options["collaborationMode"] = {
                    "mode": collaboration["mode"],
                    "settings": {
                        "model": collaboration_model["model"],
                        "reasoning_effort": settings["reasoning_effort"],
                        "developer_instructions": None,
                    },
                }
            params.update(options)
        result = await self.request("turn/start", params)
        return self._require_turn_result(result, "turn/start")

    async def steer_turn(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        *,
        client_user_message_id: str | None = None,
        skill_inputs: list[dict[str, str]] | None = None,
        leading_context: str | None = None,
    ) -> str:
        """Append text to one known active turn, never with turn overrides."""
        self._validate_rpc_id(thread_id, "thread")
        self._validate_rpc_id(expected_turn_id, "turn")
        params = self._turn_params(
            thread_id, text, client_user_message_id, skill_inputs, leading_context
        )
        params.update({"threadId": thread_id, "expectedTurnId": expected_turn_id})
        result = await self.request("turn/steer", params)
        if not isinstance(result, dict) or not isinstance(result.get("turnId"), str):
            raise AppServerProtocolError("turn/steer result missing turnId")
        return result["turnId"]

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        """Request cancellation of one turn; the terminal event remains authoritative."""
        self._validate_rpc_id(thread_id, "thread")
        self._validate_rpc_id(turn_id, "turn")
        result = await self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        if result != {}:
            raise AppServerProtocolError("turn/interrupt result must be an object")

    async def start_review(
        self, thread_id: str, target: dict[str, Any], *, delivery: str = "inline"
    ) -> dict[str, Any]:
        """Start the App Server's native review mode on an existing thread.

        Reviews deliberately use a separately-created, read-only thread in
        the caller.  ``detached`` is deprecated upstream and is never exposed
        here, avoiding an implicit second thread whose lifecycle we cannot
        reliably observe.
        """
        review_contract = self._review_start_contract()
        self._validate_rpc_id(thread_id, "thread")
        if delivery != "inline":
            raise AppServerProtocolError("review delivery must be inline")
        normalized = self._validate_review_target(target)
        result = await self.request(
            "review/start",
            {"threadId": thread_id, "target": normalized, "delivery": "inline"},
        )
        if not isinstance(result, dict):
            raise AppServerProtocolError("review/start result is invalid")
        self._require_fields(
            result, review_contract["responseRequired"],
            "review/start result",
        )
        review_thread_id = result.get("reviewThreadId")
        if review_thread_id != thread_id:
            raise AppServerProtocolError("review/start returned the wrong thread")
        self._validate_rpc_id(review_thread_id, "review thread")
        turn = result.get("turn")
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("id"), str)
            or turn.get("status") != "inProgress"
        ):
            raise AppServerProtocolError("review/start result missing active turn")
        self._validate_rpc_id(turn["id"], "review turn")
        return {"reviewThreadId": review_thread_id, "turn": turn}

    async def compact_thread(self, thread_id: str) -> None:
        """Trigger native history compaction for one loaded, idle thread."""
        self._validate_rpc_id(thread_id, "thread")
        result = await self.request(
            "thread/compact/start", {"threadId": thread_id}
        )
        if result != {}:
            raise AppServerProtocolError(
                "thread/compact/start result must be an object"
            )

    def _turn_params(
        self,
        thread_id: str,
        text: str,
        client_user_message_id: str | None,
        skill_inputs: list[dict[str, str]] | None = None,
        leading_context: str | None = None,
        leading_context_after_text: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise AppServerProtocolError("turn text must be non-empty")
        if leading_context is not None and (
            not isinstance(leading_context, str) or not leading_context.strip()
        ):
            raise AppServerProtocolError("turn leading context is invalid")
        if not isinstance(leading_context_after_text, bool):
            raise AppServerProtocolError("turn leading context ordering is invalid")
        inputs: list[dict[str, str]] = []
        if leading_context is not None and not leading_context_after_text:
            inputs.append({"type": "text", "text": leading_context})
        inputs.append({"type": "text", "text": text})
        if leading_context is not None and leading_context_after_text:
            inputs.append({"type": "text", "text": leading_context})
        thread_cwd = self._thread_cwds.get(thread_id)
        seen_skills: set[tuple[str, str]] = set()
        for skill in skill_inputs or []:
            if not isinstance(skill, dict) or set(skill) != {"type", "name", "path"}:
                raise AppServerProtocolError("structured skill input is malformed")
            name = skill.get("name")
            path = skill.get("path")
            if (
                skill.get("type") != "skill"
                or not isinstance(name, str)
                or not name
                or not isinstance(path, str)
                or not Path(path).is_absolute()
            ):
                raise AppServerProtocolError("structured skill input is invalid")
            key = (name, os.path.normpath(path))
            if key in seen_skills:
                raise AppServerProtocolError("duplicate structured skill input")
            entry = self._skills_by_cwd.get(thread_cwd or "")
            if not isinstance(entry, dict) or not any(
                candidate.get("enabled")
                and candidate.get("name") == key[0]
                and candidate.get("path") == key[1]
                for candidate in entry.get("skills", [])
            ):
                raise AppServerProtocolError(
                    "structured skill input was not resolved for the thread cwd"
                )
            seen_skills.add(key)
            inputs.append({"type": "skill", "name": name, "path": key[1]})
        params: dict[str, Any] = {"input": inputs}
        if client_user_message_id is not None:
            CodexAppServerClient._validate_rpc_id(
                client_user_message_id, "client user message"
            )
            params["clientUserMessageId"] = client_user_message_id
        return params

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
            raise AppServerProtocolError("cwd must be an absolute path")
        return os.path.normpath(cwd)

    def _review_start_contract(self) -> dict[str, Any]:
        """Require review/start in the verified protocol fixture."""
        methods = self._fixture.get("methods") if isinstance(self._fixture, dict) else None
        contract = methods.get("review/start") if isinstance(methods, dict) else None
        if not isinstance(contract, dict) or not isinstance(contract.get("responseRequired"), list):
            raise AppServerProtocolError("native review requires a verified review/start protocol fixture")
        return contract

    @staticmethod
    def _validate_review_target(target: dict[str, Any]) -> dict[str, Any]:
        """Accept exactly the four 0.159.2 ReviewTarget variants."""
        if not isinstance(target, dict) or not isinstance(target.get("type"), str):
            raise AppServerProtocolError("review target is invalid")
        kind = target["type"]
        if kind == "uncommittedChanges" and set(target) == {"type"}:
            return {"type": kind}
        if kind == "baseBranch" and set(target) == {"type", "branch"}:
            branch = target.get("branch")
            if isinstance(branch, str) and branch.strip():
                return {"type": kind, "branch": branch}
        if kind == "commit" and set(target).issubset({"type", "sha", "title"}):
            sha = target.get("sha")
            title = target.get("title")
            if (
                isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", sha)
                and (title is None or isinstance(title, str))
            ):
                value = {"type": kind, "sha": sha}
                if title is not None:
                    value["title"] = title
                return value
        if kind == "custom" and set(target) == {"type", "instructions"}:
            instructions = target.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                return {"type": kind, "instructions": instructions}
        raise AppServerProtocolError("review target is invalid")

    def _cached_model(self, requested: str) -> dict[str, Any]:
        models = self._model_catalogs.get(False)
        if models is None:
            raise AppServerProtocolError(
                "model catalog must be synchronized before starting a task"
            )
        matches = [
            item for item in models
            if requested in {item["id"], item["model"]}
        ]
        if len(matches) != 1:
            raise AppServerProtocolError("model is not in the synchronized catalog")
        return matches[0]

    @staticmethod
    def _validate_rpc_id(value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value)
        ):
            raise AppServerProtocolError(f"invalid {label} ID")

    @staticmethod
    def _require_thread_result(
        result: Any, expected_thread_id: str, method: str
    ) -> dict[str, Any]:
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != expected_thread_id:
            raise AppServerProtocolError(f"{method} result has the wrong thread")
        return result

    @staticmethod
    def _require_turn_result(result: Any, method: str) -> dict[str, Any]:
        turn = result.get("turn") if isinstance(result, dict) else None
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("id"), str)
            or turn.get("status") != "inProgress"
        ):
            raise AppServerProtocolError(f"{method} result missing turn")
        return turn

    async def _enqueue(self, message: dict[str, Any]) -> None:
        """Write through the single writer and wait for its actual send."""
        if not self._connected:
            raise self._disconnect_error or AppServerDisconnected(
                "App Server isn't connected"
            )
        sent = asyncio.get_running_loop().create_future()
        # A cancelled caller may no longer be awaiting this acknowledgement
        # when the writer fails. Consume its exception without masking it for
        # callers that are still awaiting the Future.
        sent.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        await self._outbound.put((json.dumps(message, separators=(",", ":")), sent, self._connection_epoch))
        try:
            async with asyncio.timeout(self.request_timeout):
                await asyncio.shield(sent)
        except TimeoutError as error:
            failure = AppServerDisconnected("App Server writer send timed out")
            self._mark_disconnected(
                failure, connection_epoch=self._connection_epoch,
                source="writer_send_timeout",
            )
            if not sent.done():
                sent.set_exception(failure)
            raise failure from error

    async def _writer(self, connection_epoch: int) -> None:
        sent: asyncio.Future[None] | None = None
        socket = self._socket
        try:
            while True:
                payload, sent, frame_epoch = await self._outbound.get()
                if (not self._connected or frame_epoch != connection_epoch
                        or connection_epoch != self._connection_epoch):
                    if not sent.done():
                        sent.set_exception(AppServerDisconnected("stale App Server writer frame"))
                    sent = None
                    continue
                await socket.send(payload)
                if not sent.done():
                    sent.set_result(None)
                sent = None
        except asyncio.CancelledError:
            if sent is not None and not sent.done():
                sent.set_exception(AppServerDisconnected("App Server writer stopped"))
            raise
        except Exception as error:
            if sent is not None and not sent.done():
                sent.set_exception(AppServerDisconnected("App Server writer failed"))
            self._mark_disconnected(
                error, connection_epoch=connection_epoch, source="writer_error"
            )

    async def _reader(self, connection_epoch: int) -> None:
        try:
            async for raw in self._socket:
                await self._handle_raw(raw, connection_epoch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._mark_disconnected(
                error, connection_epoch=connection_epoch, source="reader_error"
            )
        else:
            self._mark_disconnected(
                None, connection_epoch=connection_epoch, source="reader_eof"
            )

    async def _handle_raw(self, raw: str, connection_epoch: int) -> None:
        if connection_epoch != self._connection_epoch:
            return
        if not isinstance(raw, str):
            raise AppServerProtocolError("binary protocol frame rejected")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AppServerProtocolError("invalid JSON from App Server") from error
        if not isinstance(message, dict):
            raise AppServerProtocolError("JSON-RPC message must be an object")
        if "method" in message and "id" not in message:
            if message.get("method") in {
                "rawResponse/completed", "rawResponseItem/completed",
            }:
                # Never pass an internal raw Responses payload to runtime,
                # storage, logs, or browser notification handling.
                message = self._context_switch_projection(message)
                if message is None:
                    return
            if (
                self.notification_handler is not None
                or message.get("method") in {
                    "skills/changed", "account/updated", "account/login/completed",
                }
            ):
                try:
                    self._notifications.put_nowait((connection_epoch, message))
                except asyncio.QueueFull:
                    # Do not stall the sole reader (and therefore every RPC)
                    # behind an unbounded notification backlog.  The runtime
                    # will reconnect and reconcile from the native snapshot.
                    self._mark_disconnected(
                        AppServerError("notification queue is full"),
                        connection_epoch=connection_epoch,
                        source="notification_queue_full",
                    )
                    if self._socket is not None:
                        await self._socket.close()
            return
        notification_prefix = None
        if ("id" in message and "result" not in message and "error" not in message
                and message.get("method") in {"item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval", "item/permissions/requestApproval",
                    "mcpServer/elicitation/request"}):
            # Capture the boundary in the sole wire reader, not later when an
            # independently scheduled callback finally starts running.
            notification_prefix = asyncio.get_running_loop().create_future()
            self._notification_barriers.add(notification_prefix)
            try:
                self._notifications.put_nowait((connection_epoch, notification_prefix))
            except asyncio.QueueFull:
                self._notification_barriers.discard(notification_prefix)
                notification_prefix.cancel()
                self._mark_disconnected(AppServerError("notification queue is full"),
                    connection_epoch=connection_epoch, source="notification_queue_full")
                if self._socket is not None:
                    await self._socket.close()
                return
        self._dispatch_message(message, connection_epoch, notification_prefix=notification_prefix)

    def _context_switch_projection(
        self, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return only a safe new_context observation from a raw event.

        ``rawResponseItem/completed`` is explicitly internal-only in the
        versioned App Server schema. Its item may contain user text, tool
        arguments, outputs, or reasoning. This method reads only the strict
        discriminant and opaque call ID required for correlation, then returns
        a new minimal notification without retaining the raw message.
        """
        if message.get("method") != "rawResponseItem/completed":
            return None
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item")
        if (
            not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or not isinstance(item, dict)
        ):
            return None
        try:
            self._validate_rpc_id(thread_id, "raw thread")
            self._validate_rpc_id(turn_id, "raw turn")
        except AppServerProtocolError:
            return None
        item_type = item.get("type")
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        key = (thread_id, turn_id, call_id)
        if item_type == "function_call":
            if item.get("name") != "new_context":
                return None
            self._pending_context_switch_calls.add(key)
            state = "call_seen"
        elif item_type == "function_call_output":
            if key not in self._pending_context_switch_calls:
                return None
            self._pending_context_switch_calls.discard(key)
            state = "output_seen"
        else:
            return None
        call_key_hash = hashlib.sha256(
            ("new_context\\0" + call_id).encode("utf-8")
        ).hexdigest()
        return {
            "method": "internal/context-switch/observed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "callKeySha256": call_key_hash,
                "state": state,
                "serverVersion": self.server_version,
            },
        }

    def _dispatch_message(
        self, message: dict[str, Any], connection_epoch: int | None = None,
        *, notification_prefix: asyncio.Future[None] | None = None
    ) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict):
                    future.set_exception(
                        AppServerProtocolError("malformed JSON-RPC error")
                    )
                    return
                future.set_exception(
                    AppServerRPCError(
                        int(error.get("code", -32603)),
                        str(error.get("message", "unknown error")),
                        error.get("data"),
                    )
                )
            else:
                future.set_result(message["result"])
            return
        if "method" in message and "id" in message:
            received_epoch = self._connection_epoch if connection_epoch is None else connection_epoch
            task = self._start_callback(self._handle_server_request(message, received_epoch, notification_prefix=notification_prefix))
            if notification_prefix is not None:
                # Cleanup belongs to the task lifetime, even if cancellation
                # occurs before the coroutine gets its first execution slice.
                task.add_done_callback(lambda _task: self._release_notification_prefix(notification_prefix))
            return
        raise AppServerProtocolError("unrecognized JSON-RPC message")

    async def _notification_worker(self, connection_epoch: int) -> None:
        """Apply notifications in wire order while the reader stays responsive."""
        try:
            while True:
                epoch, queued = await self._notifications.get()
                if isinstance(queued, asyncio.Future):
                    if (not self._connected or epoch != connection_epoch
                            or epoch != self._connection_epoch):
                        if not queued.done():
                            queued.set_exception(
                                AppServerDisconnected("stale App Server notification barrier")
                            )
                    elif not queued.done():
                        queued.set_result(None)
                    continue
                message = queued
                if epoch != connection_epoch or epoch != self._connection_epoch:
                    continue
                try:
                    await self._handle_notification(message, connection_epoch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception(
                        "codex_app_server_notification_handler_failed method=%s",
                        message.get("method"),
                    )
        except asyncio.CancelledError:
            raise

    def _release_notification_prefix(self, future: asyncio.Future[None]) -> None:
        self._notification_barriers.discard(future)
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            # Consume a close/disconnect failure even when no await ran.
            future.exception()

    def _start_callback(self, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_done)
        return task

    def _callback_done(self, task: asyncio.Task[Any]) -> None:
        self._callback_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _handle_notification(
        self, message: dict[str, Any], connection_epoch: int | None = None
    ) -> None:
        if connection_epoch is not None and connection_epoch != self._connection_epoch:
            return
        if message.get("method") == "skills/changed":
            self.invalidate_skills()
        if message.get("method") in {"account/updated", "account/login/completed"}:
            # The worker has already rejected stale epochs before reaching us.
            self._clear_model_catalogs(reason="account update")
        if self.notification_handler is None:
            return
        handler = self.notification_handler
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(handler)
            parameters = signature.parameters
            if "connection_id" in parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            ):
                # Use the worker's epoch, not a subsequently reconnected
                # client property, so stale queued notifications cannot bind.
                kwargs["connection_id"] = (
                    self._connection_instance + ":" + str(connection_epoch)
                )
        except (TypeError, ValueError):
            pass
        result = handler(message, **kwargs)
        if inspect.isawaitable(result):
            await result

    async def _handle_server_request(
        self, message: dict[str, Any], connection_epoch: int | None = None,
        *, notification_prefix: asyncio.Future[None] | None = None
    ) -> None:
        received_epoch = self._connection_epoch if connection_epoch is None else connection_epoch
        if received_epoch != self._connection_epoch or not self._connected:
            if notification_prefix is not None:
                self._release_notification_prefix(notification_prefix)
            return
        request_id = message["id"]
        on_sent = None
        try:
            if notification_prefix is not None:
                try:
                    async with asyncio.timeout(self.request_timeout):
                        await asyncio.shield(notification_prefix)
                finally:
                    self._release_notification_prefix(notification_prefix)
                if received_epoch != self._connection_epoch or not self._connected:
                    raise AppServerDisconnected("native request prefix connection changed")
            if self.server_request_handler is None:
                raise AppServerRPCError(-32601, "server request handler unavailable")
            handler = self.server_request_handler
            kwargs: dict[str, Any] = {}
            try:
                signature = inspect.signature(handler)
                parameters = signature.parameters
                if "request_id" in parameters or any(
                    item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                ):
                    kwargs["request_id"] = request_id
                if "connection_id" in parameters or any(
                    item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                ):
                    kwargs["connection_id"] = self._connection_instance + ":" + str(received_epoch)
            except (TypeError, ValueError):
                # Builtins and mocks without a signature retain the legacy
                # two-argument contract.
                pass
            outcome = await handler(str(message["method"]), message.get("params"), **kwargs)
            on_sent = outcome.on_sent if isinstance(outcome, ServerRequestResponse) else None
            result = outcome.result if isinstance(outcome, ServerRequestResponse) else outcome
            response = {"id": request_id, "result": result}
        except AppServerRPCError as error:
            response = {
                "id": request_id,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "data": error.data,
                },
            }
        except Exception:
            response = {
                "id": request_id,
                "error": {"code": -32603, "message": "server request failed"},
            }
        if self._connected and received_epoch == self._connection_epoch:
            await self._enqueue(response)
            if on_sent is not None:
                callback_result = on_sent()
                if inspect.isawaitable(callback_result):
                    await callback_result

    def _mark_disconnected(
        self,
        cause: Exception | None,
        *,
        connection_epoch: int,
        source: str,
    ) -> None:
        if connection_epoch != self._connection_epoch or not self._connected:
            return
        self._connected = False
        self._initialized = False
        self._clear_model_catalogs(reason="disconnect")
        detail = f": {cause}" if cause else ""
        self._disconnect_error = AppServerDisconnected(
            f"App Server connection lost{detail}"
        )
        self._fail_notification_barriers(self._disconnect_error)
        self._fail_pending(self._disconnect_error)
        error_kind = type(cause).__name__ if cause is not None else "EOF"
        emit("app_server_disconnected", level="error", connection_epoch=connection_epoch, source=source, error_kind=error_kind, message=str(cause) if cause else "")
        if self.disconnect_handler is not None:
            try:
                self.disconnect_handler(connection_epoch, source, error_kind)
            except Exception:
                # The observer is diagnostic/recovery plumbing.  It must never
                # replace the authoritative transport failure seen by callers.
                LOGGER.error(
                    "codex_app_server_disconnect_observer_failed source=%s error=%s",
                    source,
                    error_kind,
                )

    def _fail_pending(self, error: Exception) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
        # Frames which never reached the socket writer must not be reported as
        # delivered. Drain their per-frame acknowledgements as failed too.
        while not self._outbound.empty():
            try:
                _payload, sent, _epoch = self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not sent.done():
                sent.set_exception(error)

    def _fail_notification_barriers(self, error: Exception) -> None:
        """Fail local barriers when their worker cannot reach them anymore."""
        # Track barriers before queue insertion: a full queue must not hide
        # flush callers from disconnect/close failure handling.
        for future in tuple(self._notification_barriers):
            if not future.done():
                future.set_exception(error)

    def _check_socket_security(self) -> Path:
        connect_path = self.socket_path
        try:
            info = self.socket_path.lstat()
            if stat.S_ISLNK(info.st_mode):
                # Since 0.156, upstream binds in a private daemon directory and
                # advertises a deterministic alias. Never accept arbitrary links
                # or connect through an alias after checking its target.
                uid = os.geteuid()
                parent = self.socket_path.parent.resolve(strict=True)
                parent_info = parent.lstat()
                if (
                    info.st_uid != uid
                    or not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != uid
                    or stat.S_IMODE(parent_info.st_mode) != 0o700
                ):
                    raise AppServerError("App Server alias parent must be owner-only")
                directory = Path(f"/tmp/codex-daemon-{uid}")
                directory_info = directory.lstat()
                if (
                    not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_uid != uid
                    or stat.S_IMODE(directory_info.st_mode) != 0o700
                ):
                    raise AppServerError("App Server daemon directory must be owner-only")
                digest = hashlib.sha256(os.fsencode(parent / self.socket_path.name)).hexdigest()
                connect_path = directory / digest
                if os.readlink(self.socket_path) != str(connect_path):
                    raise AppServerError("App Server socket alias target mismatch")
                info = connect_path.lstat()
        except OSError as error:
            raise AppServerError(
                f"App Server socket unavailable: {self.socket_path}"
            ) from error
        if not stat.S_ISSOCK(info.st_mode):
            raise AppServerError("App Server endpoint isn't a Unix socket")
        if info.st_uid != os.geteuid():
            raise AppServerError("App Server socket isn't owned by this service user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AppServerError("App Server socket must be owner-only")
        return connect_path

    @staticmethod
    def _load_fixture(path: Path) -> dict[str, Any]:
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AppServerProtocolError("protocol fixture cannot be loaded") from error
        required = {
            "fixtureVersion", "cliVersion", "serverVersion", "schemaSha256",
            "experimentalSchemaSha256", "initialize", "methods",
        }
        if not isinstance(fixture, dict) or not required.issubset(fixture):
            raise AppServerProtocolError("protocol fixture is incomplete")
        if fixture["fixtureVersion"] != 1:
            raise AppServerProtocolError("unsupported protocol fixture version")
        return fixture

    def _validate_initialize(self, result: Any) -> None:
        self._require_fields(result, ["userAgent"], "initialize result")
        if not isinstance(result["userAgent"], str):
            raise AppServerProtocolError("initialize userAgent must be a string")
        match = re.match(
            r"^[^/\r\n]+/(\d+\.\d+\.\d+"
            r"(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)(?:\s|$)",
            result["userAgent"],
        )
        actual = match.group(1) if match else "unknown"
        fixture = self._fixtures_by_version.get(actual)
        if fixture is None:
            supported = ", ".join(sorted(self._fixtures_by_version))
            raise AppServerProtocolError(
                f"App Server version mismatch: supported {supported}; got {actual}"
            )
        required = fixture["initialize"]["responseRequired"]
        self._require_fields(result, required, "initialize result")
        self._fixture = fixture

    def _validate_method_result(
        self,
        method: str,
        result: Any,
        *,
        expected_cwds: list[str] | None = None,
    ) -> None:
        schema = self._fixture["methods"][method]
        self._require_fields(result, schema["responseRequired"], f"{method} result")
        if not isinstance(result["data"], list):
            raise AppServerProtocolError(f"{method} data must be a list")
        if method == "model/list":
            next_cursor = result.get("nextCursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise AppServerProtocolError("model/list nextCursor must be a string or null")
            for item in result["data"]:
                self._require_fields(item, schema["itemRequired"], "model/list item")
                for field in ("id", "model", "displayName", "description"):
                    if not isinstance(item[field], str) or not item[field]:
                        raise AppServerProtocolError(
                            f"model/list item {field} must be non-empty"
                        )
                for field in ("hidden", "isDefault"):
                    if not isinstance(item[field], bool):
                        raise AppServerProtocolError(
                            f"model/list item {field} must be a boolean"
                        )
                default_effort = item["defaultReasoningEffort"]
                options = item["supportedReasoningEfforts"]
                if not isinstance(default_effort, str) or not default_effort:
                    raise AppServerProtocolError(
                        "model/list defaultReasoningEffort is invalid"
                    )
                if not isinstance(options, list) or not options:
                    raise AppServerProtocolError(
                        "model/list supportedReasoningEfforts must be non-empty"
                    )
                efforts: set[str] = set()
                for option in options:
                    self._require_fields(
                        option,
                        ["reasoningEffort", "description"],
                        "model/list reasoning option",
                    )
                    effort = option["reasoningEffort"]
                    if (
                        not isinstance(effort, str)
                        or not effort
                        or effort in efforts
                        or not isinstance(option["description"], str)
                    ):
                        raise AppServerProtocolError(
                            "model/list reasoning option is invalid"
                        )
                    efforts.add(effort)
                if default_effort not in efforts:
                    raise AppServerProtocolError(
                        "model/list default effort is not supported"
                    )
                service_tiers = item.get("serviceTiers", [])
                if not isinstance(service_tiers, list):
                    raise AppServerProtocolError(
                        "model/list serviceTiers must be a list"
                    )
                tier_ids: set[str] = set()
                for tier in service_tiers:
                    self._require_fields(
                        tier, ["id", "name", "description"],
                        "model/list service tier",
                    )
                    tier_id = tier.get("id")
                    if (
                        not isinstance(tier_id, str)
                        or not tier_id
                        or tier_id in tier_ids
                        or not isinstance(tier.get("name"), str)
                        or not isinstance(tier.get("description"), str)
                    ):
                        raise AppServerProtocolError(
                            "model/list service tier is invalid"
                        )
                    tier_ids.add(tier_id)
            return
        returned_cwds: list[str] = []
        for entry in result["data"]:
            self._require_fields(entry, schema["entryRequired"], "skills/list entry")
            cwd = entry["cwd"]
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise AppServerProtocolError("skills/list cwd must be absolute")
            returned_cwds.append(os.path.normpath(cwd))
            if not isinstance(entry["skills"], list) or not isinstance(entry["errors"], list):
                raise AppServerProtocolError("skills/list collections must be lists")
            for error in entry["errors"]:
                self._require_fields(error, ["message", "path"], "skills/list error")
                if not all(isinstance(error[field], str) for field in ("message", "path")):
                    raise AppServerProtocolError("skills/list error is invalid")
            for skill in entry["skills"]:
                self._require_fields(skill, schema["skillRequired"], "skills/list skill")
                if (
                    not isinstance(skill["name"], str)
                    or not skill["name"]
                    or not isinstance(skill["description"], str)
                    or not isinstance(skill["enabled"], bool)
                    or skill["scope"] not in _SKILL_SCOPES
                    or not isinstance(skill["path"], str)
                    or not Path(skill["path"]).is_absolute()
                ):
                    raise AppServerProtocolError("skills/list skill is invalid")
                interface = skill.get("interface")
                dependencies = skill.get("dependencies")
                if interface is not None and not isinstance(interface, dict):
                    raise AppServerProtocolError("skills/list interface is invalid")
                if dependencies is not None and not isinstance(dependencies, dict):
                    raise AppServerProtocolError("skills/list dependencies are invalid")
        if expected_cwds is not None:
            if returned_cwds != expected_cwds:
                raise AppServerProtocolError(
                    "skills/list response does not match requested cwds"
                )

    @staticmethod
    def _require_fields(value: Any, fields: list[str], label: str) -> None:
        if not isinstance(value, dict):
            raise AppServerProtocolError(f"{label} must be an object")
        missing = [field for field in fields if field not in value]
        if missing:
            raise AppServerProtocolError(
                f"{label} missing schema fields: {', '.join(missing)}"
            )
