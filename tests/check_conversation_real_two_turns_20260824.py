"""Isolated real-runtime gate for completed first turn -> second browser command."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.codex_app_server import CodexAppServerClient
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService


async def wait_idle(store: ConversationStore, expected_turns: int) -> dict:
    for _ in range(720):
        value = store.snapshot("real-two-turns")
        conversation = value["conversation"]
        if conversation["state"] == "needs_user":
            raise RuntimeError(
                "real conversation requires recovery: "
                + str(conversation.get("recovery_state"))
            )
        completed = [turn for turn in value["turns"] if turn["status"] == "completed"]
        if conversation["state"] == "idle" and len(completed) == expected_turns:
            return value
        await asyncio.sleep(0.25)
    raise RuntimeError(f"real conversation did not settle turn {expected_turns}")


async def run() -> dict:
    runtime_root = Path(f"/run/user/{os.getuid()}")
    with tempfile.TemporaryDirectory(
        prefix="codex-conversation-two-turns-", dir=runtime_root
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        socket = root / "app-server.sock"
        process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--listen", f"unix://{socket}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
        )
        runtime = None
        store = None
        thread_id = None
        deleted = False
        try:
            for _ in range(100):
                if socket.exists():
                    break
                if process.returncode is not None:
                    error = await process.stderr.read()
                    raise RuntimeError(error.decode("utf-8", errors="replace")[-500:])
                await asyncio.sleep(0.05)
            if not socket.exists():
                raise RuntimeError("isolated App Server socket did not start")

            probe = CodexAppServerClient(socket)
            await probe.connect()
            models = await probe.list_all_models(force_refresh=True)
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
            await probe.close()

            store = ConversationStore(root / "state" / "conversations.sqlite3")
            artifacts = ConversationArtifactService(store, root / "artifacts")
            runtime = ConversationRuntime(
                store,
                artifacts,
                lambda **handlers: CodexAppServerClient(socket, **handlers),
            )
            service = ConversationService(store, artifacts, runtime)
            store.create_conversation(
                "real-two-turns",
                title="real two turn gate",
                cwd=str(workspace),
                settings={
                    "model": selected["model"],
                    "effort": effort,
                    "permission": "read-only",
                    "service_tier": "priority" if "priority" in tiers else None,
                },
            )

            first = await service.start(
                "real-two-turns",
                command_id="browser-message-one",
                text="Reply with exactly FIRST and no other text. Do not use tools.",
            )
            if first["command"]["state"] not in {"accepted", "completed"}:
                raise RuntimeError("first browser command was not accepted")
            await wait_idle(store, 1)

            second = await service.send(
                "real-two-turns",
                command_id="browser-message-two",
                text="Reply with exactly SECOND and no other text. Do not use tools.",
            )
            if second["command"]["state"] not in {"accepted", "completed"}:
                raise RuntimeError("second browser command was not accepted")
            settled = await wait_idle(store, 2)
            command_ids = [
                command["id"]
                for turn in settled["turns"]
                for command in turn.get("commands", [])
            ]
            if command_ids != ["browser-message-one", "browser-message-two"]:
                raise RuntimeError("browser commands did not persist in turn order")
            if not all(turn.get("final_text") for turn in settled["turns"]):
                raise RuntimeError("one of the real turns has no final answer")

            thread_id = settled["conversation"]["thread_id"]
            await runtime._ensure_client()
            await runtime._client.request("thread/delete", {"threadId": thread_id})
            deleted = True
            return {
                "ok": True,
                "turns": len(settled["turns"]),
                "command_ids": command_ids,
                "state": settled["conversation"]["state"],
                "model": selected["id"],
                "effort": effort,
                "test_thread_deleted": True,
            }
        finally:
            if thread_id and not deleted and runtime is not None:
                try:
                    await runtime._ensure_client()
                    await runtime._client.request(
                        "thread/delete", {"threadId": thread_id}
                    )
                except Exception:
                    pass
            if runtime is not None:
                await runtime.close()
            if store is not None:
                store.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
