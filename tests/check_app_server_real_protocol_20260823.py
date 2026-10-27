"""Manual isolated App Server protocol probe; one tiny turn may incur model cost."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.codex_app_server import CodexAppServerClient
from shared.conversation_artifacts import publish_artifact_dynamic_tool


async def run(turn_count: int) -> dict:
    methods = []
    completed = asyncio.Event()
    settings_updates = asyncio.Event()
    settings_values = []
    terminal = None

    async def notification(message):
        nonlocal terminal
        method = message.get("method") if isinstance(message, dict) else None
        if isinstance(method, str):
            methods.append(method)
        if method == "thread/settings/updated":
            params = message.get("params") or {}
            thread_settings = params.get("threadSettings")
            if not isinstance(thread_settings, dict) or "serviceTier" not in thread_settings:
                raise RuntimeError("settings notification did not match current schema")
            settings_values.append(thread_settings.get("serviceTier"))
            settings_updates.set()
        if method == "turn/completed":
            terminal = message.get("params", {}).get("turn")
            completed.set()

    runtime_root = Path(f"/run/user/{os.getuid()}")
    with tempfile.TemporaryDirectory(
        prefix="codex-app-server-probe-", dir=runtime_root
    ) as root:
        directory = Path(root)
        socket = directory / "app-server.sock"
        process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--listen", f"unix://{socket}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=directory,
        )
        client = None
        thread_id = None
        deleted = False
        try:
            for _ in range(100):
                if socket.exists():
                    break
                if process.returncode is not None:
                    error = await process.stderr.read()
                    raise RuntimeError(
                        "isolated App Server exited before socket startup: "
                        + error.decode("utf-8", errors="replace")[-500:]
                    )
                await asyncio.sleep(0.05)
            if not socket.exists():
                raise RuntimeError("isolated App Server socket did not start")
            client = CodexAppServerClient(socket, notification_handler=notification)
            await client.connect()
            models = await client.list_all_models(force_refresh=True)
            requested_model = os.environ.get("CODEX_PROBE_MODEL")
            selected = next(
                item for item in models
                if (item.get("model") == requested_model if requested_model else item.get("isDefault"))
            )
            efforts = [
                item.get("reasoningEffort")
                for item in selected.get("supportedReasoningEfforts", [])
            ]
            effort = "low" if "low" in efforts else selected["defaultReasoningEffort"]
            tiers = [
                item.get("id") for item in selected.get("serviceTiers", [])
                if isinstance(item, dict)
            ]
            thread = await client.start_thread(
                cwd=str(directory),
                model=selected["model"],
                sandbox="read-only",
                dynamic_tools=[publish_artifact_dynamic_tool()],
            )
            thread_id = thread["id"]
            tier = "priority" if "priority" in tiers else None
            settings_updates.clear()
            await client.update_thread_settings(thread_id, tier)
            await asyncio.wait_for(settings_updates.wait(), timeout=10)
            if settings_values[-1] != tier:
                raise RuntimeError("settings notification did not confirm requested tier")
            before = await client.read_thread(thread_id, include_turns=False)
            if before["thread"].get("id") != thread_id:
                raise RuntimeError("isolated thread read returned another thread")

            turn_status = None
            final_present = None
            if turn_count:
                options = {
                    "effort": effort,
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {
                            "model": selected["model"],
                            "reasoning_effort": effort,
                            "developer_instructions": None,
                        },
                    },
                }
                if tier is not None:
                    options["serviceTier"] = tier
                statuses = []
                finals = []
                for index in range(turn_count):
                    completed.clear()
                    prompt = (
                        "Reply with exactly OK and no other text. Do not use tools."
                        if index == 0 else
                        "This is a follow-up. Reply with exactly SECOND and no other text. Do not use tools."
                    )
                    turn = await client.start_turn(thread_id, prompt, options=options)
                    await asyncio.wait_for(completed.wait(), timeout=180)
                    after = await client.read_thread(thread_id, include_turns=True)
                    persisted = next(
                        item for item in after["thread"]["turns"]
                        if item.get("id") == turn["id"]
                    )
                    turn_status = persisted.get("status")
                    final_present = any(
                        isinstance(item, dict)
                        and item.get("type") == "agentMessage"
                        and item.get("phase") == "final_answer"
                        and bool(str(item.get("text") or "").strip())
                        for item in persisted.get("items", [])
                    )
                    statuses.append(turn_status)
                    finals.append(final_present)
                    if turn_status != "completed" or not final_present:
                        raise RuntimeError("isolated turn did not settle with one final answer")
            await client.request("thread/delete", {"threadId": thread_id})
            deleted = True
            return {
                "with_turn": bool(turn_count),
                "turn_count": turn_count,
                "model": selected["id"],
                "effort": effort,
                "priority_available": "priority" in tiers,
                "settings_notification": "thread/settings/updated" in methods,
                "settings_notification_nested": settings_values[-1] == tier,
                "thread_started": "thread/started" in methods,
                "artifact_dynamic_tool": "publish_artifact",
                "native_collaboration_mode": "default" if turn_count else None,
                "agent_delta": "item/agentMessage/delta" in methods,
                "reasoning_summary_delta": (
                    "item/reasoning/summaryTextDelta" in methods
                ),
                "plan_updated": "turn/plan/updated" in methods,
                "command_output_delta": (
                    "item/commandExecution/outputDelta" in methods
                ),
                "turn_completed": "turn/completed" in methods,
                "turn_status": turn_status,
                "final_present": final_present,
                "test_thread_deleted": deleted,
            }
        finally:
            if thread_id and not deleted and client is not None and client.connected:
                try:
                    await client.request("thread/delete", {"threadId": thread_id})
                except Exception:
                    pass
            if client is not None:
                await client.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-turn", action="store_true",
        help="Run one tiny model turn; this may incur a small model charge.",
    )
    parser.add_argument(
        "--with-two-turns", action="store_true",
        help="Run two tiny turns on the same thread; this may incur a small model charge.",
    )
    args = parser.parse_args()
    if args.with_turn and args.with_two_turns:
        parser.error("choose only one turn mode")
    turn_count = 2 if args.with_two_turns else (1 if args.with_turn else 0)
    print(json.dumps(asyncio.run(run(turn_count)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
