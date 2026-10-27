"""Best-effort, privacy-bounded asyncio event-loop stall evidence.

This module deliberately observes only scheduling and Python code-object metadata.  It
never reads frames' locals, arguments, source lines, request bodies, or tool inputs.
It is not a watchdog that recovers the service: it does not interrupt, restart, retry,
or resend work.  A native extension which holds the GIL, or an entire-process freeze,
can prevent this daemon thread from running; such stalls cannot reliably be captured.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
import stat
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional


_MAX_FILE_BYTES = 256 * 1024
_MAX_RECORD_BYTES = 64 * 1024
_MAX_THREADS = 32
_MAX_FRAMES = 24
_SAFE_ERRORS = frozenset({"unsafe_path", "lock_busy", "record_too_large", "write_error", "start_error"})


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    """Return UTF-8-bounded text without inspecting values other than code metadata."""
    text = str(value)
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return data.decode("utf-8"), False
    # Keep only complete UTF-8 characters; the marker itself is intentionally short.
    marker = "…"
    kept = data[: max(0, limit - len(marker.encode("utf-8")))]
    return kept.decode("utf-8", "ignore") + marker, True


class _StallWriter:
    """A small dirfd-based JSONL writer.  It has no dependency on web hot paths."""

    def __init__(
        self,
        diagnostics_dir: os.PathLike[str] | str,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        max_record_bytes: int = _MAX_RECORD_BYTES,
    ) -> None:
        self._base = os.fspath(diagnostics_dir)
        self.max_file_bytes = max(1, min(int(max_file_bytes), _MAX_FILE_BYTES))
        self.max_record_bytes = max(1, min(int(max_record_bytes), _MAX_RECORD_BYTES, self.max_file_bytes))
        self.last_error: Optional[str] = None

    @staticmethod
    def _safe_stat(st: os.stat_result, *, directory: bool = False) -> bool:
        wanted = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        mode = 0o700 if directory else 0o600
        return (wanted and st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == mode
                and (directory or st.st_nlink == 1))

    def _open_directory(self) -> int:
        # Creating only this dedicated leaf avoids opening a caller-provided log path.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        base_fd = os.open(self._base, flags)
        try:
            if not self._safe_stat(os.fstat(base_fd), directory=True):
                raise PermissionError("unsafe_path")
            try:
                os.mkdir("web-stalls", 0o700, dir_fd=base_fd)
            except FileExistsError:
                pass
            st = os.stat("web-stalls", dir_fd=base_fd, follow_symlinks=False)
            if not self._safe_stat(st, directory=True):
                raise PermissionError("unsafe_path")
            result = os.open("web-stalls", flags, dir_fd=base_fd)
            if not self._safe_stat(os.fstat(result), directory=True):
                os.close(result)
                raise PermissionError("unsafe_path")
            return result
        finally:
            os.close(base_fd)

    def _open_regular(self, dir_fd: int, name: str) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        try:
            st = os.fstat(fd)
            if not self._safe_stat(st):
                raise PermissionError("unsafe_path")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _existing_safe(self, dir_fd: int, name: str) -> Optional[os.stat_result]:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not self._safe_stat(st):
            raise PermissionError("unsafe_path")
        return st

    def _rotate(self, dir_fd: int) -> None:
        # Refuse all non-regular or symlink targets before renaming any log generation.
        for name in ("stalls.jsonl", "stalls.jsonl.1", "stalls.jsonl.2"):
            self._existing_safe(dir_fd, name)
        if self._existing_safe(dir_fd, "stalls.jsonl.2") is not None:
            os.unlink("stalls.jsonl.2", dir_fd=dir_fd)
        if self._existing_safe(dir_fd, "stalls.jsonl.1") is not None:
            os.replace("stalls.jsonl.1", "stalls.jsonl.2", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        if self._existing_safe(dir_fd, "stalls.jsonl") is not None:
            os.replace("stalls.jsonl", "stalls.jsonl.1", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    def write(self, record: Mapping[str, Any]) -> bool:
        """Persist one complete JSONL record, returning False rather than raising."""
        try:
            raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError):
            self.last_error = "record_too_large"
            return False
        if len(raw) > self.max_record_bytes:
            self.last_error = "record_too_large"
            return False
        lock_fd = dir_fd = log_fd = old_size = None
        try:
            dir_fd = self._open_directory()
            # Validate an existing lock before O_NOFOLLOW opens it, so a link is
            # classified as unsafe rather than as an opaque open failure.
            self._existing_safe(dir_fd, "stalls.lock")
            lock_fd = self._open_regular(dir_fd, "stalls.lock")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.last_error = "lock_busy"
                return False
            current = self._existing_safe(dir_fd, "stalls.jsonl")
            if current is not None and current.st_size > self.max_file_bytes:
                self.last_error = "record_too_large"
                return False
            for generation in ("stalls.jsonl.1", "stalls.jsonl.2"):
                previous = self._existing_safe(dir_fd, generation)
                if previous is not None and previous.st_size > self.max_file_bytes:
                    self.last_error = "write_error"
                    return False
            if current is not None and current.st_size + len(raw) > self.max_file_bytes:
                self._rotate(dir_fd)
            log_fd = self._open_regular(dir_fd, "stalls.jsonl")
            old_size = os.fstat(log_fd).st_size
            if old_size + len(raw) > self.max_file_bytes:
                # A concurrent process cannot do this while it has honored stalls.lock.
                self.last_error = "record_too_large"
                return False
            os.lseek(log_fd, old_size, os.SEEK_SET)
            written = 0
            while written < len(raw):
                count = os.write(log_fd, raw[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "short write")
                written += count
            os.fsync(log_fd)
            self.last_error = None
            return True
        except (OSError, ValueError) as exc:
            # If a write failed part way through, do not leave a partial JSON record.
            if log_fd is not None and old_size is not None:
                try:
                    os.ftruncate(log_fd, old_size)
                except OSError:
                    pass
            unsafe = (isinstance(exc, PermissionError) and exc.args == ("unsafe_path",)) or (
                isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.ENOTDIR))
            self.last_error = "unsafe_path" if unsafe else "write_error"
            return False
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass


class WebStallMonitor:
    """Monitor an asyncio loop via an in-memory heartbeat and a daemon watcher.

    ``check_once`` is public chiefly for deterministic tests and does no scheduling or
    I/O unless it observes a stall or its recovery.  The loop callback only replaces a
    tuple; it neither waits for the writer nor acquires a shared lock.
    """

    def __init__(
        self,
        diagnostics_dir: os.PathLike[str] | str,
        *,
        heartbeat_seconds: float = 1.0,
        stall_seconds: float = 5.0,
        capture_interval_seconds: float = 10.0,
        cooldown_seconds: float = 300.0,
        release: str = "unknown",
        clock: Callable[[], float] = time.monotonic,
        frames_provider: Callable[[], Mapping[int, Any]] = sys._current_frames,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        writer: Optional[_StallWriter] = None,
    ) -> None:
        intervals = tuple(map(float, (heartbeat_seconds, stall_seconds, capture_interval_seconds, cooldown_seconds)))
        if (not all(math.isfinite(value) for value in intervals)
                or min(intervals[:3]) <= 0 or intervals[3] < 0):
            raise ValueError("monitor intervals must be positive (cooldown may be zero)")
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.stall_seconds = float(stall_seconds)
        self.capture_interval_seconds = float(capture_interval_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        # Release is an operator-supplied identifier, not an environment lookup.  Keep
        # it short and in a conservative alphabet before it can reach JSONL.
        release_text, _ = _bounded_text(release, 96)
        self.release = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in release_text) or "unknown"
        self._process_id = os.getpid()
        self._clock = clock
        self._frames_provider = frames_provider
        self._thread_factory = thread_factory
        self._writer = writer or _StallWriter(diagnostics_dir)
        now = self._clock()
        self._heartbeat_state: tuple[int, float] = (0, now)
        self._last_check = now
        self._stop_event = threading.Event()
        # Lifecycle calls may come from more than one startup/shutdown hook.  This
        # lock is never touched by the heartbeat callback or normal web request path.
        self._lifecycle_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._heartbeat_handle: Optional[asyncio.Handle] = None
        self._main_thread_id: Optional[int] = None
        self._episode_started: Optional[float] = None
        self._episode_gap = 0.0
        self._episode_heartbeat_seq = 0
        self._episode_heartbeat_at = now
        self._episode_captures = 0
        self._episode_suppressed = False
        self._episode_observation_gap = 0.0
        self._last_first_capture: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_gap_record: Optional[float] = None

    def status(self) -> dict[str, Any]:
        """Return non-sensitive in-memory state suitable for a startup status event."""
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "enabled": alive and not self._stop_event.is_set(),
            "thread_alive": alive,
            "active_episode": self._episode_started is not None,
            "last_error": self._last_error,
            "release": self.release,
            "threshold_seconds": self.stall_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "capture_interval_seconds": self.capture_interval_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "limits": {
                "max_threads": _MAX_THREADS,
                "max_frames_per_thread": _MAX_FRAMES,
                "max_record_bytes": getattr(self._writer, "max_record_bytes", _MAX_RECORD_BYTES),
                "max_file_bytes": getattr(self._writer, "max_file_bytes", _MAX_FILE_BYTES),
                "max_total_log_bytes": 3 * getattr(self._writer, "max_file_bytes", _MAX_FILE_BYTES),
            },
        }

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Start once; repeated calls are harmless and do not create another watcher."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._stop_event.is_set():
                return False
            self._loop = loop
            self._main_thread_id = threading.get_ident()
            try:
                loop.call_soon_threadsafe(self._install_heartbeat)
            except RuntimeError:
                self._stop_event.set()
                self._last_error = "start_error"
                return False
            try:
                thread = self._thread_factory(target=self._watch, name="web-stall-watcher", daemon=True)
                thread.start()
            except (RuntimeError, OSError):
                # A queued heartbeat must not survive a failed thread startup.
                self._stop_event.set()
                self._last_error = "start_error"
                return False
            self._thread = thread
            return True

    def stop(self) -> None:
        """Request shutdown without allowing a blocked diagnostic write to delay exit."""
        self._stop_event.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_heartbeat)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.25)

    def _install_heartbeat(self) -> None:
        if not self._stop_event.is_set():
            self._heartbeat()

    def _heartbeat(self) -> None:
        # Tuple replacement is the sole normal-path shared state update.
        if self._stop_event.is_set():
            return
        seq, _ = self._heartbeat_state
        self._heartbeat_state = (seq + 1, self._clock())
        loop = self._loop
        if loop is not None and not self._stop_event.is_set():
            try:
                self._heartbeat_handle = loop.call_later(self.heartbeat_seconds, self._heartbeat)
            except RuntimeError:
                self._last_error = "start_error"
                self._stop_event.set()

    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_handle is not None:
            self._heartbeat_handle.cancel()
            self._heartbeat_handle = None

    def _watch(self) -> None:
        while not self._stop_event.wait(min(max(self.heartbeat_seconds / 2.0, 0.01), 1.0)):
            self.check_once()

    def _stack_record(self, now: float, stale_for: float, observation_gap: float) -> dict[str, Any]:
        frames = self._frames_provider()
        ids: list[int] = []
        if self._main_thread_id is not None:
            ids.append(self._main_thread_id)
        watcher_id = threading.get_ident()
        # Do not enumerate an unbounded Thread list.  ``_current_frames`` is the
        # only useful population here and we inspect at most one extra identity to
        # make truncation explicit.
        for ident in frames:
            if ident != self._main_thread_id and ident != watcher_id and ident not in ids:
                ids.append(ident)
                if len(ids) > _MAX_THREADS:
                    break
        threads_truncated = len(ids) > _MAX_THREADS
        record_threads: list[dict[str, Any]] = []
        for ident in ids[:_MAX_THREADS]:
            frame = frames.get(ident)
            if frame is None:
                continue
            items: list[dict[str, Any]] = []
            frame_truncated = False
            while frame is not None and len(items) < _MAX_FRAMES:
                filename, cut_file = _bounded_text(frame.f_code.co_filename, 160)
                function, cut_name = _bounded_text(frame.f_code.co_name, 80)
                items.append({"file": filename, "function": function, "line": int(frame.f_lineno)})
                frame_truncated = frame_truncated or cut_file or cut_name
                frame = frame.f_back
            if frame is not None:
                frame_truncated = True
            thread_record: dict[str, Any] = {"thread_id": ident, "frames": items}
            if frame_truncated:
                thread_record["frames_truncated"] = True
            record_threads.append(thread_record)
        # _current_frames includes this watcher too. Drop frame references before
        # serializing so the snapshot does not retain live requests via a cycle.
        frame = None
        frames = None
        record: dict[str, Any] = {
            "event": "stall",
            "kind": "web_stall_snapshot",
            "process_id": self._process_id,
            "release": self.release,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "observed_at_monotonic": now,
            "stale_for_seconds": stale_for,
            "threads": record_threads,
        }
        if threads_truncated:
            record["threads_truncated"] = True
        if observation_gap >= self.stall_seconds:
            record["capture_delayed"] = True
            record["observation_gap_seconds"] = observation_gap
            record["stack_observation"] = "delayed"
        return self._fit_record(record)

    def _fit_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Bound a record independently of the writer's disk budget."""
        while True:
            raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
            if len(raw) <= _MAX_RECORD_BYTES:
                return record
            threads = record.get("threads", [])
            if len(threads) > 1:
                # Preserve the complete main-thread stack and discard trailing
                # threads in bounded steps, not hundreds of full re-encodings.
                threads.pop()
                record["threads_truncated"] = True
            elif threads and threads[0]["frames"]:
                threads[0]["frames"].pop()
                threads[0]["frames_truncated"] = True
            else:
                # This is unreachable with bounded fields, but remains a finite fail-safe.
                return {"event": "stall", "record_truncated": True}
            record["record_truncated"] = True

    def _write(self, record: Mapping[str, Any]) -> bool:
        # Attach only fixed process metadata; callers never supply request content.
        payload = dict(record)
        payload["process_id"] = self._process_id
        payload["release"] = self.release
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        payload = self._fit_record(payload)
        previous_error = self._last_error
        ok = self._writer.write(payload)
        if not ok:
            error = self._writer.last_error
            self._last_error = error if error in _SAFE_ERRORS else "write_error"
            if self._last_error != previous_error:
                # Journal visibility without touching the application's diagnostic
                # writer/locks or exposing paths, values, exception text or secrets.
                print("codex-web stall evidence unavailable: " + self._last_error,
                      file=sys.stderr, flush=True)
        else:
            self._last_error = None
        return ok

    def _finish_episode(self, now: float, heartbeat: tuple[int, float],
                        observation_gap: float, reason: str) -> bool:
        """A new heartbeat proves progress, not the exact time a stall ended."""
        heartbeat_seq, heartbeat_at = heartbeat
        if self._episode_started is None or heartbeat_seq == self._episode_heartbeat_seq:
            return False
        gap = max(self._episode_observation_gap, observation_gap)
        record = {
            "event": "recovery",
            "kind": "web_stall_recovered",
            "observed_at_monotonic": now,
            "episode_started_monotonic": self._episode_started,
            "last_heartbeat_before_stall_monotonic": self._episode_heartbeat_at,
            "latest_heartbeat_at_monotonic": heartbeat_at,
            "observed_stale_gap_seconds": self._episode_gap,
            # The latest heartbeat need not be the first one after recovery. This
            # span includes unobserved time and must never be called stall duration.
            "observation_span_seconds": max(0.0, now - self._episode_heartbeat_at),
            "span_is_upper_bound": True,
            "recovery_observation_delayed": gap >= self.stall_seconds or now - heartbeat_at >= self.stall_seconds,
            "recovery_observation": reason,
            "captures": self._episode_captures,
            "capture_suppressed": self._episode_suppressed,
            "observation_gap_seconds": gap,
        }
        # Reset even if evidence cannot be persisted. A subsequent stall belongs to
        # a new heartbeat and must not inherit this episode's two-capture ceiling.
        self._episode_started = None
        self._write(record)
        return True

    def check_once(self) -> None:
        """Perform one watcher observation; all failures are contained in this method."""
        try:
            now = self._clock()
            observation_gap = max(0.0, now - self._last_check)
            self._last_check = now
            heartbeat_seq, heartbeat_at = self._heartbeat_state
            finished_episode = self._finish_episode(
                now, (heartbeat_seq, heartbeat_at), observation_gap, "heartbeat_advanced")
            if finished_episode:
                # A write may release the GIL or block on storage. Re-observe before
                # deciding whether the latest heartbeat is already stale again.
                now = self._clock()
                observation_gap = max(observation_gap, max(0.0, now - self._last_check))
                self._last_check = now
                heartbeat_seq, heartbeat_at = self._heartbeat_state
            stale_for = max(0.0, now - heartbeat_at)
            if (observation_gap >= self.stall_seconds and stale_for < self.stall_seconds
                    and self._episode_started is None and not finished_episode
                    and (self._last_gap_record is None or now - self._last_gap_record >= self.cooldown_seconds)):
                # The loop may have resumed before this Python thread obtained
                # the GIL. Record the missing observation, never a fake past stack.
                self._last_gap_record = now
                self._write({"kind": "web_stall_observation_gap", "event": "observation_gap",
                             "observed_at_monotonic": now, "observation_gap_seconds": observation_gap,
                             "stack_captured": False})
            if stale_for < self.stall_seconds:
                return
            if self._episode_started is None:
                self._episode_started = now
                self._episode_gap = stale_for
                self._episode_heartbeat_seq = heartbeat_seq
                self._episode_heartbeat_at = heartbeat_at
                self._episode_captures = 0
                self._episode_suppressed = False
                self._episode_observation_gap = observation_gap
            else:
                self._episode_gap = max(self._episode_gap, stale_for)
                self._episode_observation_gap = max(self._episode_observation_gap, observation_gap)
            due = self._episode_captures == 0 or now - self._last_capture_at >= self.capture_interval_seconds
            if not due or self._episode_captures >= 2:
                return
            if self._episode_captures == 0 and self._last_first_capture is not None and now - self._last_first_capture < self.cooldown_seconds:
                self._episode_suppressed = True
                return

            # Revalidate immediately before collecting live frames: the watcher may
            # have been descheduled after its first heartbeat read. A recovered loop
            # must not acquire a fake snapshot of the earlier stall.
            now = self._clock()
            observation_gap = max(observation_gap, max(0.0, now - self._last_check))
            self._last_check = now
            heartbeat_seq, heartbeat_at = self._heartbeat_state
            if self._finish_episode(now, (heartbeat_seq, heartbeat_at), observation_gap,
                                    "heartbeat_advanced_before_capture"):
                return
            stale_for = max(0.0, now - heartbeat_at)
            if stale_for < self.stall_seconds:
                return
            self._episode_gap = max(self._episode_gap, stale_for)
            self._episode_observation_gap = max(self._episode_observation_gap, observation_gap)
            record = self._stack_record(now, stale_for, observation_gap)
            capture_finished = self._clock()
            heartbeat_after = self._heartbeat_state
            capture_duration = max(0.0, capture_finished - now)
            observation_gap = max(observation_gap, capture_duration)
            self._last_check = capture_finished
            self._episode_observation_gap = max(self._episode_observation_gap, observation_gap)
            unchanged = heartbeat_after[0] == heartbeat_seq
            record.update({
                "capture_finished_monotonic": capture_finished,
                "capture_duration_seconds": capture_duration,
                "heartbeat_sequence_before": heartbeat_seq,
                "heartbeat_sequence_after": heartbeat_after[0],
                "heartbeat_unchanged_during_capture": unchanged,
                "observation_gap_seconds": observation_gap,
                "stack_observation": "live_stale_heartbeat",
            })
            if not unchanged:
                # Live frames may straddle recovery. Keep them only as explicitly
                # uncertain context, never evidence of the earlier blocking site.
                record["capture_delayed"] = True
                record["stack_observation"] = "heartbeat_advanced_during_capture"
            elif observation_gap >= self.stall_seconds:
                record["capture_delayed"] = True
                record["stack_observation"] = "delayed_stale_heartbeat"
            self._episode_captures += 1
            self._last_capture_at = capture_finished
            if self._episode_captures == 1:
                self._last_first_capture = now
            self._write(record)
            self._finish_episode(capture_finished, heartbeat_after, observation_gap,
                                 "heartbeat_advanced_during_capture")
        except BaseException:
            # Diagnostic evidence must never fail the application's loop/watcher.
            self._last_error = "write_error"
