#!/usr/bin/env python3
"""Read-only verification of pinned core Python distributions."""
from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
import sys

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)$")


class LockFileError(ValueError):
    """The lock is not an unambiguous list of exact distribution versions."""


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(lock_path: Path) -> list[tuple[str, str]]:
    """Read one exact ``name==version`` per line without executing input."""
    entries: list[tuple[str, str]] = []
    names: set[str] = set()
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LockFileError(f"无法读取锁文件 {lock_path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        match = _ENTRY.fullmatch(line)
        if not match:
            raise LockFileError(f"第 {line_number} 行不是精确的 name==version：{line!r}")
        name, version = match.groups()
        canonical_name = _canonical_name(name)
        if canonical_name in names:
            raise LockFileError(f"第 {line_number} 行的分发名重复：{name}")
        names.add(canonical_name)
        entries.append((name, version))

    if not entries:
        raise LockFileError("锁文件不能为空")
    return entries


def check_lock(lock_path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Report missing or mismatched distributions without importing the app."""
    missing: list[tuple[str, str]] = []
    mismatched: list[tuple[str, str, str]] = []
    for name, expected_version in read_lock(lock_path):
        try:
            installed_version = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append((name, expected_version))
        else:
            if installed_version != expected_version:
                mismatched.append((name, expected_version, installed_version))
    return missing, mismatched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查核心 Python 依赖")
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "requirements-core.lock",
        help="要检查的精确核心依赖锁文件",
    )
    args = parser.parse_args(argv)

    try:
        missing, mismatched = check_lock(args.lock)
    except LockFileError as exc:
        print(f"核心依赖锁文件无效：{exc}", file=sys.stderr)
        return 2

    if not missing and not mismatched:
        print("核心依赖检查通过。")
        return 0

    print("核心依赖检查失败：", file=sys.stderr)
    for name, expected_version in missing:
        print(f"- 缺少：{name}=={expected_version}", file=sys.stderr)
    for name, expected_version, installed_version in mismatched:
        print(
            f"- 版本不符：{name} 需要 {expected_version}，当前 {installed_version}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
