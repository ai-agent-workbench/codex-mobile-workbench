#!/usr/bin/env python3
"""Run the selected offline Playwright UI check groups serially."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
GROUPS = {
    "core": ("workspace_mobile", "conversation_mobile", "conversation_progressive", "compact_mobile_controls",
             "light_dismiss", "diagnostics", "service_worker_diagnostics", "diagnostics_retry_backoff", "reconnect_coordinator", "pwa_startup", "diagnostic_report"),
    "history": ("history_live", "history_order", "history_search", "history_title_wrap",
                "conversation_history_paging"),
    "files": ("file_confirmation", "file_listing", "message_attachments", "project_upload_settings"),
    "settings": ("settings_center", "model_picker_scroll"),
    "native": ("conversation_async_questions", "conversation_questions", "native_interactions",
               "plan_context_review", "plan_goal_actions"),
    "security": ("request_origin", "admin_pwa"),
    "dot": ("dot_mobile", "dot_presentation", "dot_read_history"),
    "automation": ("automations",),
}
ALL_GROUPS = tuple(GROUPS)
OPENSSL_CHECKS = {"request_origin", "admin_pwa", "settings_center", "automations"}
NGINX_CHECKS = {"settings_center", "automations"}


def script_for(check: str) -> Path:
    suffix = ".js" if check in {"diagnostics_retry_backoff", "reconnect_coordinator"} else "_playwright.js"
    return Path("tests") / f"check_{check}{suffix}"


def plan(groups: list[str]) -> list[str]:
    selected: list[str] = []
    for group in groups:
        names = sum((GROUPS[name] for name in ALL_GROUPS), ()) if group == "all" else GROUPS[group]
        for name in names:
            if name not in selected:
                selected.append(name)
    return selected


def command_exists(command: str) -> bool:
    return Path(command).is_file() if os.sep in command else shutil.which(command) is not None


def check_command(command: str, *args: str, env: dict[str, str]) -> bool:
    try:
        return subprocess.run((command, *args), cwd=ROOT, env=env, check=False).returncode == 0
    except OSError:
        return False


def preflight(selected: list[str], env: dict[str, str]) -> bool:
    python = env.get("CODEX_WEB_PYTHON", sys.executable)
    node = env.get("CODEX_WEB_NODE", "node")
    missing = []
    if not command_exists(python):
        missing.append(f"Python 不可用：{python}")
    if not command_exists(node):
        missing.append(f"Node 不可用：{node}")
    if missing:
        print("\n".join(missing), file=sys.stderr)
        return False
    if not check_command(node, "-e", "require('playwright')", env=env):
        print("Playwright 不可用：Node 无法 require('playwright')", file=sys.stderr)
        return False
    needed = set(selected)
    if needed & OPENSSL_CHECKS and not check_command("openssl", "version", env=env):
        print("OpenSSL 不可用。", file=sys.stderr)
        return False
    need_nginx = bool(needed & NGINX_CHECKS)
    if env.get("CODEX_SETTINGS_GATEWAY") == "direct":
        need_nginx = bool(needed & {"automations"})
    if need_nginx and not check_command("nginx", "-v", env=env):
        print("Nginx 不可用。", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出分组及检查脚本")
    parser.add_argument("--dry-run", action="store_true", help="仅列出实际执行计划")
    parser.add_argument("groups", nargs="*", help="分组：" + "/".join((*ALL_GROUPS, "all")))
    args = parser.parse_args()
    if args.list and args.dry_run:
        parser.error("--list 与 --dry-run 不能同时使用")
    if args.list:
        for group, checks in GROUPS.items():
            for check in checks:
                print(f"{group} -> {script_for(check)}")
        return 0
    invalid = [group for group in args.groups if group not in (*ALL_GROUPS, "all")]
    if invalid:
        parser.error("未知分组：" + ", ".join(invalid))
    if not args.groups:
        parser.error("至少指定一个 UI 分组；使用 --list 查看可用分组")
    selected = plan(args.groups)
    if args.dry_run:
        for check in selected:
            print(script_for(check))
        return 0

    env = os.environ.copy()
    # Node resolution and browser locations are supplied by the installation or
    # CI environment; do not inherit a maintainer-specific tools directory.
    if not preflight(selected, env):
        return 1
    node = env.get("CODEX_WEB_NODE", "node")
    for check in selected:
        script = script_for(check)
        print(f"[ui] {script}", flush=True)
        result = subprocess.run((node, str(script)), cwd=ROOT, env=env, check=False)
        if result.returncode:
            print(f"UI 检查失败：{script}（退出码 {result.returncode}）", file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nUI 检查已中断。", file=sys.stderr)
        raise SystemExit(130)
