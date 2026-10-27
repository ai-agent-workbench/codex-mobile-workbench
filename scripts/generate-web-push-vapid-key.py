#!/usr/bin/env python3
"""Create the initial Web Push VAPID P-256 key without printing key material."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    state = Path(
        os.environ.get("CODEX_WEB_STATE_DIR", str(Path.home() / ".local/share/codex-web"))
    )
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SystemExit("VAPID 密钥生成失败：状态目录权限不安全")

    target = state / "web-push-vapid-private.pem"
    if target.exists() or target.is_symlink():
        raise SystemExit("VAPID 密钥已存在；为避免轮换，未作改动")

    value = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    temporary = state / (".web-push-vapid-" + secrets.token_hex(12) + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while creating VAPID key")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(state, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise SystemExit("VAPID 密钥已存在；为避免轮换，未作改动") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    created = target.lstat()
    if (
        not stat.S_ISREG(created.st_mode)
        or created.st_uid != os.geteuid()
        or stat.S_IMODE(created.st_mode) != 0o600
        or created.st_nlink != 1
    ):
        raise SystemExit("VAPID 密钥生成失败：生成后的文件权限不安全")
    print("VAPID P-256 私钥已安全生成（密钥内容未输出）")


if __name__ == "__main__":
    main()
