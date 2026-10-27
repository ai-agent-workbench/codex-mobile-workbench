"""Lifecycle-safe runner for the App Server native code-review channel.

A review is intentionally not a normal conversation turn: it owns a fresh
read-only thread and a fresh App Server connection, then waits for the native
``exitedReviewMode`` item in the matching terminal turn.  The runner never
uses a model final message as proof that review completed.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from engine.codex_app_server import AppServerError, AppServerProtocolError, AppServerRPCError

_POLL_SECONDS = 1.0
# A review fingerprint is streamed, never held as one worktree-sized object.
# Auxiliary evidence has a resource budget; exceeding it never blocks review.
_MAX_FINGERPRINT_BYTES = 512 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024


class ReviewCleanupUnconfirmed(AppServerProtocolError):
    """A known native review could not be observed terminal after cancellation."""

    def __init__(self, message: str, review_thread_id: str, review_turn_id: str | None):
        super().__init__(message)
        self.review_thread_id = review_thread_id
        self.review_turn_id = review_turn_id


def code_review_dynamic_tool() -> dict[str, Any]:
    """The narrowly-shaped dynamic tool registered only on newly made threads."""
    return {
        "type": "function",
        "name": "code_review",
        "description": (
            "Run an independent, read-only native code review. It defaults to high "
            "reasoning effort and has no local total time limit. It returns the native "
            "result unchanged, is not release approval, and does not automatically retry "
            "an unknown outcome."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Optional absolute repository path; defaults to this conversation's cwd."},
                "instructions": {"type": "string", "description": "Required original request and review focus."},
                "scope": {
                    "type": "object",
                    "description": "The exact review scope.",
                    "properties": {
                        "type": {"type": "string", "enum": ["uncommittedChanges", "baseBranch", "commit", "custom"]},
                        "branch": {"type": "string"},
                        "sha": {"type": "string"},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            },
            "required": ["instructions", "scope"],
            "additionalProperties": False,
        },
    }


def validate_scope(scope: Any) -> dict[str, str]:
    if not isinstance(scope, dict) or not isinstance(scope.get("type"), str):
        raise AppServerProtocolError("code review scope is invalid")
    kind = scope["type"]
    if kind == "uncommittedChanges" and set(scope) == {"type"}:
        return {"type": kind}
    if kind == "custom" and set(scope) == {"type"}:
        return {"type": kind}
    if kind == "baseBranch" and set(scope) == {"type", "branch"}:
        branch = scope.get("branch")
        if (
            isinstance(branch, str) and branch.strip() == branch
            and not branch.startswith("-") and not any(ord(char) < 32 or ord(char) == 127 for char in branch)
        ):
            return {"type": kind, "branch": branch}
    if kind == "commit" and set(scope) == {"type", "sha"}:
        sha = scope.get("sha")
        if isinstance(sha, str) and len(sha) in range(7, 65) and all(c in "0123456789abcdefABCDEF" for c in sha):
            return {"type": kind, "sha": sha}
    raise AppServerProtocolError("code review scope is invalid")


def validate_review_request(arguments: Any, default_cwd: str) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset({"cwd", "instructions", "scope"}):
        raise AppServerProtocolError("code review arguments are invalid")
    instructions = arguments.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise AppServerProtocolError("code review instructions are invalid")
    cwd = arguments.get("cwd", default_cwd)
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        raise AppServerProtocolError("code review cwd must be absolute")
    return {"cwd": os.path.normpath(cwd), "instructions": instructions.strip(), "scope": validate_scope(arguments.get("scope"))}


def _run_git(cwd: str, *args: str, max_bytes: int = _MAX_GIT_OUTPUT_BYTES,
             pass_fds: tuple[int, ...] = (), allowed_returncodes: tuple[int, ...] = (0,)) -> bytes:
    """Run only Git builtins without hooks, fsmonitor, textconv, or a shell."""
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args],
                shell=False, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
                pass_fds=pass_fds,
            )
            if completed.returncode not in allowed_returncodes:
                raise subprocess.CalledProcessError(completed.returncode, completed.args)
            del completed
            size = output.tell()
            if size > max_bytes:
                raise AppServerProtocolError("review fingerprint input exceeds limit")
            output.seek(0)
            return output.read()
    except (OSError, subprocess.SubprocessError) as error:
        raise AppServerProtocolError("code review cwd is not a readable git repository") from error


def resolve_scope(cwd: str, scope: dict[str, str]) -> dict[str, str]:
    """Pin a commit/base branch before review so moving refs are observable."""
    resolved = dict(scope)
    if scope["type"] in {"commit", "baseBranch"}:
        reference = scope["sha"] if scope["type"] == "commit" else scope["branch"]
        value = _run_git(
            cwd, "rev-parse", "--verify", "--end-of-options", reference + "^{commit}",
            max_bytes=128,
        ).decode("ascii", "strict").strip()
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise AppServerProtocolError("review scope does not resolve to a commit")
        resolved["resolved_sha"] = value
    return resolved


class _FingerprintBudget:
    def __init__(self, deadline: float) -> None:
        self.used = 0
        self.deadline = deadline

    def add(self, amount: int) -> None:
        if time.monotonic() > self.deadline:
            raise AppServerProtocolError("review fingerprint timed out")
        self.used += amount
        if self.used > _MAX_FINGERPRINT_BYTES:
            raise AppServerProtocolError("review content exceeds fingerprint limit")


def _safe_parent_fd(root_fd: int, raw: bytes) -> tuple[int, bytes]:
    """Open every parent under ``root_fd`` without following a symlink."""
    parts = raw.split(b"/")
    if not parts or any(part in {b"", b".", b".."} for part in parts):
        raise AppServerProtocolError("review worktree path is invalid")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd, parts[-1]
    except OSError:
        os.close(parent_fd)
        raise


def _fd_cwd(descriptor: int) -> str:
    return f"/proc/self/fd/{descriptor}"


def _hash_worktree_path(
    root_fd: int, raw: bytes, label: bytes, digest: Any, budget: _FingerprintBudget,
    *, missing_allowed: bool,
) -> None:
    """Hash raw worktree content through descriptor-relative, no-follow opens."""
    try:
        parent_fd, leaf = _safe_parent_fd(root_fd, raw)
    except OSError as error:
        if missing_allowed and error.errno == errno.ENOENT:
            digest.update(label + b"_missing\0" + raw + b"\0")
            return
        raise AppServerProtocolError("review worktree file is unavailable") from error
    try:
        try:
            info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            if missing_allowed:
                digest.update(label + b"_missing\0" + raw + b"\0")
                return
            raise AppServerProtocolError("review worktree file is unavailable") from error
        mode = info.st_mode & 0o7777
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(leaf, dir_fd=parent_fd)
            target_bytes = os.fsencode(target)
            budget.add(len(target_bytes))
            digest.update(label + b"_symlink\0" + raw + b"\0")
            digest.update(mode.to_bytes(4, "big") + len(target_bytes).to_bytes(8, "big"))
            digest.update(target_bytes)
            return
        if not stat.S_ISREG(info.st_mode):
            raise AppServerProtocolError("review worktree file is not regular")
        if budget.used + info.st_size > _MAX_FINGERPRINT_BYTES:
            raise AppServerProtocolError("review content exceeds fingerprint limit")
        digest.update(label + b"\0" + raw + b"\0")
        digest.update(mode.to_bytes(4, "big") + info.st_size.to_bytes(8, "big"))
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or (opened.st_mode & 0o7777) != mode
                or opened.st_size != info.st_size
            ):
                raise AppServerProtocolError("review worktree file changed while opening")
            read_bytes = 0
            while chunk := handle.read(256 * 1024):
                read_bytes += len(chunk)
                budget.add(len(chunk))
                digest.update(chunk)
            if read_bytes != info.st_size:
                raise AppServerProtocolError("review worktree file changed while reading")
    except OSError as error:
        raise AppServerProtocolError("review worktree file is unavailable") from error
    finally:
        os.close(parent_fd)


def _hash_repository(
    root_fd: int, git_cwd: str, digest: Any, budget: _FingerprintBudget,
    *, label: bytes, visited: set[tuple[int, int]], depth: int,
) -> str:
    """Hash index and raw worktree recursively, including gitlink worktrees."""
    if depth > 8:
        raise AppServerProtocolError("nested git repository depth exceeds limit")
    budget.add(0)
    root_stat = os.fstat(root_fd)
    identity = (root_stat.st_dev, root_stat.st_ino)
    if identity in visited:
        raise AppServerProtocolError("nested git repository repeats a parent")
    visited.add(identity)
    try:
        head = _run_git(
            git_cwd, "rev-parse", "--verify", "--quiet", "HEAD", max_bytes=128,
            pass_fds=(root_fd,), allowed_returncodes=(0, 1),
        ).decode().strip()
        if not head:
            branch = _run_git(git_cwd, "symbolic-ref", "--quiet", "HEAD", max_bytes=1024, pass_fds=(root_fd,)).decode().strip()
            head = "unborn:" + branch
        index = _run_git(git_cwd, "ls-files", "--stage", "-z", pass_fds=(root_fd,))
        untracked = _run_git(git_cwd, "ls-files", "--others", "--exclude-standard", "-z", pass_fds=(root_fd,))
        digest.update(label + b"head\0" + head.encode() + b"\0")
        digest.update(label + b"index\0" + index)
        for entry in index.split(b"\0"):
            if not entry or b"\t" not in entry:
                continue
            metadata, raw = entry.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            digest.update(label + b"tracked_index\0" + metadata + b"\0")
            if mode != b"160000":
                _hash_worktree_path(root_fd, raw, label + b"tracked", digest, budget, missing_allowed=True)
                continue
            # A gitlink is a directory, not a normal tracked file.  Recurse
            # through its raw index/worktree at the already no-follow-opened
            # descriptor so its HEAD and internal dirty files are observable.
            try:
                parent_fd, leaf = _safe_parent_fd(root_fd, raw)
                try:
                    sub_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    digest.update(label + b"gitlink_missing\0" + raw + b"\0")
                    continue
                raise AppServerProtocolError("review gitlink is unavailable") from error
            try:
                digest.update(label + b"gitlink\0" + raw + b"\0")
                # An uninitialized gitlink is often just an empty directory.
                # Do not let Git discover the outer repository from it.
                sub_cwd = _fd_cwd(sub_fd)
                sub_top = _run_git(sub_cwd, "rev-parse", "--show-toplevel", max_bytes=16 * 1024, pass_fds=(sub_fd,)).decode().strip()
                if not sub_top or not os.path.samefile(sub_top, sub_cwd):
                    digest.update(label + b"gitlink_uninitialized\0" + raw + b"\0")
                    continue
                _hash_repository(sub_fd, sub_cwd, digest, budget, label=label + b"sub/", visited=visited, depth=depth + 1)
            finally:
                os.close(sub_fd)
        for raw in untracked.split(b"\0"):
            if raw:
                _hash_worktree_path(root_fd, raw, label + b"untracked", digest, budget, missing_allowed=False)
        return head
    finally:
        visited.remove(identity)


def repository_fingerprint(cwd: str) -> dict[str, Any]:
    """A bounded raw-content fingerprint, with no Git filters or status/diff."""
    root = _run_git(cwd, "rev-parse", "--show-toplevel", max_bytes=16 * 1024).decode().strip()
    if not root or not Path(root).is_absolute():
        raise AppServerProtocolError("code review repository root is invalid")
    try:
        root_fd = os.open(os.fsencode(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise AppServerProtocolError("code review repository root is unavailable") from error
    digest = hashlib.sha256()
    budget = _FingerprintBudget(time.monotonic() + 60)
    try:
        head = _hash_repository(root_fd, _fd_cwd(root_fd), digest, budget, label=b"", visited=set(), depth=0)
    finally:
        os.close(root_fd)
    return {"root": root, "head": head, "sha256": digest.hexdigest(), "untracked_bytes": budget.used}


def _review_instructions(request: dict[str, Any], fingerprint: dict[str, Any]) -> str:
    if "sha256" in fingerprint:
        repository = fingerprint.get("root", request["cwd"])
        fingerprint_note = fingerprint["sha256"]
    else:
        repository = request["cwd"]
        fingerprint_note = "unavailable; local scope verification is unverified: " + str(
            fingerprint.get("error", "unknown fingerprint error")
        )
    return (
        "Perform an official code review. You are in a dedicated read-only review "
        "thread. You may read local code and already-authorized external material, "
        "but do not edit files, publish, deploy, send messages, or modify remote "
        "resources. Using an external tool does not expand existing authorization. "
        "Do not claim a self-check is an official review.\n\n"
        f"Repository: {repository}\nDeclared scope: {json.dumps(request['scope'], ensure_ascii=False, sort_keys=True)}\n"
        f"Review-start fingerprint: {fingerprint_note}\n"
        "Original request and review focus:\n" + request["instructions"]
    )


def native_target(scope: dict[str, str], *, instructions: str | None = None) -> dict[str, str]:
    """Translate declared scope to the official target with its ref pinned."""
    if scope["type"] == "uncommittedChanges":
        return {"type": "uncommittedChanges"}
    if scope["type"] == "commit":
        return {"type": "commit", "sha": scope["resolved_sha"]}
    if scope["type"] == "custom":
        if not isinstance(instructions, str) or not instructions.strip():
            raise AppServerProtocolError("custom review instructions are invalid")
        return {"type": "custom", "instructions": instructions}
    # ReviewStart's baseBranch target accepts a revision string.  Passing the
    # verified immutable commit avoids a branch moving while the review runs.
    return {"type": "baseBranch", "branch": scope["resolved_sha"]}


def _review_result(snapshot: dict[str, Any], thread_id: str, turn_id: str) -> str | None:
    thread = snapshot.get("thread") if isinstance(snapshot, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise AppServerProtocolError("review snapshot has the wrong thread")
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict) or turn.get("id") != turn_id:
            continue
        if turn.get("status") in {"failed", "interrupted"}:
            raise AppServerProtocolError("native review did not complete successfully")
        if turn.get("status") != "completed":
            return None
        for item in turn.get("items", []):
            if isinstance(item, dict) and item.get("type") == "exitedReviewMode":
                review = item.get("review")
                if isinstance(review, str) and review.strip():
                    return review
        raise AppServerProtocolError("native review completed without exitedReviewMode")
    return None


def _cleanup_turn_from_snapshot(
    snapshot: dict[str, Any], thread_id: str, requested_turn_id: str | None,
) -> tuple[str | None, bool]:
    """Return the known reviewer turn and whether it is terminal.

    A lost ``review/start`` response can leave the turn id unknown.  This is a
    dedicated fresh reviewer thread, so exactly one active turn is sufficient
    evidence for *cancellation only*; it is never evidence to replay a start.
    """
    thread = snapshot.get("thread") if isinstance(snapshot, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return None, False
    turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict) and isinstance(turn.get("id"), str)]
    active = [turn for turn in turns if turn.get("status") not in {"completed", "failed", "interrupted"}]
    # Native review can replace its initial turn during disconnect recovery.
    # This fresh reviewer thread owns no normal/source turns: cancel its sole
    # active review turn, not a stale initial id, and never pick among several.
    if len(active) == 1:
        return active[0]["id"], False
    if active:
        return None, False
    status = thread.get("status")
    if isinstance(status, dict) and status.get("type") == "active":
        return None, False
    if requested_turn_id is not None:
        turn = next((candidate for candidate in turns if candidate["id"] == requested_turn_id), None)
        if turn is None:
            return requested_turn_id, bool(turns)
        return requested_turn_id, turn.get("status") in {"completed", "failed", "interrupted"}
    if turns and not active:
        return None, True
    return None, False


async def _read_review_snapshot(client: Any, thread_id: str) -> dict[str, Any]:
    """Retry only reads of a new rollout whose metadata has not flushed yet."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return await client.read_thread(thread_id, include_turns=True)
        except AppServerRPCError as error:
            if not (
                error.code == -32603
                and "failed to read session metadata" in error.message
                and error.message.endswith(" is empty")
                and time.monotonic() < deadline
            ):
                raise
            await asyncio.sleep(.1)


async def _best_effort_review_cleanup(
    client: Any, client_factory: Callable[..., Any], thread_id: str, turn_id: str | None,
    *, cleanup_transferred: Callable[[], bool] | None = None,
    interrupt_attempted: Callable[[str], None] | None = None,
) -> tuple[bool, str | None]:
    """Cancel once; reconnecting only observes an uncertain delivery."""
    attempted = False
    async def read_and_interrupt(candidate: Any, known_turn: str | None) -> tuple[bool, str | None]:
        nonlocal attempted
        snapshot = await asyncio.wait_for(_read_review_snapshot(candidate, thread_id), timeout=2)
        known_turn, terminal = _cleanup_turn_from_snapshot(snapshot, thread_id, known_turn)
        if terminal:
            return True, known_turn
        if known_turn is None:
            return False, None
        if cleanup_transferred is not None and cleanup_transferred():
            return False, known_turn
        if not attempted:
            if interrupt_attempted is not None:
                interrupt_attempted(thread_id)
            attempted = True
            await asyncio.wait_for(candidate.interrupt_turn(thread_id, known_turn), timeout=2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            await asyncio.sleep(.2)
            snapshot = await asyncio.wait_for(_read_review_snapshot(candidate, thread_id), timeout=2)
            _, terminal = _cleanup_turn_from_snapshot(snapshot, thread_id, known_turn)
            if terminal:
                return True, known_turn
        return False, known_turn

    # The original client can occasionally still read and interrupt after the
    # request that exposed a disconnect.  If not, make one bounded, isolated
    # connection that is deliberately incapable of starting another review.
    try:
        return await read_and_interrupt(client, turn_id)
    except Exception:
        pass
    recovery = None
    try:
        recovery = client_factory(notification_handler=lambda *_args, **_kwargs: None)
        await asyncio.wait_for(recovery.connect(), timeout=3)
        return await read_and_interrupt(recovery, turn_id)
    except Exception:
        return False, turn_id
    finally:
        if recovery is not None:
            try:
                await asyncio.wait_for(recovery.close(), timeout=3)
            except Exception:
                pass


async def _repository_fingerprint_or_unverified(cwd: str) -> dict[str, Any]:
    """Keep local fingerprint budgets from blocking the native review itself."""
    try:
        return await asyncio.to_thread(repository_fingerprint, cwd)
    except Exception as error:
        return {"status": "unverified", "error": str(error) or type(error).__name__}


async def run_native_review(
    client_factory: Callable[..., Any], request: dict[str, Any], *,
    model: str | None = None, reasoning_effort: str | None = "high",
    thread_start_lock: asyncio.Lock | None = None,
    launch_guard: Callable[[], bool] | None = None,
    cleanup_unconfirmed: Callable[[str, str | None], None] | None = None,
    cleanup_confirmed: Callable[[], None] | None = None,
    lifecycle_update: Callable[[dict[str, str] | None], None] | None = None,
    cleanup_transferred: Callable[[], bool] | None = None,
    interrupt_attempted: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute exactly one native review; uncertain delivery is never replayed."""
    request = validate_review_request(request, request["cwd"])
    request["scope"] = await asyncio.to_thread(resolve_scope, request["cwd"], request["scope"])
    before = await _repository_fingerprint_or_unverified(request["cwd"])
    terminal_event = asyncio.Event()
    review_thread_id: str | None = None
    review_turn_id: str | None = None
    review_start_requested = False
    completed = False

    async def notification(message: dict[str, Any], **_kwargs: Any) -> None:
        nonlocal review_turn_id
        params = message.get("params") if isinstance(message, dict) else None
        if not isinstance(params, dict) or params.get("threadId") != review_thread_id:
            return
        turn = params.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            return
        # A turn/started notification may precede a lost review/start reply.
        # Capture only the new turn on this isolated reviewer thread.
        if message.get("method") == "turn/started" and review_turn_id is None:
            review_turn_id = turn["id"]
            if lifecycle_update is not None:
                lifecycle_update({
                    "phase": "turn_started", "review_thread_id": review_thread_id or "",
                    "review_turn_id": review_turn_id,
                })
        if turn.get("id") == review_turn_id and turn.get("status") in {"completed", "failed", "interrupted"}:
            terminal_event.set()

    client = client_factory(notification_handler=notification)
    try:
        await client.connect()
        selection = await client.resolve_model_selection(model, reasoning_effort, force_refresh=True)
        instructions = _review_instructions(request, before)
        if launch_guard is not None and not launch_guard():
            raise AppServerProtocolError("source turn ended before native review started")
        if thread_start_lock is None:
            thread = await client.start_thread(
                cwd=request["cwd"], model=selection["model"], sandbox="read-only",
                dynamic_tools=[], review_mode=True,
                review_instructions=instructions,
                review_reasoning_effort=selection["reasoning_effort"],
            )
        else:
            # Main runtime and reviewer share a server; serialize only
            # thread creation so an unsolicited thread/started cannot be
            # mistaken for a pending developer-thread creation.
            async with thread_start_lock:
                thread = await client.start_thread(
                    cwd=request["cwd"], model=selection["model"], sandbox="read-only",
                    dynamic_tools=[], review_mode=True,
                    review_instructions=instructions,
                    review_reasoning_effort=selection["reasoning_effort"],
                )
        review_thread_id = thread.get("id")
        if not isinstance(review_thread_id, str):
            raise AppServerProtocolError("review thread is invalid")
        if lifecycle_update is not None:
            lifecycle_update({"phase": "thread_created", "review_thread_id": review_thread_id})
        # Do not send a normal turn: review/start itself creates the native turn.
        if launch_guard is not None and not launch_guard():
            raise AppServerProtocolError("source turn ended before native review started")
        target = native_target(request["scope"], instructions=request["instructions"])
        if lifecycle_update is not None:
            lifecycle_update({"phase": "start_requested", "review_thread_id": review_thread_id})
        review_start_requested = True
        started = await client.start_review(review_thread_id, target)
        returned_turn_id = started["turn"]["id"]
        if review_turn_id is not None and review_turn_id != returned_turn_id:
            raise AppServerProtocolError("review notification and response disagree")
        review_turn_id = returned_turn_id
        if lifecycle_update is not None:
            lifecycle_update({
                "phase": "turn_started", "review_thread_id": review_thread_id,
                "review_turn_id": review_turn_id,
            })
        while True:
            snapshot = await _read_review_snapshot(client, review_thread_id)
            result_text = _review_result(snapshot, review_thread_id, review_turn_id)
            if result_text is not None:
                after = await _repository_fingerprint_or_unverified(request["cwd"])
                try:
                    after_scope = await asyncio.to_thread(
                        resolve_scope, request["cwd"],
                        {key: value for key, value in request["scope"].items() if key != "resolved_sha"},
                    )
                except AppServerProtocolError as error:
                    after_scope = None
                    scope_verification = {"status": "unverified", "error": str(error)}
                else:
                    scope_verification = {"status": "verified"}
                verified_fingerprints = "sha256" in before and "sha256" in after
                scope_changed: bool | None = None
                if verified_fingerprints and after_scope is not None:
                    scope_changed = before["sha256"] != after["sha256"] or request["scope"] != after_scope
                if not verified_fingerprints and scope_verification["status"] == "verified":
                    scope_verification = {
                        "status": "unverified",
                        "error": "review fingerprint was unavailable; repository drift is unknown",
                    }
                completed = True
                if lifecycle_update is not None:
                    lifecycle_update(None)
                return {
                    "success": True, "official_channel": "app_server_native_review",
                    "review_thread_id": review_thread_id, "review_turn_id": review_turn_id,
                    "configured_model": selection["model"],
                    "configured_reasoning_effort": selection["reasoning_effort"],
                    "result_text": result_text, "scope": request["scope"],
                    "request_target": target, "before": before, "after": after,
                    "scope_changed": scope_changed, "scope_verification": scope_verification,
                }
            try:
                await asyncio.wait_for(terminal_event.wait(), timeout=_POLL_SECONDS)
            except TimeoutError:
                pass
            terminal_event.clear()
    finally:
        cleanup_problem: str | None = None
        transferred = bool(cleanup_transferred is not None and cleanup_transferred())
        if not completed and review_thread_id and review_start_requested and not transferred:
            async def cleanup() -> tuple[bool, str | None]:
                try:
                    confirmed, known_turn = await asyncio.wait_for(
                        _best_effort_review_cleanup(
                            client, client_factory, review_thread_id, review_turn_id,
                            cleanup_transferred=cleanup_transferred,
                            interrupt_attempted=interrupt_attempted,
                        ), timeout=8,
                    )
                    if confirmed:
                        if lifecycle_update is not None:
                            lifecycle_update(None)
                        if cleanup_confirmed is not None:
                            cleanup_confirmed()
                    elif cleanup_unconfirmed is not None:
                        cleanup_unconfirmed(review_thread_id, known_turn or review_turn_id)
                    return confirmed, known_turn
                except Exception:
                    if cleanup_unconfirmed is not None:
                        cleanup_unconfirmed(review_thread_id, review_turn_id)
                    return False, review_turn_id
                finally:
                    try:
                        await asyncio.wait_for(client.close(), timeout=5)
                    except Exception:
                        pass

            cleanup_task = asyncio.create_task(cleanup(), name="native-review-cleanup")
            repeated_cancel = False
            while True:
                try:
                    confirmed, known_turn = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    # A second cancellation must not cut off the isolated
                    # cleanup task (notably during runtime shutdown).
                    # Persist a conservative reservation then keep waiting on
                    # the same bounded task; propagate cancellation afterward.
                    repeated_cancel = True
                    if cleanup_unconfirmed is not None:
                        cleanup_unconfirmed(review_thread_id, review_turn_id)
            review_turn_id = known_turn or review_turn_id
            if confirmed:
                # A cancellation can win the shield waiter just after cleanup
                # finished and conservatively re-register an orphan. Clear it
                # once more after observing the confirmed task result.
                if lifecycle_update is not None:
                    lifecycle_update(None)
                if cleanup_confirmed is not None:
                    cleanup_confirmed()
            # Do not replace a source-turn cancellation with a new exception,
            # but every other unconfirmed cleanup must remain visible.  The
            # IDs let Runtime retain its persistent lifecycle record.
            if not confirmed and not asyncio.current_task().cancelling():
                active = sys.exc_info()[1]
                prefix = str(active) if active is not None else "native code review did not complete"
                cleanup_problem = (
                    prefix + "; remote cancellation "
                    f"was not confirmed (orphan review_thread_id={review_thread_id}, "
                    f"review_turn_id={review_turn_id or 'unknown'})"
                )
            if repeated_cancel:
                raise asyncio.CancelledError
        elif completed or transferred or (review_thread_id is not None and not review_start_requested):
            if review_thread_id is not None and lifecycle_update is not None and not transferred:
                # A fresh reviewer thread alone has no paid native review turn.
                lifecycle_update(None)
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception:
                pass
        else:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception:
                pass
        if cleanup_problem is not None:
            raise ReviewCleanupUnconfirmed(cleanup_problem, review_thread_id, review_turn_id)
