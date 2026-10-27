"""Transactional promotion helpers for verified runtime bundles.

This module only manipulates release metadata and the ``runtime-current``
symlink.  It deliberately has no service, database, backup, or lifecycle side
 effects.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from typing import Any, Iterator

from shared.runtime_bundle import BundleError, build_bundle, verify_bundle

_STAMP = re.compile(r"^\d{8}-\d{6}$")
_META = "runtime-release.json"
_PENDING = "pending-publish"
_CURRENT = "runtime-current"
_LOCK = ".runtime.lock"
_FORMAT = 1


class RuntimeReleaseError(ValueError):
    """The requested runtime release transition is not safe."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _no_links(path: Path, *, directory: bool = True) -> Path:
    path = _absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            item = os.lstat(current)
        except FileNotFoundError:
            raise RuntimeReleaseError(f"missing path: {path}")
        if stat.S_ISLNK(item.st_mode):
            raise RuntimeReleaseError(f"symbolic-link path is not allowed: {current}")
    if directory and not path.is_dir():
        raise RuntimeReleaseError(f"not a directory: {path}")
    return path


def _stamp(value: str) -> str:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise RuntimeReleaseError("invalid release stamp")
    return value


def _release(releases: Path, stamp: str, *, exists: bool = True) -> Path:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    path = releases / stamp
    if exists:
        _no_links(path)
    elif path.exists() or path.is_symlink():
        raise RuntimeReleaseError(f"release already exists: {stamp}")
    return path


def _regular(path: Path) -> None:
    try:
        item = os.lstat(path)
    except FileNotFoundError as exc:
        raise RuntimeReleaseError(f"missing file: {path}") from exc
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_uid != os.getuid():
        raise RuntimeReleaseError(f"not a regular file: {path}")


def _read_text(path: Path) -> str:
    _regular(path)
    return path.read_text(encoding="utf-8")


def _write_new(path: Path, content: str, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise RuntimeReleaseError(f"refusing to overwrite: {path}") from exc
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def _replace(path: Path, content: str, mode: int = 0o600) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _locked(releases: Path) -> Iterator[None]:
    releases = _no_links(releases)
    lock = releases / _LOCK
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RuntimeReleaseError("cannot open runtime lock") from exc
    try:
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        # Closing an fd releases flock even when a transition is interrupted.
        pass


def _current_stamp(releases: Path) -> str | None:
    current = releases / _CURRENT
    try:
        item = os.lstat(current)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(item.st_mode):
        raise RuntimeReleaseError("runtime-current is not a symbolic link")
    target = os.readlink(current)
    match = re.fullmatch(r"(\d{8}-\d{6})/runtime", target)
    if not match:
        raise RuntimeReleaseError("runtime-current target is not an exact relative runtime path")
    stamp = _stamp(match.group(1))
    runtime = _release(releases, stamp) / "runtime"
    _no_links(runtime)
    return stamp


def _swap_current(releases: Path, stamp: str) -> None:
    stamp = _stamp(stamp)
    runtime = _release(releases, stamp) / "runtime"
    _no_links(runtime)
    target = f"{stamp}/runtime"
    temporary = releases / f".{_CURRENT}.{uuid.uuid4().hex}"
    try:
        os.symlink(target, temporary)
        os.replace(temporary, releases / _CURRENT)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_meta(releases: Path, stamp: str) -> dict[str, Any]:
    release = _release(releases, stamp)
    try:
        value = json.loads(_read_text(release / _META))
    except json.JSONDecodeError as exc:
        raise RuntimeReleaseError("invalid runtime release metadata") from exc
    if not isinstance(value, dict) or set(value) != {"format_version", "release", "previous", "commit"}:
        raise RuntimeReleaseError("invalid runtime release metadata fields")
    if type(value["format_version"]) is not int or value["format_version"] != _FORMAT:
        raise RuntimeReleaseError("invalid runtime release metadata format")
    if (value["release"] != stamp or (value["previous"] is not None
            and (not isinstance(value["previous"], str) or not _STAMP.fullmatch(value["previous"])) )):
        raise RuntimeReleaseError("invalid runtime release metadata stamps")
    if not isinstance(value["commit"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", value["commit"]):
        raise RuntimeReleaseError("invalid runtime release commit")
    return value


def _status(releases: Path, stamp: str) -> str:
    result = _read_text(_release(releases, stamp) / "status")
    if result not in {"candidate\n", "pending\n", "healthy\n", "failed\n"}:
        raise RuntimeReleaseError("invalid runtime release status")
    return result.strip()


def _set_status(releases: Path, stamp: str, status: str) -> None:
    if status not in {"candidate", "pending", "healthy", "failed"}:
        raise RuntimeReleaseError("invalid runtime release status")
    _replace(_release(releases, stamp) / "status", status + "\n")


def _verify_release(releases: Path, stamp: str, *, trusted_base_interpreter: Path | None = None) -> dict[str, Any]:
    release = _release(releases, stamp)
    runtime = release / "runtime"
    _no_links(runtime)
    manifest = verify_bundle(runtime, **({'trusted_base_interpreter':trusted_base_interpreter} if trusted_base_interpreter is not None else {}))
    metadata = _read_meta(releases, stamp)
    if manifest.get("commit") != metadata["commit"]:
        raise RuntimeReleaseError("bundle commit and release metadata disagree")
    if _read_text(release / "version") != stamp + "\n":
        raise RuntimeReleaseError("release version does not match directory")
    return manifest


def _pending(releases: Path) -> tuple[str, str | None] | None:
    marker = releases / _PENDING
    if not marker.exists() and not marker.is_symlink():
        return None
    _regular(marker)
    if stat.S_IMODE(os.lstat(marker).st_mode) != 0o600:
        raise RuntimeReleaseError("runtime pending marker permissions are invalid")
    value = _read_text(marker)
    if not value.endswith("\n") or value.count("\n") != 2:
        raise RuntimeReleaseError("invalid runtime pending marker")
    stamp, previous, _ = value.split("\n")
    _stamp(stamp)
    if previous:
        _stamp(previous)
    return stamp, previous or None


def _pending_for(releases: Path, stamp: str, previous: str | None) -> None:
    pending = _pending(releases)
    if pending != (stamp, previous):
        raise RuntimeReleaseError("runtime pending marker does not match release")


def selected_runtime(releases: Path, verify: bool = True) -> Path:
    """Return the real selected bundle directory, optionally fully verified."""
    releases = _no_links(releases)
    stamp = _current_stamp(releases)
    if stamp is None:
        raise RuntimeReleaseError("no runtime release is selected")
    runtime = _release(releases, stamp) / "runtime"
    if verify:
        _verify_release(releases, stamp)
    return runtime.resolve()


def prepare_runtime_release(
    source: Path, snapshot: Path, previous: str | None, *, dependency_source: Path | None = None,
) -> dict[str, Any]:
    """Build a candidate into an already-created ``releases/<stamp>`` snapshot."""
    snapshot = _no_links(snapshot)
    releases = _no_links(snapshot.parent)
    stamp = _stamp(snapshot.name)
    if previous is not None:
        _stamp(previous)
    for name in ("runtime", _META, "version", "status"):
        if (snapshot / name).exists() or (snapshot / name).is_symlink():
            raise RuntimeReleaseError(f"snapshot already has {name}")
    build_arguments: dict[str, Path] = {}
    if dependency_source is not None:
        build_arguments["dependency_source"] = Path(dependency_source)
    manifest = build_bundle(Path(source), snapshot / "runtime", **build_arguments)
    payload = {"format_version": _FORMAT, "release": stamp, "previous": previous, "commit": manifest["commit"]}
    _write_new(snapshot / _META, json.dumps(payload, sort_keys=True) + "\n")
    _write_new(snapshot / "version", stamp + "\n")
    _write_new(snapshot / "status", "candidate\n")
    _verify_release(releases, stamp)
    return payload


def write_runtime_pending(releases: Path, stamp: str, previous: str | None) -> None:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    if previous is not None:
        _stamp(previous)
    if previous == stamp:
        raise RuntimeReleaseError("candidate cannot be its own fallback")
    with _locked(releases):
        if _pending(releases) is not None:
            raise RuntimeReleaseError("another runtime transaction is pending")
        if _current_stamp(releases) != previous:
            raise RuntimeReleaseError("selected runtime changed before pending marker")
        metadata = _read_meta(releases, stamp)
        if metadata["previous"] != previous or _status(releases, stamp) != "candidate":
            raise RuntimeReleaseError("candidate metadata or status is not eligible")
        _verify_release(releases, stamp)
        if previous is not None:
            if _status(releases, previous) != "healthy":
                raise RuntimeReleaseError("previous selected runtime is not healthy")
            _verify_release(releases, previous)
        _write_new(releases / _PENDING, f"{stamp}\n{previous or ''}\n")
        _set_status(releases, stamp, "pending")


def handoff_runtime(releases: Path, stamp: str) -> Path:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        metadata = _read_meta(releases, stamp)
        _pending_for(releases, stamp, metadata["previous"])
        if _status(releases, stamp) != "pending" or _current_stamp(releases) != metadata["previous"]:
            raise RuntimeReleaseError("runtime handoff preconditions do not hold")
        _verify_release(releases, stamp)
        if metadata["previous"] is not None:
            if _status(releases, metadata["previous"]) != "healthy":
                raise RuntimeReleaseError("previous selected runtime is not healthy")
            _verify_release(releases, metadata["previous"])
        _swap_current(releases, stamp)
        return selected_runtime(releases, verify=False)


def accept_runtime(releases: Path, stamp: str) -> None:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        metadata = _read_meta(releases, stamp)
        _pending_for(releases, stamp, metadata["previous"])
        if _current_stamp(releases) != stamp or _status(releases, stamp) not in {"pending", "healthy"}:
            raise RuntimeReleaseError("runtime acceptance preconditions do not hold")
        _verify_release(releases, stamp)
        _set_status(releases, stamp, "healthy")
        (releases / _PENDING).unlink()


def rollback_runtime(releases: Path, stamp: str) -> None:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        metadata = _read_meta(releases, stamp)
        previous = metadata["previous"]
        if previous is None:
            raise RuntimeReleaseError("runtime release has no fallback")
        _pending_for(releases, stamp, previous)
        current = _current_stamp(releases)
        failed = _status(releases, stamp)
        if current == previous and failed == "failed":
            if _status(releases, previous) != "healthy":
                raise RuntimeReleaseError("completed fallback is no longer healthy")
            _verify_release(releases, previous)
            (releases / _PENDING).unlink()
            return
        if current != stamp or failed not in {"pending", "failed", "healthy"}:
            raise RuntimeReleaseError("runtime rollback preconditions do not hold")
        if _status(releases, previous) != "healthy":
            raise RuntimeReleaseError("fallback runtime is not healthy")
        _verify_release(releases, previous)
        _set_status(releases, stamp, "failed")
        _swap_current(releases, previous)
        (releases / _PENDING).unlink()


def cancel_runtime_pending(releases: Path, stamp: str) -> None:
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        metadata = _read_meta(releases, stamp)
        _pending_for(releases, stamp, metadata["previous"])
        if _current_stamp(releases) != metadata["previous"]:
            raise RuntimeReleaseError("handoff already occurred; pending transaction cannot be cancelled")
        if _status(releases, stamp) not in {"pending", "candidate"}:
            raise RuntimeReleaseError("runtime release is not pending")
        (releases / _PENDING).unlink()
        _set_status(releases, stamp, "candidate")


def withdraw_initial_runtime(releases: Path, stamp: str) -> None:
    """Abandon a failed FIRST activation after the caller proves consumers stopped.

    No established healthy selection can be removed by this operation. Keep
    failed bytes/data for diagnosis; this is not a database rollback.
    """
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        metadata = _read_meta(releases, stamp)
        current = _current_stamp(releases)
        status = _status(releases, stamp)
        if metadata['previous'] is not None or current not in {stamp,None} or (current is None and status != 'failed'):
            raise RuntimeReleaseError('only an initial selected candidate can be withdrawn')
        _pending_for(releases, stamp, None)
        if _status(releases, stamp) not in {'pending', 'failed'}:
            raise RuntimeReleaseError('initial candidate is not pending')
        _verify_release(releases, stamp)
        _set_status(releases, stamp, 'failed')
        if current is not None:
            (releases / _CURRENT).unlink()
        (releases / _PENDING).unlink()


def select_healthy_runtime(releases: Path, stamp: str, *, trusted_base_interpreter: Path | None = None) -> Path:
    """Select an independently healthy runtime after a stopped-service manual fallback."""
    releases = _no_links(releases)
    stamp = _stamp(stamp)
    with _locked(releases):
        if _pending(releases) is not None:
            raise RuntimeReleaseError("cannot select while a runtime transaction is pending")
        if _status(releases, stamp) != "healthy":
            raise RuntimeReleaseError("runtime release is not healthy")
        _verify_release(releases, stamp, **({'trusted_base_interpreter':trusted_base_interpreter} if trusted_base_interpreter is not None else {}))
        _swap_current(releases, stamp)
        return selected_runtime(releases, verify=False)
