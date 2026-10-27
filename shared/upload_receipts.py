"""Short-lived, server-side names for multi-file upload notices."""
import json
import os
import re
import secrets
import time
from pathlib import Path

from shared.config import UPLOADS

_RECEIPT_ID = re.compile(r"[0-9a-f]{24}\Z")
_RECEIPT_TTL = 24 * 60 * 60


def _receipt_path(batch_id, directory):
    directory = UPLOADS if directory is None else directory
    if not isinstance(batch_id, str) or not _RECEIPT_ID.fullmatch(batch_id):
        return None
    return Path(directory) / ("receipt-" + batch_id + ".json")


def create_upload_receipt(names, *, directory=None):
    """Persist a batch's display names under an opaque, short redirect id."""
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("上传回执格式无效。")
    directory = Path(UPLOADS if directory is None else directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    for _ in range(10):
        batch_id = secrets.token_hex(12)
        path = _receipt_path(batch_id, directory)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(names, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return batch_id
    raise ValueError("无法创建上传回执，请重试。")


def discard_upload_receipt(batch_id, *, directory=None):
    path = _receipt_path(batch_id, directory)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def load_upload_receipt(batch_id, *, directory=None):
    """Return a validated display-name list, or None for an unavailable receipt."""
    path = _receipt_path(batch_id, directory)
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > _RECEIPT_TTL:
            return None
        with path.open(encoding="utf-8") as source:
            names = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    return names
