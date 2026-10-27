"""Validate conversation working directories without file-browser policy gates."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def conversation_cwd(value: Any) -> Path:
    """Return an accessible, existing directory for an App Server conversation."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("对话工作目录必须是非空绝对路径")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("对话工作目录必须是绝对路径")
    try:
        directory = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("对话工作目录不存在或不可访问") from exc
    if not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
        raise ValueError("对话工作目录必须是服务可读取和进入的目录")
    return directory
