"""Private, user-initiated browser fault-report storage.

This intentionally does not reuse browser uploads or conversation attachments.  A
report is a small, bounded receipt plus (optionally) one image selected by the
user; it is not a browser crash collector.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shared.config import STATE
from shared.diagnostics import current_release, redact, request_id_var, utc_ms

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + 64 * 1024
MAX_REPORT_BYTES = 128 * 1024 * 1024
MAX_REPORTS = 100
_ID = re.compile(r"^[a-f0-9]{32}$")
_SMALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PHASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ALLOWED_CATEGORIES = {"open_failed", "page_crashed", "other"}
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


class ReportError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def reports_dir() -> Path:
    """The already-controlled diagnostics root, with a private reports leaf."""
    root = Path(os.environ.get("CODEX_WEB_DIAGNOSTICS_DIR", str(STATE / "diagnostics")))
    return root / "reports"


def new_report_id() -> str:
    return uuid.uuid4().hex


def validate_report_id(value: Any) -> str:
    if value in (None, ""):
        return new_report_id()
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ReportError("invalid_report_id")
    return value


def parse_fault_time(value: Any, *, now: dt.datetime | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ReportError("invalid_fault_time")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        # The no-script form uses datetime-local and explicitly labels it UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        parsed = parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ReportError("invalid_fault_time") from exc
    now = now or dt.datetime.now(dt.timezone.utc)
    if parsed < now - dt.timedelta(days=366) or parsed > now + dt.timedelta(minutes=10):
        raise ReportError("fault_time_out_of_range")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _one(value: Any, name: str, *, limit: int, pattern: re.Pattern[str] | None = None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > limit or (pattern and not pattern.fullmatch(value)):
        raise ReportError("invalid_" + name)
    return value


def _optional_client_time(value: Any) -> str | None:
    """A device clock is evidence, not a gate on an otherwise valid report."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def validate_fields(values: dict[str, Any]) -> dict[str, Any]:
    category = values.get("category")
    if category not in _ALLOWED_CATEGORIES:
        raise ReportError("invalid_category")
    source_path = _one(values.get("source_path"), "source_path", limit=512)
    if source_path:
        # Preserve only a same-site path. Query and fragment can contain entry
        # tokens, and are intentionally discarded before any persistence.
        parsed = urlsplit(source_path)
        if (not source_path.startswith("/") or parsed.scheme or parsed.netloc or
                "\\" in parsed.path or "\x00" in parsed.path):
            raise ReportError("invalid_source_path")
        source_path = parsed.path
    result = {
        "category": category,
        "fault_time": parse_fault_time(values.get("fault_time")),
        "client_page_id": _one(values.get("client_page_id"), "client_page_id", limit=160, pattern=_SMALL_ID),
        "source_path": redact(source_path, limit=512) if source_path else None,
        "last_phase": _one(values.get("last_phase"), "last_phase", limit=64, pattern=_PHASE),
        "client_time": _optional_client_time(values.get("client_time")),
        "client_version": _one(values.get("client_version"), "client_version", limit=160, pattern=_SMALL_ID),
    }
    return result


def image_info(content_type: Any, data: bytes | None) -> dict[str, Any]:
    if data is None:
        return {"present": False, "bytes": 0, "sha256": None, "media_type": None, "extension": None}
    if len(data) > MAX_IMAGE_BYTES:
        raise ReportError("image_too_large", 413)
    if content_type == "image/png" and data.startswith(_PNG):
        media_type, extension = "image/png", "png"
    elif content_type == "image/jpeg" and data.startswith(_JPEG):
        media_type, extension = "image/jpeg", "jpg"
    else:
        raise ReportError("invalid_image")
    return {"present": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": media_type, "extension": extension}


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _private_dir(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReportError("report_storage_unavailable", 503)
    if info.st_uid != os.geteuid() or (stat.S_IMODE(info.st_mode) & 0o077):
        raise ReportError("report_storage_unavailable", 503)


def _private_file(path: Path) -> os.stat_result:
    info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReportError("report_storage_unavailable", 503)
    if info.st_uid != os.geteuid() or (stat.S_IMODE(info.st_mode) & 0o077):
        raise ReportError("report_storage_unavailable", 503)
    return info


def _write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise OSError("short report write")
            offset += count
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _report_bytes(path: Path) -> int:
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                    info.st_uid != os.geteuid() or (stat.S_IMODE(info.st_mode) & 0o077)):
                raise ReportError("report_storage_unavailable", 503)
            total += info.st_size
    return total


def _usage(root: Path) -> tuple[int, int]:
    total = count = 0
    with os.scandir(root) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if entry.name == ".lock":
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ReportError("report_storage_unavailable", 503)
                total += info.st_size
                continue
            if entry.name.startswith(".tmp-"):
                # Failed calls only clean their own temporary directory.  Count
                # stale bytes conservatively but never remove or traverse them.
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ReportError("report_storage_unavailable", 503)
                total += _report_bytes(Path(entry.path))
                continue
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or not _ID.fullmatch(entry.name):
                raise ReportError("report_storage_unavailable", 503)
            _private_dir(Path(entry.path))
            total += _report_bytes(Path(entry.path)); count += 1
    return total, count


def _load_existing(directory: Path) -> dict[str, Any]:
    _private_dir(directory)
    # A receipt is never returned from a directory containing a link or a file
    # whose ownership/mode no longer meets the private-store contract.
    _report_bytes(directory)
    path = directory / "metadata.json"
    info = _private_file(path)
    if info.st_size > 16 * 1024:
        raise ReportError("report_storage_unavailable", 503)
    try:
        with path.open("rb") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError("report_storage_unavailable", 503) from exc
    if not isinstance(value, dict):
        raise ReportError("report_storage_unavailable", 503)
    return value


def _remove_own_stage(path: Path) -> None:
    """Only remove known regular files in this invocation's unique stage."""
    try:
        info = _lstat(path)
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return
        for name in ("metadata.json", "screenshot.png", "screenshot.jpg"):
            child = path / name
            child_info = _lstat(child)
            if child_info and stat.S_ISREG(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
                os.unlink(child)
        os.rmdir(path)
    except OSError:
        pass


def _fingerprint(owner: str, values: dict[str, Any], image: dict[str, Any]) -> str:
    body = {"owner": owner, "fields": values, "image": {k: image[k] for k in ("bytes", "sha256", "media_type")}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def persist_report(report_id: str, owner: str, values: dict[str, Any], screenshot: bytes | None,
                   image: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Write a report transactionally.  Called in a worker thread only."""
    root = reports_dir()
    lock_fd: int | None = None
    stage: Path | None = None
    try:
        import fcntl
        _private_dir(root.parent, create=True)
        _private_dir(root, create=True)
        lock_path = root / ".lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(lock_path, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        _private_file(lock_path)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        final = root / report_id
        if _lstat(final) is not None:
            existing = _load_existing(final)
            if existing.get("owner") != owner or existing.get("content_fingerprint") != _fingerprint(owner, values, image):
                raise ReportError("report_id_conflict", 409)
            receipt = existing.get("receipt")
            if not isinstance(receipt, dict):
                receipt = {"report_id": report_id, "saved": True, "diagnostics_logged": False}
            _fsync_dir(root)
            return receipt, existing, True
        used, count = _usage(root)
        expected = len(screenshot or b"") + 4096
        if count >= MAX_REPORTS or used + expected > MAX_REPORT_BYTES:
            raise ReportError("report_capacity_exhausted", 507)
        stage = root / (".tmp-" + report_id + "-" + uuid.uuid4().hex)
        os.mkdir(stage, 0o700)
        _private_dir(stage)
        fingerprint = _fingerprint(owner, values, image)
        metadata = {
            "schema": "browser-fault-report-v1", "report_id": report_id, "owner": owner,
            "created_at": utc_ms(), "fault_time": values["fault_time"], "category": values["category"],
            "client_page_id": values["client_page_id"], "source_path": values["source_path"],
            "last_phase": values["last_phase"], "client_time": values["client_time"],
            "client_version": values["client_version"], "release": redact(current_release(), limit=160),
            "request_id": request_id_var.get(), "screenshot": {k: image[k] for k in ("present", "bytes", "sha256", "media_type")},
            "content_fingerprint": fingerprint,
            "receipt": {"report_id": report_id, "saved": True, "diagnostics_logged": False},
        }
        if screenshot is not None:
            _write_private(stage / ("screenshot." + image["extension"]), screenshot)
        _write_private(stage / "metadata.json", json.dumps(redact(metadata), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        _fsync_dir(stage)
        os.rename(stage, final)
        _fsync_dir(root)
        return metadata["receipt"], metadata, False
    except ReportError:
        if stage is not None:
            _remove_own_stage(stage)
        raise
    except Exception as exc:
        if stage is not None:
            _remove_own_stage(stage)
        raise ReportError("report_storage_unavailable", 503) from exc
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def update_log_receipt(report_id: str, logged: bool) -> dict[str, Any]:
    """Persist the original receipt after the independent diagnostic emit attempt."""
    root = reports_dir(); final = root / report_id
    _private_dir(root); metadata = _load_existing(final)
    receipt = {"report_id": report_id, "saved": True, "diagnostics_logged": bool(logged)}
    metadata["receipt"] = receipt
    temporary = final / ".metadata-new"
    _write_private(temporary, json.dumps(redact(metadata), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    os.replace(temporary, final / "metadata.json")
    _fsync_dir(final)
    return receipt
