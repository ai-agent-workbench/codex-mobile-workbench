"""File-system helpers: path validation, staging, copy/move primitives."""
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.config import (
    EDITS, FILE_ROOTS, MAX_EDIT_BYTES, MAX_UPLOAD_BYTES,
    SENSITIVE_PATHS, UPLOADS,
)


def sensitive_path(candidate):
    return any(
        candidate == root or root in candidate.parents
        for root in SENSITIVE_PATHS
    )


def sensitive_direct_child_names(directory):
    """Fast, request-local equivalent for non-link direct children only.

    A sensitive ancestor must still use the generic guard. A validated safe
    parent otherwise has sensitive children only where a root is its child.
    Nothing about permissions, entry type or link destinations is cached.
    """
    if sensitive_path(directory):
        return None
    return frozenset(root.name for root in SENSITIVE_PATHS if root.parent == directory)


def allowed_path(path, require_directory=False):
    candidate = Path(path).expanduser().resolve()
    if not any(candidate == root or root in candidate.parents for root in FILE_ROOTS):
        raise ValueError("该路径不在网页文件管理器的可见范围内。")
    if sensitive_path(candidate):
        raise ValueError("该路径已由网页文件管理器的误操作护栏隐藏。")
    if not candidate.exists():
        raise ValueError("路径不存在。")
    if require_directory and not candidate.is_dir():
        raise ValueError("该路径不是目录。")
    return candidate


def allowed_destination(path):
    """Validate the complete write target without resolving its final link."""
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    parent = allowed_path(candidate.parent, require_directory=True)
    if parent != candidate.parent:
        raise ValueError("目标目录已改变或包含符号链接，请重新确认。")
    if sensitive_path(candidate):
        raise ValueError("该路径已由网页文件管理器的误操作护栏隐藏。")
    if candidate.is_symlink():
        raise ValueError("不能覆盖符号链接，请选择普通文件或新名称。")
    if candidate.exists() and not (candidate.is_file() or candidate.is_dir()):
        raise ValueError("目标不是普通文件或目录。")
    return candidate


def staged_upload(token):
    path = UPLOADS / safe_upload_filename(token)
    if path.is_symlink() or not path.is_file():
        raise ValueError("上传暂存文件不存在或已过期。")
    return path


def safe_upload_filename(value):
    """Return a plain browser upload name, never a client-supplied path."""
    name = str(value or "").strip()
    if (not name or name in {".", ".."} or "\0" in name
            or "/" in name or "\\" in name):
        raise ValueError("上传文件名不能包含路径。")
    return name


def install_upload(staged, destination, *, expected=None):
    from shared.file_operations import install_staged
    install_staged(staged, destination, expected=expected)


def copy_file_compatible(source, destination, *, expected=None):
    from shared.file_operations import transfer
    transfer(source, destination, expected=expected)


def copy_directory_compatible(source, destination, *, expected=None):
    from shared.file_operations import transfer
    transfer(source, destination, expected=expected)


def staged_edit(token):
    path = EDITS / safe_upload_filename(token)
    if path.is_symlink() or not path.is_file():
        raise ValueError("编辑暂存内容不存在或已过期。")
    return path


def clean_staging():
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    for directory in (UPLOADS, EDITS):
        if not directory.exists():
            continue
        for child in directory.iterdir():
            if not child.is_file():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    child.unlink(missing_ok=True)
            except OSError:
                pass


def editable_text(path):
    if not path.is_file():
        raise ValueError("只能编辑普通文件。")
    if path.stat().st_size > MAX_EDIT_BYTES:
        raise ValueError("文件超过 1 MB 上限。")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        raise ValueError("该文件不是文本文件。")
