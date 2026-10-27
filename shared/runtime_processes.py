"""Linux supervisor-owned descendant barrier, including detached sessions.

This runs in a dedicated runtime supervisor, never in the request worker. A
subreaper retains orphaned jobs; pidfds prevent a recycled PID being signalled.
The barrier must succeed before a failed candidate can touch the fallback DB.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import time

_owner = None


def enable_subreaper() -> None:
    global _owner
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot retain orphan runtime children')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('runtime descendant barrier requires pidfds')
    _owner = os.getpid()


def _children(pid: int) -> set[int]:
    try:
        tasks = list((Path('/proc') / str(pid) / 'task').iterdir())
    except FileNotFoundError:
        return set()
    result = set()
    for task in tasks:
        try:
            result.update(map(int, (task / 'children').read_text().split()))
        except FileNotFoundError:
            continue  # exited thread, its children are reparented to subreaper
    return result


def _descendants() -> set[int]:
    pending = list(_children(os.getpid()))
    found = set()
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(_children(pid))
    return found


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def stop_descendants(grace: float = 20, kill_timeout: float = 10) -> None:
    if _owner != os.getpid():
        return
    handles = {}
    try:
        for signum, timeout in ((signal.SIGTERM, grace), (signal.SIGKILL, kill_timeout)):
            deadline = time.monotonic() + timeout
            while True:
                _reap()
                descendants = _descendants()
                if not descendants:
                    return
                for pid in descendants:
                    if pid not in handles:
                        try:
                            fd = os.pidfd_open(pid)
                        except ProcessLookupError:
                            continue
                        # Re-check ownership AFTER pinning the process. A PID
                        # recycled between enumeration and open is not ours.
                        if pid not in _descendants():
                            os.close(fd)
                            continue
                        handles[pid] = fd
                    try:
                        signal.pidfd_send_signal(handles[pid], signum)
                    except ProcessLookupError:
                        # A newly forked descendant may now reuse this number.
                        # The fd is still safely pinned to the exited ORIGINAL,
                        # but retaining it would strand the replacement writer.
                        os.close(handles.pop(pid))
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        _reap()
        if _descendants():
            raise RuntimeError('candidate descendants remain; refusing compensation')
    finally:
        for fd in handles.values():
            os.close(fd)
