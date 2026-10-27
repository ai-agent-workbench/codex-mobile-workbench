"""Pinned, standard-library supervisor for immutable Web runtime consumers.

Installed together with runtime_bundle/release helpers, independently of the
selected application.  Thus an application import failure cannot disable the
publish compensation path.  Paths are resolved once before each child starts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request

from shared.runtime_release import (
    RuntimeReleaseError, _pending, _read_meta, _status, accept_runtime,
    rollback_runtime, selected_runtime,
)
from shared.runtime_processes import enable_subreaper, stop_descendants

ROLES = {"web"}


def child_environment(bundle: Path, state: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        env.pop(key, None)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "CODEX_WEB_PYTHON": str(bundle / ".venv/bin/python"),
        "CODEX_WEB_STATE_DIR": str(state), "CODEX_WEB_RUNTIME_SUPERVISED": "1",
    })
    return env


def command_for(bundle: Path, role: str, *, port: int = 18080) -> list[str]:
    if role not in ROLES or not 1 <= port <= 65535:
        raise RuntimeReleaseError("invalid runtime consumer or port")
    app = bundle / "app"
    python = str(bundle / ".venv/bin/python")
    if role == "web":
        # Legacy healthy fallbacks predate the runtime env guard. Suppress
        # only their old publish hook in memory; never patch immutable bytes.
        code = "import sys;sys.path.insert(0,sys.argv[1]);import app;app._verify_pending_publish=lambda:None;import uvicorn;uvicorn.run(app.app,host='127.0.0.1',port=int(sys.argv[2]),proxy_headers=True)"
        return [python, "-B", "-I", "-c", code, str(app), str(port)]


def _log(releases: Path, stamp: str, status: str, detail: str = "") -> None:
    try:
        with (releases / "publish.log").open("a", encoding="utf-8") as output:
            output.write(f"{datetime.now(timezone.utc).isoformat()} release={stamp} status={status} {detail}\n")
    except OSError:
        print(f"runtime publish log unavailable: release={stamp} status={status}", file=sys.stderr)


def stop_child(child: subprocess.Popen | None) -> None:
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
    # The main process exiting does not prove jobs (setsid/double fork) stopped.
    stop_descendants()


def _quarantine(bundle: Path, previous: Path, state: Path) -> None:
    from shared.runtime_quarantine import quarantine
    quarantine(previous, state / 'conversations.sqlite3')


def compensate(releases: Path, bundle: Path, state: Path) -> None:
    stamp = bundle.parent.name
    metadata = _read_meta(releases, stamp)
    previous = metadata["previous"]
    if previous is None:
        raise RuntimeReleaseError("missing healthy runtime fallback")
    # Verify the fallback before any DB barrier is considered.
    from shared.runtime_release import _verify_release
    _verify_release(releases, previous)
    _quarantine(bundle, releases / previous / "runtime", state)
    rollback_runtime(releases, stamp)
    _log(releases, stamp, "rolled_back", f"previous={previous} verify=runtime-supervisor")


def wait_ready(child: subprocess.Popen, port: int, timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def smoke(bundle: Path, state: Path, port: int) -> bool:
    result = subprocess.run(
        ["bash", str(bundle / "app/smoke_test.sh"), f"http://127.0.0.1:{port}"],
        cwd=bundle / "app", env=child_environment(bundle, state), timeout=180,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Do not persist raw output, cookies, environment or exception payloads.
    return result.returncode == 0


def run_consumer(state: Path, role: str, *, port: int = 18080) -> int:
    enable_subreaper()
    releases = state / "releases"
    stopping = False
    child = None

    def stop(signum, frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        stop_child(child)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            bundle = selected_runtime(releases, verify=False)
            stamp = bundle.parent.name
            pending = _pending(releases)
            if role == "web" and pending is not None and pending[0] != stamp:
                # Complete a previously interrupted rollback only when the
                # pointer is already on its exact healthy previous release.
                if _status(releases, pending[0]) == "failed" and pending[1] == stamp:
                    rollback_runtime(releases, pending[0])
                    pending = None
            owned_pending = role == "web" and pending is not None and pending[0] == stamp
            try:
                selected_runtime(releases, verify=True)
                if owned_pending and pending[1] != _read_meta(releases, stamp)["previous"]:
                    raise RuntimeReleaseError("pending marker disagrees with selected runtime metadata")
                if _status(releases, stamp) not in {"pending", "healthy"}:
                    raise RuntimeReleaseError("selected runtime is not eligible to run")
                child = subprocess.Popen(command_for(bundle, role, port=port),
                                         cwd=bundle / "app", env=child_environment(bundle, state))
                if owned_pending:
                    if not wait_ready(child, port) or not smoke(bundle, state, port):
                        raise RuntimeReleaseError("candidate startup or smoke failed")
                    if stopping or child.poll() is not None:
                        raise RuntimeReleaseError("candidate stopped during verification")
                    accept_runtime(releases, stamp)
                    _log(releases, stamp, "healthy", "verify=startup-hook runtime=supervisor")
                    _log(releases, stamp, "lifecycle", "awaiting_owner_acceptance")
                return child.wait()
            except Exception as error:
                stop_child(child)
                if stopping:
                    return 0
                if owned_pending:
                    _log(releases, stamp, "smoke_failed", f"error_type={type(error).__name__}")
                    compensate(releases, bundle, state)
                    continue
                raise
        return 0
    finally:
        stop_child(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args()
    return run_consumer(args.state.absolute(), args.role, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
