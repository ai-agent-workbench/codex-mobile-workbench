import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.codex_app_server import AppServerProtocolError, CodexAppServerClient, PROTOCOL_FIXTURE_0_159_2
from engine.conversation_review import (
    _cleanup_turn_from_snapshot, _review_result, native_target, repository_fingerprint, validate_review_request,
    validate_scope, resolve_scope,
)


class ConversationReviewTests(unittest.TestCase):
    def test_scope_validation_rejects_option_and_control_branch(self):
        self.assertEqual(validate_scope({"type": "uncommittedChanges"}), {"type": "uncommittedChanges"})
        for scope in ({"type": "baseBranch", "branch": "-bad"}, {"type": "baseBranch", "branch": "main\nnext"}, {"type": "commit", "sha": "bad"}):
            with self.assertRaises(AppServerProtocolError):
                validate_scope(scope)

    def test_native_target_uses_pinned_commit(self):
        self.assertEqual(native_target({"type": "commit", "sha": "abc", "resolved_sha": "a" * 40}), {"type": "commit", "sha": "a" * 40})
        self.assertEqual(native_target({"type": "baseBranch", "branch": "main", "resolved_sha": "b" * 40}), {"type": "baseBranch", "branch": "b" * 40})

    def test_native_target_uses_top_level_instructions_for_custom_scope(self):
        self.assertEqual(
            native_target({"type": "custom"}, instructions="review this specific concern"),
            {"type": "custom", "instructions": "review this specific concern"},
        )

    def test_terminal_review_requires_native_exit_marker(self):
        snapshot = {"thread": {"id": "r", "turns": [{"id": "t", "status": "completed", "items": []}]}}
        with self.assertRaisesRegex(AppServerProtocolError, "exitedReviewMode"):
            _review_result(snapshot, "r", "t")
        failed = {"thread": {"id": "r", "turns": [{"id": "t", "status": "failed", "items": []}]}}
        with self.assertRaisesRegex(AppServerProtocolError, "did not complete"):
            _review_result(failed, "r", "t")

    def test_unknown_cleanup_turn_requires_nonempty_all_terminal_snapshot(self):
        empty = {"thread": {"id": "r", "turns": []}}
        self.assertEqual(_cleanup_turn_from_snapshot(empty, "r", None), (None, False))
        terminal = {"thread": {"id": "r", "turns": [{"id": "t", "status": "interrupted"}]}}
        self.assertEqual(_cleanup_turn_from_snapshot(terminal, "r", None), (None, True))

    def test_fingerprint_includes_untracked_content(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            (root / "tracked.txt").write_text("one")
            subprocess.run(["git", "-C", str(root), "add", "--", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            (root / "new.txt").write_text("one")
            nested = root / "nested"
            nested.mkdir()
            before = repository_fingerprint(str(nested))
            (root / "new.txt").write_text("two")
            after = repository_fingerprint(str(nested))
            self.assertNotEqual(before["sha256"], after["sha256"])

    def test_untracked_symlinks_hash_link_text_without_following_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", raw], check=True)
            subprocess.run(["git", "-C", raw, "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid", "commit", "--allow-empty", "-qm", "baseline"], check=True)
            link = root / "link"
            link.symlink_to("missing-first-target")
            before = repository_fingerprint(raw)
            link.unlink()
            link.symlink_to("missing-second-target")
            self.assertNotEqual(before["sha256"], repository_fingerprint(raw)["sha256"])

    def test_fingerprint_accepts_unborn_head_and_tracks_initial_files(self):
        with tempfile.TemporaryDirectory() as raw:
            subprocess.run(["git", "init", "-q", raw], check=True)
            path = Path(raw) / "first.py"
            path.write_text("value = 1\n")
            before = repository_fingerprint(raw)
            self.assertTrue(before["head"].startswith("unborn:refs/heads/"))
            subprocess.run(["git", "-C", raw, "add", "first.py"], check=True)
            staged = repository_fingerprint(raw)
            self.assertNotEqual(before["sha256"], staged["sha256"])
            path.write_text("value = 2\n")
            self.assertNotEqual(staged["sha256"], repository_fingerprint(raw)["sha256"])

    def test_fingerprint_and_scope_never_run_repository_clean_filter(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            marker = root / "filter-ran"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for key, value in (
                ("user.email", "test@example.invalid"),
                ("user.name", "test"),
                ("filter.evil.clean", f"sh -c 'echo filter >> {marker}; cat'"),
            ):
                subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
            (root / ".gitattributes").write_text("*.filtered filter=evil\n")
            (root / "tracked.filtered").write_text("one")
            subprocess.run(["git", "-C", str(root), "add", "--", ".gitattributes", "tracked.filtered"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            marker.unlink(missing_ok=True)
            (root / "tracked.filtered").write_text("two")
            resolve_scope(str(root), {"type": "commit", "sha": "HEAD"})
            repository_fingerprint(str(root))
            self.assertFalse(marker.exists(), "fingerprint executed repository clean filter")

    def test_fingerprint_includes_executable_mode_and_parent_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as outside:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", raw], check=True)
            subprocess.run(["git", "-C", raw, "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", raw, "config", "user.name", "test"], check=True)
            tracked = root / "dir" / "script"
            tracked.parent.mkdir()
            tracked.write_text("echo safe\n")
            subprocess.run(["git", "-C", raw, "add", "--", "dir/script"], check=True)
            subprocess.run(["git", "-C", raw, "commit", "-qm", "init"], check=True)
            before = repository_fingerprint(raw)
            os.chmod(tracked, 0o755)
            self.assertNotEqual(before["sha256"], repository_fingerprint(raw)["sha256"])
            tracked.parent.rename(root / "saved-dir")
            (Path(outside) / "script").write_text("must never be read")
            (root / "dir").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(AppServerProtocolError):
                repository_fingerprint(raw)

    def test_fingerprint_allows_content_larger_than_legacy_eight_mebibytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", raw], check=True)
            subprocess.run(["git", "-C", raw, "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", raw, "config", "user.name", "test"], check=True)
            (root / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            subprocess.run(["git", "-C", raw, "add", "--", "large.bin"], check=True)
            subprocess.run(["git", "-C", raw, "commit", "-qm", "init"], check=True)
            self.assertGreater(repository_fingerprint(raw)["untracked_bytes"], 8 * 1024 * 1024)

    def test_fingerprint_recurses_gitlink_head_and_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source, root = base / "source", base / "root"
            source.mkdir(); root.mkdir()
            for repo in (source, root):
                subprocess.run(["git", "init", "-q", repo], check=True)
                subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
                subprocess.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
            (source / "sub.txt").write_text("one")
            subprocess.run(["git", "-C", source, "add", "--", "sub.txt"], check=True)
            subprocess.run(["git", "-C", source, "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qam", "submodule"], check=True)
            before = repository_fingerprint(str(root))
            (root / "sub" / "sub.txt").write_text("dirty")
            self.assertNotEqual(before["sha256"], repository_fingerprint(str(root))["sha256"])

    def test_request_defaults_to_source_cwd(self):
        value = validate_review_request({"instructions": "focus", "scope": {"type": "uncommittedChanges"}}, "/tmp")
        self.assertEqual(value["cwd"], "/tmp")

    def test_review_wrapper_requires_0159_fields_and_inline_target(self):
        client = object.__new__(CodexAppServerClient)
        client._fixture = json.loads(PROTOCOL_FIXTURE_0_159_2.read_text())
        async def request(method, params):
            self.assertEqual(method, "review/start")
            self.assertEqual(params, {"threadId": "thread-1", "target": {"type": "commit", "sha": "a" * 40}, "delivery": "inline"})
            return {"reviewThreadId": "thread-1", "turn": {"id": "turn-1", "status": "inProgress"}}
        client.request = request
        value = __import__("asyncio").run(client.start_review("thread-1", {"type": "commit", "sha": "a" * 40}))
        self.assertEqual(value["turn"]["id"], "turn-1")


if __name__ == "__main__":
    unittest.main()

class _FakeReviewClient:
    def __init__(self, *, notification_handler=None, outcome="success", early=False, disconnect=False, review_text="complete"):
        self.notification_handler = notification_handler
        self.outcome, self.early, self.disconnect = outcome, early, disconnect
        self.calls, self.closed = [], False
        self.thread_id, self.turn_id = "review-thread", "review-turn"
        self.review_text = review_text
        self.review_started = asyncio.Event()

    async def connect(self): self.calls.append("connect")
    async def close(self): self.closed = True
    async def resolve_model_selection(self, model, effort, force_refresh=True):
        return {"model": "model-x", "reasoning_effort": "high"}
    async def start_thread(self, **params): self.calls.append(("thread", params)); return {"id": self.thread_id}
    async def start_review(self, thread_id, target):
        self.calls.append(("review", target))
        if self.early:
            await self.notification_handler({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": self.turn_id, "status": "inProgress"}}})
        if self.disconnect: raise __import__("engine.codex_app_server", fromlist=["AppServerDisconnected"]).AppServerDisconnected("lost")
        self.review_started.set()
        return {"reviewThreadId": thread_id, "turn": {"id": self.turn_id, "status": "inProgress"}}
    async def read_thread(self, thread_id, include_turns=True):
        status = {"success": "completed", "failed": "failed", "interrupted": "interrupted", "missing": "completed", "wait": "inProgress"}[self.outcome]
        items = [] if self.outcome == "missing" else ([{"type": "exitedReviewMode", "review": self.review_text}] if self.outcome == "success" else [])
        return {"thread": {"id": thread_id, "turns": [{"id": self.turn_id, "status": status, "items": items}]}}
    async def interrupt_turn(self, thread_id, turn_id): self.calls.append(("interrupt", turn_id))


class NativeReviewRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_empty_rollout_retries_reads_not_review_start(self):
        from engine.codex_app_server import AppServerRPCError
        class Materializing(_FakeReviewClient):
            reads = 0
            async def read_thread(self, *args, **kwargs):
                self.reads += 1
                if self.reads == 1:
                    raise AppServerRPCError(-32603, "failed to read session metadata rollout is empty")
                return await super().read_thread(*args, **kwargs)
        client = Materializing()
        result = await self._run(client)
        self.assertTrue(result["success"])
        self.assertEqual(client.reads, 2)
        self.assertEqual(sum(isinstance(call, tuple) and call[0] == "review"
                             for call in client.calls), 1)

    async def test_snapshot_does_not_retry_other_metadata_errors(self):
        from engine.codex_app_server import AppServerRPCError
        from engine.conversation_review import _read_review_snapshot
        class Corrupted:
            reads = 0
            async def read_thread(self, *args, **kwargs):
                self.reads += 1
                raise AppServerRPCError(-32603, "failed to read session metadata: invalid JSON")
        client = Corrupted()
        with self.assertRaises(AppServerRPCError):
            await _read_review_snapshot(client, "thread")
        self.assertEqual(client.reads, 1)

    async def test_cleanup_cancels_replacement_review_turn_not_stale_initial_id(self):
        from engine.conversation_review import _best_effort_review_cleanup
        class Replacement(_FakeReviewClient):
            stopped = False
            async def read_thread(self, thread_id, **kwargs):
                return {"thread": {"id": thread_id, "status": {"type": "idle" if self.stopped else "active"},
                    "turns": [
                        {"id": "initial-review", "status": "completed"},
                        {"id": "replacement-review", "status": "interrupted" if self.stopped else "inProgress"},
                    ]}}
            async def interrupt_turn(self, thread_id, turn_id):
                self.asserted_turn = turn_id
                self.stopped = True
        client = Replacement()
        confirmed, turn = await _best_effort_review_cleanup(client, lambda **handlers: client,
                                                          "review-thread", "initial-review")
        self.assertTrue(confirmed)
        self.assertEqual(turn, "replacement-review")
        self.assertEqual(client.asserted_turn, "replacement-review")

    async def _run(self, client, **kwargs):
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        def factory(**handlers):
            client.notification_handler = handlers.get("notification_handler")
            return client
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            return await run_native_review(factory, {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}})

    async def test_runner_uses_readonly_effort_and_native_target_with_early_notification(self):
        client = _FakeReviewClient(early=True)
        result = await self._run(client)
        start = next(call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "thread")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertEqual(start["review_reasoning_effort"], "high")
        self.assertEqual(start["dynamic_tools"], [])
        self.assertEqual(next(call[1] for call in client.calls if isinstance(call, tuple) and call[0] == "review"), {"type": "uncommittedChanges"})
        self.assertEqual(result["result_text"], "complete")
        self.assertEqual(result["configured_model"], "model-x")
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "interrupt" for call in client.calls))

    async def test_runner_keeps_review_when_fingerprint_or_post_scope_verification_fails(self):
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        client = _FakeReviewClient()
        scope_calls = 0
        def resolve(_cwd, scope):
            nonlocal scope_calls
            scope_calls += 1
            if scope_calls == 2:
                raise AppServerProtocolError("post-review scope is unavailable")
            return scope
        with patch("engine.conversation_review.resolve_scope", side_effect=resolve), patch(
            "engine.conversation_review.repository_fingerprint",
            side_effect=AppServerProtocolError("fingerprint budget exceeded"),
        ):
            result = await run_native_review(
                lambda **handlers: client,
                {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["before"]["status"], "unverified")
        self.assertEqual(result["after"]["status"], "unverified")
        self.assertEqual(result["scope_changed"], None)
        self.assertEqual(result["scope_verification"]["status"], "unverified")

    async def test_auxiliary_fingerprint_unexpected_failure_is_not_a_review_gate(self):
        from unittest.mock import patch
        from engine.conversation_review import _repository_fingerprint_or_unverified
        with patch("engine.conversation_review.repository_fingerprint",
                   side_effect=UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid")):
            result = await _repository_fingerprint_or_unverified("/tmp")
        self.assertEqual(result["status"], "unverified")

    async def test_runner_does_not_downgrade_unsupported_high_effort(self):
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        class HighUnsupported(_FakeReviewClient):
            async def resolve_model_selection(self, model, effort, force_refresh=True):
                self.requested_effort = effort
                if effort == "high":
                    raise AppServerProtocolError("reasoning effort 'high' is not supported")
                return await super().resolve_model_selection(model, effort, force_refresh=force_refresh)
        client = HighUnsupported()
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch(
            "engine.conversation_review.repository_fingerprint", return_value=fingerprint,
        ):
            with self.assertRaisesRegex(AppServerProtocolError, "high"):
                await run_native_review(
                    lambda **handlers: client,
                    {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                )
        self.assertEqual(client.requested_effort, "high")

    async def test_runner_journals_thread_and_start_before_native_review(self):
        client = _FakeReviewClient()
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        lifecycle = []
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            await run_native_review(
                lambda **handlers: client,
                {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                lifecycle_update=lifecycle.append,
            )
        self.assertEqual(lifecycle[0], {"phase": "thread_created", "review_thread_id": "review-thread"})
        self.assertEqual(lifecycle[1], {"phase": "start_requested", "review_thread_id": "review-thread"})
        self.assertEqual(lifecycle[2]["phase"], "turn_started")
        self.assertIsNone(lifecycle[-1])

    async def test_thread_created_without_review_start_is_closed_not_orphaned(self):
        client = _FakeReviewClient()
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        lifecycle, guards = [], iter((True, False))
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            with self.assertRaisesRegex(AppServerProtocolError, "source turn ended"):
                await run_native_review(
                    lambda **handlers: client,
                    {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                    launch_guard=lambda: next(guards), lifecycle_update=lifecycle.append,
                )
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "review" for call in client.calls))
        self.assertIsNone(lifecycle[-1])
        self.assertTrue(client.closed)

    async def test_runner_rejects_failed_and_missing_exit_marker(self):
        for outcome, message in (("failed", "did not complete"), ("interrupted", "did not complete"), ("missing", "exitedReviewMode")):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(AppServerProtocolError, message):
                    await self._run(_FakeReviewClient(outcome=outcome))

    async def test_runner_disconnect_never_retries_review_start(self):
        client = _FakeReviewClient(disconnect=True)
        with self.assertRaises(Exception):
            await self._run(client)
        self.assertEqual(sum(1 for call in client.calls if isinstance(call, tuple) and call[0] == "review"), 1)
        self.assertTrue(client.closed)

    async def test_runner_cancellation_interrupts_known_turn(self):
        # This tests a confirmed interrupt, not the separate orphan deadline.
        class Stoppable(_FakeReviewClient):
            async def interrupt_turn(self, thread_id, turn_id):
                await super().interrupt_turn(thread_id, turn_id)
                self.outcome = "interrupted"
        timeout_client = Stoppable(outcome="wait")
        timeout_task = asyncio.create_task(self._run(timeout_client))
        await asyncio.wait_for(timeout_client.review_started.wait(), timeout=3)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(timeout_task, timeout=.01)
        self.assertIn(("interrupt", "review-turn"), timeout_client.calls)
        cancel_client = Stoppable(outcome="wait")
        task = asyncio.create_task(self._run(cancel_client))
        await asyncio.wait_for(cancel_client.review_started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(("interrupt", "review-turn"), cancel_client.calls)

    async def test_runner_early_notification_then_lost_response_interrupts_known_turn(self):
        class Stoppable(_FakeReviewClient):
            async def interrupt_turn(self, thread_id, turn_id):
                await super().interrupt_turn(thread_id, turn_id)
                self.outcome = "interrupted"
        client = Stoppable(early=True, disconnect=True, outcome="wait")
        with self.assertRaises(Exception):
            await self._run(client)
        self.assertIn(("interrupt", "review-turn"), client.calls)

    async def test_disconnected_cleanup_uses_fresh_read_interrupt_only_client(self):
        from engine.conversation_review import _best_effort_review_cleanup

        class Dead:
            async def read_thread(self, *_args, **_kwargs): raise ConnectionError("gone")
            async def interrupt_turn(self, *_args, **_kwargs): raise ConnectionError("gone")

        class Recovery:
            def __init__(self): self.calls = []; self.interrupted = False; self.closed = False
            async def connect(self): self.calls.append("connect")
            async def close(self): self.closed = True
            async def interrupt_turn(self, thread_id, turn_id):
                self.calls.append(("interrupt", thread_id, turn_id)); self.interrupted = True
            async def read_thread(self, thread_id, include_turns=True):
                self.calls.append(("read", thread_id))
                return {"thread": {"id": thread_id, "turns": [{
                    "id": "turn", "status": "interrupted" if self.interrupted else "inProgress",
                }]}}

        recovery = Recovery()
        confirmed, turn = await _best_effort_review_cleanup(
            Dead(), lambda **_handlers: recovery, "thread", "turn",
        )
        self.assertTrue(confirmed)
        self.assertEqual(turn, "turn")
        self.assertEqual(recovery.calls[0], "connect")
        self.assertIn(("interrupt", "thread", "turn"), recovery.calls)
        self.assertTrue(recovery.closed)

    async def test_cancelled_runner_retains_unconfirmed_cleanup_ids_via_callback(self):
        client = _FakeReviewClient(outcome="wait")
        orphan = {}
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            task = asyncio.create_task(run_native_review(
                lambda **handlers: client, {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                cleanup_unconfirmed=lambda thread_id, turn_id: orphan.update(thread_id=thread_id, turn_id=turn_id),
            ))
            async with asyncio.timeout(5):
                while not any(isinstance(call, tuple) and call[0] == "review" for call in client.calls):
                    await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(orphan, {"thread_id": "review-thread", "turn_id": "review-turn"})

    async def test_second_cancel_waits_for_cleanup_close_and_persists_orphan(self):
        client = _FakeReviewClient(outcome="wait")
        orphan = {}
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64, "untracked_bytes": 0}
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            task = asyncio.create_task(run_native_review(
                lambda **handlers: client, {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                cleanup_unconfirmed=lambda thread_id, turn_id: orphan.update(thread_id=thread_id, turn_id=turn_id),
            ))
            async with asyncio.timeout(5):
                while not any(isinstance(call, tuple) and call[0] == "review" for call in client.calls):
                    await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(orphan, {"thread_id": "review-thread", "turn_id": "review-turn"})
        self.assertTrue(client.closed)

    async def test_cancel_at_cleanup_confirmation_does_not_leave_stale_orphan(self):
        from unittest.mock import patch
        from engine.conversation_review import run_native_review
        class Stoppable(_FakeReviewClient):
            async def interrupt_turn(self, *args):
                self.outcome = "interrupted"
        client = Stoppable(outcome="wait")
        orphan, confirmations = {}, []
        task = None
        def confirmed():
            orphan.clear()
            confirmations.append(True)
            if len(confirmations) == 1:
                task.cancel()
        fingerprint = {"root": "/tmp", "head": "a" * 40, "sha256": "b" * 64}
        with patch("engine.conversation_review.resolve_scope", side_effect=lambda _cwd, scope: scope), patch("engine.conversation_review.repository_fingerprint", return_value=fingerprint):
            task = asyncio.create_task(run_native_review(
                lambda **handlers: client,
                {"cwd": "/tmp", "instructions": "focus", "scope": {"type": "uncommittedChanges"}},
                cleanup_unconfirmed=lambda thread, turn: orphan.update(thread=thread, turn=turn),
                cleanup_confirmed=confirmed,
            ))
            async with asyncio.timeout(5):
                while not any(isinstance(call, tuple) and call[0] == "review" for call in client.calls):
                    await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(orphan)
        self.assertGreaterEqual(len(confirmations), 2)
        self.assertTrue(client.closed)

class ReviewStopOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_interrupt_is_not_replayed_by_reconnect(self):
        from engine.codex_app_server import AppServerDisconnected
        from engine.conversation_review import _best_effort_review_cleanup
        calls=[]
        class Original(_FakeReviewClient):
            async def interrupt_turn(self, thread_id, turn_id):
                calls.append(('original',turn_id))
                raise AppServerDisconnected('delivery unknown')
        class Recovery(_FakeReviewClient):
            reads=0
            async def read_thread(self, thread_id, **kwargs):
                self.reads+=1
                if self.reads > 1: self.outcome='interrupted'
                return await super().read_thread(thread_id,**kwargs)
            async def interrupt_turn(self, thread_id, turn_id):
                calls.append(('recovery',turn_id))
        original, recovery=Original(outcome='wait'),Recovery(outcome='wait')
        confirmed,_=await _best_effort_review_cleanup(original,lambda **kwargs:recovery,'review-thread','review-turn')
        self.assertTrue(confirmed)
        self.assertEqual(calls,[('original','review-turn')])

    async def test_transferred_cleanup_never_interrupts(self):
        from engine.conversation_review import _best_effort_review_cleanup
        client=_FakeReviewClient(outcome='wait')
        confirmed,_=await _best_effort_review_cleanup(client,lambda **kwargs:client,'review-thread','review-turn',
                                                     cleanup_transferred=lambda:True)
        self.assertFalse(confirmed)
        self.assertFalse(any(isinstance(call,tuple) and call[0]=='interrupt' for call in client.calls))

    async def test_cancelled_runner_closes_without_competing_native_cleanup(self):
        from unittest.mock import patch, AsyncMock
        from engine.conversation_review import run_native_review
        client=_FakeReviewClient(outcome='wait')
        transferred=False
        fingerprint={'root':'/tmp','head':'a'*40,'sha256':'b'*64,'untracked_bytes':0}
        with patch('engine.conversation_review.resolve_scope',side_effect=lambda cwd,scope:scope), \
             patch('engine.conversation_review.repository_fingerprint',return_value=fingerprint), \
             patch('engine.conversation_review._best_effort_review_cleanup',new_callable=AsyncMock) as cleanup:
            task=asyncio.create_task(run_native_review(lambda **kwargs:client,
                {'cwd':'/tmp','instructions':'focus','scope':{'type':'uncommittedChanges'}},
                cleanup_transferred=lambda:transferred))
            async with asyncio.timeout(5):
                while not any(isinstance(call,tuple) and call[0]=='review' for call in client.calls):
                    await asyncio.sleep(.001)
            transferred=True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            cleanup.assert_not_awaited()
        self.assertTrue(client.closed)
