#!/usr/bin/env python3
"""Local administrator account maintenance; archived employee identities are inert.

Usage: accounts.py list | enable ADMIN | disable ADMIN
Employee creation, passwords, quotas and unlock operations have been retired.
This tool never removes historical account entries or their landing directories.
"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ.get("CODEX_WEB_STATE_DIR", str(Path.home() / ".local/share/codex-web")))
ACCOUNTS_FILE = STATE / "accounts.json"


def load():
    if not ACCOUNTS_FILE.exists():
        return {}
    value = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid account store")
    return value


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 0
    if args[0] not in {"list", "enable", "disable"}:
        print("员工账号创建、密码、配额及解锁功能已退役。", file=sys.stderr)
        return 2
    accounts = load()
    if args == ["list"]:
        for name, entry in accounts.items():
            status = ("enabled" if entry.get("enabled") else "disabled") if entry.get("role") == "admin" else "archived / login retired"
            print(f"{name}: {status}")
        return 0
    if len(args) != 2 or args[0] == "list":
        print(__doc__, file=sys.stderr)
        return 2
    entry = accounts.get(args[1], {})
    if entry.get("role") != "admin":
        print("仅允许维护现有管理员；历史员工账号保持归档不变。", file=sys.stderr)
        return 2
    entry["enabled"] = args[0] == "enable"
    target = ACCOUNTS_FILE.with_suffix(".json.new")
    target.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    target.chmod(0o600)
    target.replace(ACCOUNTS_FILE)
    print("管理员状态已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
