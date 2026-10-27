"""Small, fail-closed helpers for deferred release state."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


RELEASE_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}$")
KNOWN_STATES = {"pending", "healthy", "failed"}
LOG_STATUS = re.compile(
    r"\brelease=([0-9]{8}-[0-9]{6})\s+status=([a-z_]+)\b"
)


def _release_directories(releases_dir: Path) -> dict[str, Path]:
    if not releases_dir.is_dir():
        return {}
    return {
        entry.name: entry
        for entry in releases_dir.iterdir()
        if entry.is_dir() and RELEASE_NAME.fullmatch(entry.name)
    }


def _legacy_log_states(log_path: Path) -> dict[str, str]:
    states: dict[str, str] = {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return states
    for line in lines:
        match = LOG_STATUS.search(line)
        if match:
            states[match.group(1)] = match.group(2)
    return states


def latest_healthy_release(releases_dir: Path, log_path: Path) -> str | None:
    """Return the newest verified snapshot, never merely the newest directory.

    New snapshots carry an authoritative ``status`` file. Older snapshots are
    admitted only when their last per-release publish-log status is healthy.
    """

    releases = _release_directories(Path(releases_dir))
    legacy_states = _legacy_log_states(Path(log_path))
    healthy: list[str] = []
    for name, directory in releases.items():
        status_path = directory / "status"
        if status_path.exists():
            try:
                status = status_path.read_text(encoding="ascii").strip()
            except OSError:
                continue
        else:
            status = legacy_states.get(name, "")
        if status == "healthy":
            healthy.append(name)
    return max(healthy) if healthy else None


def write_release_status(releases_dir: Path, release: str, status: str) -> Path:
    """Atomically persist one release's verification status."""

    if not RELEASE_NAME.fullmatch(str(release)):
        raise ValueError("invalid release name")
    if status not in KNOWN_STATES:
        raise ValueError("invalid release status")
    directory = Path(releases_dir) / release
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    destination = directory / "status"
    temporary = directory / f".status.{os.getpid()}.tmp"
    try:
        temporary.write_text(status + "\n", encoding="ascii")
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    latest = subparsers.add_parser("latest-healthy")
    latest.add_argument("--releases", type=Path, required=True)
    latest.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "latest-healthy":
        value = latest_healthy_release(args.releases, args.log)
        if value:
            print(value)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
