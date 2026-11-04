#!/usr/bin/env python3
"""Verify the CI-produced desktop ZIP before it is retained as an artifact."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import instance_package as package


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def expected_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("SHA-256 文件必须是普通文件。")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("SHA-256 文件必须恰好包含一行。")
    fields = lines[0].split(maxsplit=1)
    if not fields:
        raise ValueError("SHA-256 文件不能为空。")
    value = fields[0]
    if not SHA256.fullmatch(value):
        raise ValueError("SHA-256 文件格式无效。")
    return value


def validate_member(info: zipfile.ZipInfo, prefix: str, seen: set[str]) -> None:
    name = info.filename
    if name in seen:
        raise ValueError("ZIP 含有重复路径。")
    seen.add(name)
    path = PurePosixPath(name)
    if not name.startswith(prefix + "/") or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("ZIP 顶层前缀或路径无效。")
    mode = info.external_attr >> 16
    kind = mode & 0o170000
    if kind and kind not in {0o040000, 0o100000}:
        raise ValueError("ZIP 含有链接或特殊文件。")
    if info.is_dir() != name.endswith("/"):
        raise ValueError("ZIP 目录项格式无效。")


def verify_archive(archive: Path, sha256_file: Path, prefix: str, source_commit: str) -> dict:
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("ZIP 必须是普通文件。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix):
        raise ValueError("ZIP 顶层前缀无效。")
    actual = digest(archive)
    if actual != expected_digest(sha256_file):
        raise ValueError("ZIP SHA-256 不匹配。")
    with zipfile.ZipFile(archive) as source:
        seen: set[str] = set()
        for member in source.infolist():
            validate_member(member, prefix, seen)
        required = {f"{prefix}/package-source.json", f"{prefix}/{package.PANEL}"}
        if not required <= seen:
            raise ValueError("ZIP 缺少安装包清单或 Windows 面板。")
        with tempfile.TemporaryDirectory(prefix="ci-package-") as temporary:
            destination = Path(temporary)
            source.extractall(destination)
            value = package.verify(destination / prefix)
    if value["source_commit"] != source_commit:
        raise ValueError("安装包来源 commit 与当前 checkout 不一致。")
    return {"sha256": actual, "source_commit": value["source_commit"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256-file", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        result = verify_archive(args.archive, args.sha256_file, args.prefix, args.source_commit)
    except (OSError, ValueError, zipfile.BadZipFile, package.cfg.InstanceError) as error:
        print(f"CI 安装包校验失败：{error}", file=sys.stderr)
        return 1
    print(f"archive_sha256={result['sha256']}")
    print(f"source_commit={result['source_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
