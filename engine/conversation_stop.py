"""Bounded native conversation-tree stop; acknowledgements are not completion.

Only ancestor-filtered native threads and journal-owned reviews are touched.
Background terminals are observed, never cleaned by an agent-stop request.
No thread is resumed, and
an uncertain mutation is never retried by verification or process recovery.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from engine.codex_app_server import AppServerError, AppServerProtocolError, AppServerRPCError
from engine.conversation_store import ACTIVE_TURN_STATUSES

_PENDING = {"submitted", "accepted", "delivery_unknown"}
_RESULT_PREFIX = "conversation_tree_stop:"


def is_tree_stop(command: dict[str, Any] | None) -> bool:
    return bool(command and command.get("kind") == "interrupt"
                and command.get("payload", {}).get("scope") == "conversation_tree")


def pending_tree_stop(command: dict[str, Any] | None) -> bool:
    return is_tree_stop(command) and command.get("state") in _PENDING


class ConversationTreeStop:
    def __init__(self, client: Any, root: str, *, observe_root_goal: Callable,
                 passes: int = 6, interval: float = .15, owned_review_threads: tuple[str, ...] = (),
                 interrupt_delivered_threads: tuple[str, ...] = ()):
        self.client = client
        self.root = root
        self.observe_root_goal = observe_root_goal
        self.passes = passes
        self.interval = interval
        self.owned_review_threads = owned_review_threads
        self.observed_ids: frozenset[str] = frozenset()
        self.attempted: set[tuple[str, str, str]] = set()
        self.interrupt_delivery: set[str] = set(interrupt_delivered_threads)
        self.root_turn_ids: list[str] = []
        self.loaded_threads: set[str] = set()

    async def _tree_ids(self) -> list[str]:
        # Native reviews are separate roots. Their agents still belong to
        # this task; validate every root's native ancestry, not its cwd.
        roots = list(dict.fromkeys([self.root, *self.owned_review_threads]))
        ids = list(roots)
        for root in roots:
            ids.extend(entry['id'] for entry in await self.client.list_descendant_threads(root))
        return list(dict.fromkeys(ids))

    async def observe_background(self, result: dict[str, Any]) -> dict[str, Any]:
        """Advisory inventory has one separate budget, never a stop dependency."""
        try:
            inventories = await asyncio.wait_for(asyncio.gather(*(
                self.client.list_background_terminals(tid) for tid in sorted(self.loaded_threads)
            ), return_exceptions=True), timeout=1)
            return {**result,
                'terminals': sum(len(items) for items in inventories if isinstance(items, list)),
                'background_errors': sum(not isinstance(items, list) for items in inventories)}
        except TimeoutError:
            return {**result, 'background_errors': len(self.loaded_threads)}

    async def run(self, *, mutate: bool, observe_background: bool = True) -> dict[str, Any]:
        if not getattr(self.client, "supports_conversation_tree_stop", False):
            raise AppServerProtocolError("conversation tree stop is unsupported")
        result = {"status": "unconfirmed", "threads": 0, "active_threads": 0,
                  "active_goals": 0, "terminals": 0, "background_errors": 0, "errors": 0}
        stable: frozenset[str] | None = None
        for _ in range(self.passes):
            ids = await self._tree_ids()
            self.loaded_threads = set()
            result = {**result, "threads": len(ids), "active_threads": 0,
                      "active_goals": 0, "terminals": 0, "background_errors": 0, "errors": 0}
            # Root first: stop goal continuation before its children can spawn.
            for thread_id in ids:
                try:
                    goal = await self.client.get_thread_goal(thread_id)
                    if thread_id == self.root:
                        self.observe_root_goal(goal)
                    if goal and goal.get("status") == "active":
                        result["active_goals"] += 1
                        key = (thread_id, "pause", "")
                        if mutate and key not in self.attempted:
                            self.attempted.add(key)  # remember before await; recovery never replays
                            paused = await self.client.set_thread_goal(thread_id, status="paused")
                            if thread_id == self.root:
                                self.observe_root_goal(paused)
                    snapshot = await self.client.read_thread(thread_id, include_turns=False)
                    thread = snapshot.get("thread", {})
                    if thread.get("id") != thread_id:
                        raise AppServerProtocolError("stop read returned another thread")
                    status = (thread.get("status") or {}).get("type")
                    if status == "active":
                        result["active_threads"] += 1
                        if mutate and thread_id not in self.interrupt_delivery:
                            turns = await self.client.read_thread_turn_metadata(thread_id)
                            # These are candidates, NOT a claim that newest is
                            # current. An explicit stop may try each, stopping
                            # after native accepts one; later passes only read.
                            for turn in reversed(turns):
                                if turn.get("status") not in ACTIVE_TURN_STATUSES:
                                    continue
                                key = (thread_id, "interrupt", turn["id"])
                                if key in self.attempted:
                                    continue
                                self.attempted.add(key)
                                self.interrupt_delivery.add(thread_id)
                                try:
                                    await self.client.interrupt_turn(thread_id, turn["id"])
                                    break
                                except AppServerRPCError as error:
                                    if error.code != -32600:
                                        raise
                                    self.interrupt_delivery.discard(thread_id)
                    elif status not in {"idle", "notLoaded"}:
                        raise AppServerProtocolError("stop thread runtime status is unknown")
                    # Inventory is deferred until all agents are stopped.
                    # Never resume unloaded threads for advisory information.
                    if status != "notLoaded":
                        self.loaded_threads.add(thread_id)
                except AppServerError:
                    result["errors"] += 1
            # Detect agents spawned while the preceding scan was in flight.
            observed = frozenset(ids)
            after_ids = frozenset(await self._tree_ids())
            self.observed_ids = observed
            quiet = (observed == after_ids and not any(result[key] for key in
                     ("active_threads", "active_goals", "errors")))
            if quiet and stable == observed:
                # IDs, not local import timestamps, delimit the stopped epoch
                # and keep late-hydrated historical questions from auto-answer.
                turns = await self.client.read_thread_turn_metadata(self.root)
                self.root_turn_ids = [turn['id'] for turn in turns]
                result = {**result, "status": "verified", "root_turn_ids": self.root_turn_ids}
                return await self.observe_background(result) if observe_background else result
            stable = observed if quiet else None
            await asyncio.sleep(self.interval)
        return result
