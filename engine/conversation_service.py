"""Application service for the App Server-only workbench.

Routes talk to this service instead of constructing protocol clients or
touching persistence directly.  The service also defines the small public
snapshot returned to the browser; native paths and managed-storage details
never cross that boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from engine.codex_app_server import CodexAppServerClient, AppServerProtocolError
from engine.conversation_automation import AutomationStore, AutomationScheduler
from shared.automation_tools import tool_response
from engine.conversation_runtime import (
    ConversationRuntime,
    validate_conversation_settings,
    has_pending_native_delete,
)
from engine.conversation_store import ConversationStore
from engine.conversation_plan import plan_confirmation_candidate
from engine.conversation_search import (
    SearchQuery, agent_message_text_key, normalize_search_query,
    realtime_agent_message, thread_read_agent_copy,
)
from shared.config import (
    AUTOMATIONS_ENABLED,
    COMPLETION_NOTIFICATIONS_ENABLED,
    CODEX_APP_SERVER_SOCKET,
    CONVERSATION_ARTIFACTS,
    CONVERSATION_DB,
    CONVERSATION_WORKSPACES,
    NEW_CONVERSATION_MODEL,
    NEW_CONVERSATION_PERMISSION,
    NEW_CONVERSATION_REASONING,
)
from shared.conversation_artifacts import ConversationArtifactService
from shared.message_attachments import MessageAttachmentService, public_attachment
from shared.conversation_export import export_markdown as format_conversation_export
from shared.conversation_pagination import (
    PAGE_SIZE, ConversationPageSnapshots,
)
from shared.conversation_paths import conversation_cwd
from shared.notification_format import format_completion_notification


ACTIVE_STATES = {
    "starting_thread",
    "starting_turn",
    "active",
    "interrupting",
    "reconciling",
}

class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        artifacts: ConversationArtifactService,
        runtime: ConversationRuntime,
        *, enable_automations: bool = False,
        enable_completion_notifications: bool = True,
    ):
        self.store = store
        self.artifacts = artifacts
        self.runtime = runtime
        self._pages = ConversationPageSnapshots()
        self._automations = None
        self._scheduler = None
        self._enable_automations = enable_automations
        self._enable_completion_notifications = enable_completion_notifications
        self._attachments = None
        runtime.automation_tool_handler = self._automation_tool
        runtime.scheduled_guard = self._scheduled_guard

    @property
    def attachments(self) -> MessageAttachmentService:
        if self._attachments is None:
            self._attachments = (self.runtime.message_attachments
                if isinstance(self.runtime, ConversationRuntime) else MessageAttachmentService(self.store))
        return self._attachments

    def _delete_message_attachments(self, conversation_id: str) -> None:
        if self._has_message_attachments():
            self.attachments.delete_conversation(conversation_id)

    def _has_message_attachments(self) -> bool:
        if self._attachments is not None:
            return True
        if not isinstance(self.store, ConversationStore):
            return False
        with self.store._lock:
            return bool(self.store._db.execute("SELECT 1 FROM sqlite_master WHERE "
                "type='table' AND name='message_attachments'").fetchone())

    @property
    def automations(self) -> AutomationStore:
        if self._automations is None:
            self._automations = AutomationStore(self.store)
        return self._automations

    @property
    def scheduler(self) -> AutomationScheduler:
        if self._scheduler is None:
            self._scheduler = AutomationScheduler(self.automations, self._dispatch_automation, self._automation_owner_enabled)
        return self._scheduler

    @property
    def automations_enabled(self) -> bool:
        return self._enable_automations

    @property
    def scheduler_running(self) -> bool:
        return bool(self._scheduler and self._scheduler._task and not self._scheduler._task.done())

    def require_automations_enabled(self) -> None:
        if not self._enable_automations:
            raise ValueError("此部署未启用定时任务；可查看、暂停或删除已有任务。启用需修改安装配置并按维护说明重启，不是对话权限不足。")

    @classmethod
    def configured(cls) -> "ConversationService":
        store = ConversationStore(CONVERSATION_DB)
        try:
            artifacts = ConversationArtifactService(
                store, CONVERSATION_ARTIFACTS
            )
            runtime = ConversationRuntime(
                store,
                artifacts,
                lambda **handlers: CodexAppServerClient(
                    CODEX_APP_SERVER_SOCKET, **handlers
                ),
                status_handler=_broadcast_status,
                terminal_handler=_handle_terminal,
            )
            return cls(store, artifacts, runtime,
                       enable_automations=AUTOMATIONS_ENABLED,
                       enable_completion_notifications=COMPLETION_NOTIFICATIONS_ENABLED)
        except BaseException:
            store.close()
            raise

    async def startup(self) -> list[dict[str, Any]]:
        # Neither transport retries uncertain sends after process death.
        # ntfy only recovers fresh, unsent, non-superseded live completions;
        # Web Push deliberately never scans past terminal turns.
        if self._has_message_attachments():
            self.attachments.resume_admission()
        self.store.release_incomplete_web_push_deliveries()
        self.store.release_incomplete_notifications()
        self.store.configure_completion_notifications(self._enable_completion_notifications)
        self.automations.configure_module(self._enable_automations)
        recovered = await self.runtime.startup_recover()
        for pending in self.store.pending_completion_notifications():
            await self._deliver_completion_notification(
                pending["conversation_id"],
                pending["turn_id"],
                self.store.snapshot(pending["conversation_id"]),
                recovery=True,
            )
        self.automations.recover_runs()
        if self._enable_automations:
            self.scheduler.start()
        return recovered

    async def close(self) -> None:
        try:
            if self._scheduler is not None:
                await self._scheduler.close()
            await self.runtime.close()
        finally:
            self.store.close()

    @staticmethod
    def _automation_owner_enabled(owner: str) -> bool:
        from shared.security import load_accounts
        entry = load_accounts().get(owner)
        return bool(isinstance(entry, dict) and entry.get("enabled") and entry.get("role") == "admin")

    def automation_conversations(self, owner: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT id,title,settings_json FROM conversations WHERE owner_account_id=? AND thread_id IS NOT NULL ORDER BY activity_at DESC LIMIT 200", (owner,)).fetchall()
        import json
        return [{"id": row["id"], "title": row["title"],
                 "permission": json.loads(row["settings_json"]).get("permission"),
                 "collaboration_mode": json.loads(row["settings_json"]).get("collaboration_mode", "default")}
                for row in rows]

    def _scheduled_guard(self, command: dict[str, Any], conversation: dict[str, Any]) -> bool:
        scope = command.get("payload", {}).get("scheduled")
        if not isinstance(scope, dict):
            return False
        try:
            task = self.automations.get(scope.get("task_id"), scope.get("owner_account_id"))
            return bool(task["state"] == "active" and task["revision"] == scope.get("revision") and task["conversation_id"] == conversation["id"]
                        and task["permission"] == scope.get("permission") == conversation["settings"].get("permission")
                        and task["collaboration_mode"] == scope.get("collaboration_mode", "default")
                        == conversation["settings"].get("collaboration_mode", "default")
                        and scope.get("cwd") == conversation["cwd"]
                        and task["owner_account_id"] == conversation.get("owner_account_id")
                        and self._automation_owner_enabled(task["owner_account_id"]))
        except (ValueError, OSError, LookupError):
            return False

    async def _dispatch_automation(self, task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        conversation = self.store.get_conversation(task["conversation_id"])
        return await self.runtime.send_message(
            task["conversation_id"], command_id=run["command_id"], text=task["prompt"],
            skill_selectors=conversation["settings"].get("default_skills", []),
            force_new_turn=True, origin="scheduled",
            scheduled_context={key: task[key] for key in ("owner_account_id", "cwd", "permission", "collaboration_mode")}
                | {"task_id":task["id"], "revision":task["revision"], "run_id":run["id"], "scheduled_at":run["scheduled_at"]},
        )

    def _automation_tool(self, conversation: dict[str, Any], turn_id: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"action","task_id","revision","title","prompt","schedule"}
        if set(arguments) - allowed:
            raise ValueError("定时任务参数包含不支持的字段。")
        action = arguments.get("action")
        if action not in {"create","list","get","runs","update","pause","resume","delete"}:
            raise ValueError("不支持的定时任务操作。")
        if action in {"create", "update", "resume"}:
            self.require_automations_enabled()
        owner = conversation.get("owner_account_id")
        if not owner or not self._automation_owner_enabled(owner):
            raise ValueError("当前对话没有有效的任务所有者；请新建自己的对话。")
        if action not in {"list","get","runs"} and any(c.get("origin") == "scheduled" and c.get("result_turn_id") == turn_id for c in self.store.snapshot(conversation["id"])["commands"]):
            raise ValueError("定时执行不能创建或修改定时任务；请由用户在对话或管理页操作。")
        if action == "list":
            if set(arguments) != {"action"}:
                raise ValueError("list 不接受额外参数。")
            return tool_response({"tasks":self.automations.list(owner,conversation["id"])})
        if action == "create":
            if set(arguments) != {"action","title","prompt","schedule"}:
                raise ValueError("创建任务需要 title、prompt 和 schedule。")
            task = self.automations.create(conversation["id"],owner,title=arguments["title"],prompt=arguments["prompt"],schedule=arguments["schedule"],request_key="tool:"+call_id,initial_state="active")
        else:
            task = self.automations.get(arguments.get("task_id"),owner)
            if task["conversation_id"] != conversation["id"]:
                raise ValueError("不能管理其他对话的定时任务。")
            if action in {"get","runs"}:
                if set(arguments) != {"action","task_id"}:
                    raise ValueError("读取任务不接受额外参数。")
                return tool_response({"task":task, **({"runs":self.automations.runs(task["id"],owner)} if action=="runs" else {})})
            changes = {key:arguments[key] for key in ("title","prompt","schedule") if key in arguments}
            if action == "update" and "schedule" in changes and task["state"] == "finished":
                changes["state"] = "active"
            if action != "update":
                if changes:
                    raise ValueError("此操作不接受任务内容修改。")
                changes["state"] = {"pause":"paused","resume":"active","delete":"deleted"}[action]
            task = self.automations.update(task["id"],owner,revision=arguments.get("revision"),request_key="tool:"+call_id,**changes)
        return tool_response({"task":task,"note":"结果留在原对话；可在对话中要求暂停、恢复或删除，也可使用定时任务管理页。暂停/删除阻止后续轮次，不中断已开始的轮次。权限、执行模式或目录改变时需重新创建任务。"})

    async def catalog(self, cwd: str | None = None) -> dict[str, Any]:
        directory = conversation_cwd(cwd or str(CONVERSATION_WORKSPACES))
        return await self.runtime.catalog(str(directory))

    def create_draft(
        self,
        *,
        cwd: str | None = None,
        project_id: str | None = None,
        model: str = NEW_CONVERSATION_MODEL,
        effort: str = NEW_CONVERSATION_REASONING,
        permission: str = NEW_CONVERSATION_PERMISSION,
        service_tier: str | None = None,
        completion_notification: bool = False,
        collaboration_mode: str = "default",
        client_origin: str | None = None,
        owner_account_id: str | None = None,
        web_preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = {
            "model": model or None,
            "effort": effort or None,
            "permission": permission,
            "service_tier": service_tier,
            "collaboration_mode": collaboration_mode,
        }
        # Only future mobile drafts opt into fallback/native initialization
        # and in-turn AI naming. Never migrate old drafts or named threads.
        if client_origin == "mobile_web":
            settings["_native_initial_title"] = True
            settings["_ai_initial_title"] = {"state": "pending"}
        if web_preferences is not None:
            from shared.workbench_settings import _defaults, _personal
            preferences = _defaults({key: web_preferences[key] for key in ('followup', 'skills')})
            settings.update(_personal_instructions=_personal(web_preferences.get('personal', '')),
                            _followup=preferences['followup'], default_skills=preferences['skills'])
        validate_conversation_settings(settings)
        conversation_id = "c" + uuid.uuid4().hex
        if cwd is None:
            CONVERSATION_WORKSPACES.mkdir(mode=0o700, parents=True, exist_ok=True)
            CONVERSATION_WORKSPACES.chmod(0o700)
            directory = CONVERSATION_WORKSPACES / conversation_id
            directory.mkdir(mode=0o700)
        else:
            directory = conversation_cwd(cwd)
        return self.store.create_conversation(
            conversation_id,
            title="新对话",
            cwd=str(directory),
            project_id=project_id,
            settings=settings,
            completion_notification=completion_notification,
            client_origin=client_origin,
            owner_account_id=owner_account_id,
        )

    async def start(
        self,
        conversation_id: str,
        *,
        command_id: str,
        text: str,
        skill_selectors: list[str | dict[str, str]] | None = None,
        attachment_ids: list[str] | None = None,
        attachment_owner_id: str | None = None,
        initial_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = self.store.get_conversation(conversation_id)
        references = self.attachments.resolve(attachment_ids,
            owner=attachment_owner_id, conversation=conversation) if attachment_ids else []
        text = self._message(text, allow_empty=bool(references))
        if (conversation["thread_id"] is None and conversation["title"] == "新对话"
                and conversation["settings"].get("_ai_initial_title", {}).get("state") != "manual"):
            self.store.update_conversation_title(
                conversation_id, self._title_from_message(text or references[0]["name"]),
                manual=False,
            )
        if skill_selectors is None:
            skill_selectors = conversation['settings'].get('default_skills', [])
        return await self.runtime.start_conversation(
            conversation_id,
            command_id=command_id,
            text=text,
            skill_selectors=skill_selectors,
            **({"attachment_refs": references} if references else {}),
            **({"request_context": initial_request} if initial_request is not None else {}),
        )

    async def send(
        self,
        conversation_id: str,
        *,
        command_id: str,
        text: str,
        skill_selectors: list[str | dict[str, str]] | None = None,
        force_new_turn: bool | None = None,
        attachment_ids: list[str] | None = None,
        attachment_owner_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = self.store.get_conversation(conversation_id)
        preferences = conversation['settings']
        references = self.attachments.resolve(attachment_ids,
            owner=attachment_owner_id, conversation=conversation) if attachment_ids else []
        if force_new_turn is None:
            force_new_turn = preferences.get('_followup') == 'queue'
        if skill_selectors is None:
            skill_selectors = preferences.get('default_skills', [])
        return await self.runtime.send_message(
            conversation_id,
            command_id=command_id,
            text=self._message(text, allow_empty=bool(references)),
            skill_selectors=skill_selectors,
            force_new_turn=force_new_turn,
            **({"attachment_refs": references} if references else {}),
        )

    async def interrupt(
        self,
        conversation_id: str,
        *,
        command_id: str,
        expected_turn_id: str = "",
        scope: str = "root_turn",
    ) -> dict[str, Any]:
        return await self.runtime.interrupt(
            conversation_id,
            command_id=command_id,
            expected_turn_id=expected_turn_id,
            **({"scope": scope} if scope != "root_turn" else {}),
        )

    async def start_goal(self, conversation_id: str, *, command_id: str,
                         objective: str, goal_version: str = "",
                         skill_selectors=None) -> dict[str, Any]:
        from engine.conversation_actions import ConversationActions
        conversation = self.store.get_conversation(conversation_id)
        objective = self._message(objective)
        if skill_selectors is None:
            skill_selectors = conversation["settings"].get("default_skills", [])
        return await ConversationActions(self.runtime).start_goal(conversation_id,
            command_id=command_id, objective=objective, goal_version=goal_version,
            skills=skill_selectors)

    async def implement_plan(self, conversation_id: str, *, command_id: str,
                             turn_id: str, item_id: str, plan_digest: str, context_digest: str = "",
                             skill_selectors=None) -> dict[str, Any]:
        from engine.conversation_actions import ConversationActions
        if skill_selectors is None:
            skill_selectors = self.store.get_conversation(conversation_id)["settings"].get("default_skills", [])
        return await ConversationActions(self.runtime).implement_plan(conversation_id,
            command_id=command_id, turn_id=turn_id, item_id=item_id, plan_digest=plan_digest,
            context_digest=context_digest, skills=skill_selectors)

    async def reconcile(self, conversation_id: str) -> dict[str, Any]:
        return await self.runtime.reconcile(conversation_id, origin="manual")

    async def update_settings(
        self,
        conversation_id: str,
        *,
        command_id: str,
        updates: dict[str, Any],
        completion_notification: bool | None = None,
    ) -> dict[str, Any]:
        if not updates and completion_notification is None:
            raise ValueError("no conversation settings were supplied")
        if updates:
            value = await self.runtime.update_settings(
                conversation_id, command_id=command_id, updates=updates
            )
        else:
            value = {
                "command": None,
                "snapshot": self.store.snapshot(conversation_id),
            }
        if completion_notification is not None:
            self.store.set_completion_notification(
                conversation_id, completion_notification
            )
            value = {
                "command": value.get("command"),
                "snapshot": self.store.snapshot(conversation_id),
            }
        return value

    async def compact(
        self, conversation_id: str, *, command_id: str
    ) -> dict[str, Any]:
        return await self.runtime.compact(conversation_id, command_id=command_id)

    async def answer_interaction(
        self, conversation_id: str, *, interaction_id: str, command_id: str,
        attempt: int, fingerprint: str, response: Any, answered_by: str,
    ) -> dict[str, Any]:
        return await self.runtime.answer_interaction(
            conversation_id, interaction_id=interaction_id, command_id=command_id,
            attempt=attempt, fingerprint=fingerprint, response=response, answered_by=answered_by,
        )

    async def answer_question(
        self, conversation_id: str, *, question_id: str, command_id: str,
        answers: Any,
    ) -> dict[str, Any]:
        return await self.runtime.answer_question(
            conversation_id, question_id=question_id, command_id=command_id,
            answers=answers,
        )

    async def answer_async_question(
        self, conversation_id: str, *, request_id: str, command_id: str,
        answer_text: str,
    ) -> dict[str, Any]:
        return await self.runtime.answer_async_question(
            conversation_id, request_id=request_id, command_id=command_id,
            answer_text=self._message(answer_text),
        )

    async def delete(self, conversation_id: str) -> None:
        conversation = self.store.get_conversation(conversation_id)
        retrying_unknown_delete = (
            conversation["state"] == "needs_user"
            and has_pending_native_delete(conversation)
        )
        if conversation["state"] in ACTIVE_STATES or (
            conversation["state"] == "needs_user" and not retrying_unknown_delete
        ):
            raise AppServerProtocolError(
                "active or uncertain conversation must be settled before deletion"
            )
        # Stop future/queued automation before native deletion can yield.
        owner = conversation.get("owner_account_id")
        if owner:
            for task in self.automations.list(owner, conversation_id):
                if task["state"] == "active":
                    self.automations.update(task["id"], owner, revision=task["revision"], state="paused")
        await self.runtime.delete_native_thread(conversation_id)
        self._remove_private_workspace(conversation_id, conversation["cwd"])
        self.artifacts.delete_conversation(conversation_id)
        self._delete_message_attachments(conversation_id)
        if isinstance(self.runtime, ConversationRuntime):
            self.runtime.interactions.close_conversation(conversation_id)
        self.store.delete_conversation(conversation_id)

    async def delete_many(self, conversation_ids: list[str]) -> None:
        for conversation_id in list(dict.fromkeys(conversation_ids)):
            await self.delete(conversation_id)

    def set_pinned(self, conversation_ids: list[str], pinned: bool) -> None:
        self.store.set_pinned(conversation_ids, pinned)

    async def update_metadata(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        if title is None and pinned is None:
            raise ValueError("no conversation metadata was supplied")
        if title is not None:
            await self.runtime.update_manual_title(conversation_id, title)
        if pinned is not None:
            self.store.set_pinned([conversation_id], pinned)
        return self.public_snapshot(conversation_id)

    def has_active_projects(self, project_ids: set[str]) -> bool:
        return self.store.has_active_conversations(project_ids)

    def delete_projects(self, project_ids: list[str], delete) -> None:
        # Reserve synchronously with command admission, then release the DB
        # lock before potentially slow filesystem deletion. Other projects
        # and authoritative native ingress remain available throughout.
        selected = set(project_ids)
        with self.store._lock:
            if self.has_active_projects(selected):
                raise ValueError('选中项目包含运行中、排队或待核对的对话，请先处理。')
            self.store._project_deletes.update(selected)
        try:
            for project_id in project_ids:
                delete(project_id)
                if self._has_message_attachments():
                    self.attachments.delete_project(project_id)
        finally:
            with self.store._lock:
                self.store._project_deletes.difference_update(selected)

    def upload_context(self, conversation_id: str) -> dict[str, str]:
        value = self.store.get_conversation(conversation_id)
        return {"cwd": value["cwd"], "project_id": value.get("project_id") or ""}

    def workspace_summary(self, conversation_id: str) -> dict[str, str]:
        value = self.store.get_conversation(conversation_id)
        return {
            "title": str(value.get("title") or "新对话"),
            "url": "/conversation/" + value["id"],
        }

    def cleanup_abandoned_drafts(self, *, maximum_age_hours: int = 48) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=maximum_age_hours)
        removed = 0
        drafts = self.store.list_conversations(500, include_drafts=True)
        for draft in drafts:
            if draft.get("thread_id") is not None:
                continue
            try:
                created = datetime.fromisoformat(draft["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue
            try:
                self._remove_private_workspace(draft["id"], draft["cwd"])
                self.artifacts.delete_conversation(draft["id"])
                self._delete_message_attachments(draft["id"])
                self.store.delete_conversation(draft["id"])
            except OSError:
                continue
            removed += 1
        return removed

    @staticmethod
    def _remove_private_workspace(conversation_id: str, cwd: str) -> None:
        """Remove only the app-owned per-conversation workspace, including uploads."""
        root = CONVERSATION_WORKSPACES.resolve()
        candidate = Path(cwd).absolute()
        if candidate.parent.resolve() != root or candidate.name != conversation_id:
            return
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.exists():
            shutil.rmtree(candidate)

    async def handle_terminal(
        self,
        conversation_id: str,
        turn_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        # A task spawned before deletion can arrive with an old snapshot.
        # Per-channel atomic claims below still recheck after any awaits.
        if not self.store.source_delivery_is_eligible(conversation_id):
            self.store.defer_terminal_delivery(conversation_id, turn_id)
            return
        if isinstance(self.runtime, ConversationRuntime) and self.runtime.goals.store.notice_suppressed(conversation_id, turn_id):
            return
        conversation = snapshot["conversation"]
        turn = next(
            (value for value in snapshot["turns"] if value["id"] == turn_id),
            None,
        )
        if (
            turn is not None
            and conversation.get("last_terminal_turn_id") == turn_id
        ):
            from routes.ws import manager

            await manager.broadcast("conversation.terminal", {
                "conversation_id": conversation_id,
                "title": conversation.get("title") or "新对话",
                "turn_id": turn_id,
                "generation": int(conversation.get("terminal_generation") or 0),
                "status": turn.get("status"),
                "completed_at": turn.get("completed_at"),
            })
        # Preserve the existing ntfy delivery timing.  Web Push is an
        # additional, independently accounted channel and must not delay it.
        await self._deliver_completion_notification(
            conversation_id, turn_id, snapshot,
            recovery=snapshot.get("terminal_source") in {"reconcile", "delete_deferred_recovery"},
        )
        await self._deliver_web_push_notification(conversation_id, turn_id, snapshot)
        # Deletion can begin during a slow broadcast or another channel's
        # send. Preserve only the channels whose claim has not started.
        self.store.defer_terminal_delivery(conversation_id, turn_id)

    @staticmethod
    def _web_push_owner(conversation: dict[str, Any]) -> str | None:
        owner = str(conversation.get("owner_account_id") or "").strip()
        from shared.security import load_accounts
        accounts = load_accounts()
        if owner:
            entry = accounts.get(owner)
            if (
                isinstance(entry, dict)
                and entry.get("role") == "admin"
                and entry.get("enabled")
            ):
                return owner
            return None
        # Historical conversations predate owner tracking.  Fall back only
        # when one enabled administrator exists; ambiguity means no delivery.
        admins = [
            name for name, entry in accounts.items()
            if isinstance(entry, dict) and entry.get("role") == "admin"
            and entry.get("enabled")
        ]
        return admins[0] if len(admins) == 1 else None

    async def _deliver_web_push_notification(
        self, conversation_id: str, turn_id: str, snapshot: dict[str, Any]
    ) -> None:
        conversation = snapshot.get("conversation") or {}
        turn = next(
            (value for value in snapshot.get("turns") or [] if value.get("id") == turn_id),
            None,
        )
        if not turn or not conversation.get("completion_notification"):
            return
        owner = self._web_push_owner(conversation)
        if not owner:
            return
        deliveries = self.store.prepare_web_push_deliveries(
            conversation_id, turn_id, owner,
            resume_pending=snapshot.get("terminal_source") in {"delete_deferred", "delete_deferred_recovery"},
        )
        if not deliveries:
            self.store.defer_terminal_delivery(conversation_id, turn_id)
            return
        from shared.web_push import WebPushError, build_payload, send
        labels = {
            "completed": "回复已完成",
            "failed": "回复失败",
            "interrupted": "回复已停止",
        }
        try:
            payload = build_payload(
                title=labels.get(turn.get("status"), "回复已结束"),
                final_text=str(turn.get("final_text") or ""),
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        except WebPushError:
            return
        for delivery in deliveries:
            subscription_id = str(delivery["subscription_id"])
            if not self.store.claim_web_push_delivery(turn_id, subscription_id):
                self.store.defer_terminal_delivery(conversation_id, turn_id)
                continue
            try:
                state, detail = await asyncio.to_thread(send, delivery, payload)
            except WebPushError as exc:
                state, detail = "unknown", type(exc).__name__
            self.store.record_web_push_delivery(
                turn_id, subscription_id, state=state, detail=detail
            )

    async def _deliver_completion_notification(
        self,
        conversation_id: str,
        turn_id: str,
        snapshot: dict[str, Any],
        *,
        recovery: bool = False,
    ) -> None:
        if not self._enable_completion_notifications:
            self.store.skip_completion_notification(conversation_id, turn_id)
            return
        if isinstance(self.runtime, ConversationRuntime) and self.runtime.goals.store.notice_suppressed(conversation_id, turn_id):
            return
        if not self.store.claim_completion_notification(
            conversation_id, turn_id, recovery=recovery
        ):
            self.store.defer_terminal_delivery(conversation_id, turn_id)
            return
        conversation = snapshot["conversation"]
        turn = next(
            (value for value in snapshot["turns"] if value["id"] == turn_id),
            None,
        )
        project_name = "临时对话"
        if conversation.get("project_id"):
            project_name = "项目对话"
            try:
                import projects

                project_name = projects.get(conversation["project_id"])["name"]
            except (OSError, ValueError, KeyError):
                pass
        title, body, tags = format_completion_notification(
            status=turn.get("status") if turn else None,
            conversation_title=conversation.get("title"),
            project_name=project_name,
            ordinal=turn.get("ordinal") if turn else None,
            final_text=turn.get("final_text") if turn else None,
        )
        from shared.security import notify_task_completion

        sent, detail = await asyncio.to_thread(
            notify_task_completion, title, body, tags=tags
        )
        self.store.record_completion_notification(
            conversation_id, turn_id, sent=sent, detail=detail
        )

    def list_public(
        self,
        *,
        limit: int = 100,
        project_id: str | None | object = ...,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self._public_conversation(value)
            for value in self.store.list_conversations(
                limit,
                project_id=project_id,
                include_drafts=False,
                account_id=account_id,
            )
        ]

    def list_public_page(
        self,
        *,
        account_id: str | None,
        project_id: str | None | object = ...,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one stable page without holding full records in memory.

        The page snapshot fixes ID order only.  Each request reloads the small
        current records so deleted or re-scoped conversations disappear rather
        than leaking into an old project page.
        """
        scope = "all" if project_id is ... else "temporary" if project_id is None else "project:" + str(project_id)
        if cursor is None:
            ids, order_version = self.store.list_conversation_order(
                project_id=project_id, account_id=account_id,
            )
            snapshot = self._pages.start(
                ids,
                account_id=account_id,
                scope=scope,
                order_version=order_version,
            )
            position = 0
        else:
            snapshot, position = self._pages.resume(
                cursor, account_id=account_id, scope=scope
            )
        values, next_position = self._snapshot_page_values(
            snapshot.conversation_ids,
            position=position,
            project_id=project_id,
            account_id=account_id,
        )
        next_cursor = (
            self._pages.cursor(snapshot, next_position)
            if next_position < len(snapshot.conversation_ids) else None
        )
        return {"values": values, "next_cursor": next_cursor,
                "order_version": snapshot.order_version}

    def public_order_version(
        self, *, account_id: str | None, project_id: str | None | object = ...,
    ) -> str:
        """Read-only index change probe; never evicts anyone's paging snapshot."""
        return self.store.list_conversation_order(
            account_id=account_id, project_id=project_id,
        )[1]

    def search_public_page(
        self,
        query: str | SearchQuery,
        *,
        account_id: str | None,
        project_id: str | None | object = ...,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a stable, local-only page of history-search cards.

        The snapshot fixes an initial ID order, while every later request
        repeats both list scope and literal search checks.  Thus a deleted,
        re-scoped, or edited-away conversation cannot surface through an old
        search cursor.
        """
        parsed = query if isinstance(query, SearchQuery) else normalize_search_query(query)
        base_scope = (
            "all" if project_id is ... else
            "temporary" if project_id is None else "project:" + str(project_id)
        )
        scope = "search:" + base_scope + ":" + parsed.scope_hash
        if cursor is None:
            snapshot = self._pages.start(
                self.store.search_conversation_ids(parsed, project_id=project_id),
                account_id=account_id,
                scope=scope,
            )
            position = 0
        else:
            snapshot, position = self._pages.resume(
                cursor, account_id=account_id, scope=scope
            )
        values, next_position = self._search_snapshot_page_values(
            parsed,
            snapshot.conversation_ids,
            position=position,
            project_id=project_id,
            account_id=account_id,
        )
        next_cursor = (
            self._pages.cursor(snapshot, next_position)
            if next_position < len(snapshot.conversation_ids) else None
        )
        return {"values": values, "next_cursor": next_cursor}

    def _search_snapshot_page_values(
        self,
        query: SearchQuery,
        conversation_ids: tuple[str, ...],
        *,
        position: int,
        project_id: str | None | object,
        account_id: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        values: list[dict[str, Any]] = []
        next_position = position
        while next_position < len(conversation_ids):
            end = min(
                len(conversation_ids), next_position + max(1, PAGE_SIZE - len(values))
            )
            batch = list(conversation_ids[next_position:end])
            current = self.store.list_conversations_by_ids(
                batch, project_id=project_id, account_id=account_id,
            )
            by_id = {str(value["id"]): value for value in current}
            hits = {
                str(value["conversation_id"]): value
                for value in self.store.search_matches_by_ids(
                    query, batch, project_id=project_id,
                )
            }
            for conversation_id in batch:
                value = by_id.get(conversation_id)
                hit = hits.get(conversation_id)
                if value is not None and hit is not None:
                    if len(values) == PAGE_SIZE:
                        return values, next_position
                    values.append(self._public_conversation(value) | {"search_hit": hit})
                next_position += 1
        return values, next_position

    def _snapshot_page_values(
        self,
        conversation_ids: tuple[str, ...],
        *,
        position: int,
        project_id: str | None | object,
        account_id: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        values: list[dict[str, Any]] = []
        next_position = position
        # Re-query in bounded batches: never materialize a snapshot's full
        # rows, and keep room to compensate for records deleted between pages.
        while next_position < len(conversation_ids):
            end = min(
                len(conversation_ids), next_position + max(1, PAGE_SIZE - len(values))
            )
            current = self.store.list_conversations_by_ids(
                list(conversation_ids[next_position:end]),
                project_id=project_id,
                account_id=account_id,
            )
            by_id = {str(value["id"]): value for value in current}
            for conversation_id in conversation_ids[next_position:end]:
                value = by_id.get(conversation_id)
                if value is not None:
                    # One surviving lookahead keeps the final button hidden
                    # even if all remaining snapshot rows were deleted.
                    if len(values) == PAGE_SIZE:
                        return values, next_position
                    values.append(self._public_conversation(value))
                next_position += 1
        return values, next_position

    def public_index_by_ids(
        self,
        conversation_ids: list[str],
        *,
        account_id: str | None,
        project_id: str | None | object = ...,
    ) -> list[dict[str, Any]]:
        """Return current index-safe rows for an explicitly loaded card batch.

        This deliberately reads the existing list projection only: it does not
        create a pagination snapshot, reconcile a conversation, mark it read,
        or contact the App Server runtime.
        """
        wanted = list(dict.fromkeys(
            value for value in conversation_ids if isinstance(value, str)
        ))
        query: dict[str, Any] = {"account_id": account_id}
        if project_id is not ...:
            query["project_id"] = project_id
        return [
            self._public_conversation(value)
            for value in self.store.list_conversations_by_ids(
                wanted, **query,
            )
        ]

    def scoped_public_ids(
        self,
        conversation_ids: list[str],
        *,
        project_id: str | None | object = ...,
        account_id: str | None = None,
    ) -> list[str]:
        """Validate selected IDs against their current list scope in batches."""
        wanted = list(dict.fromkeys(
            value for value in conversation_ids if isinstance(value, str)
        ))
        found: set[str] = set()
        for offset in range(0, len(wanted), 800):
            found.update(
                str(value["id"])
                for value in self.store.list_conversations_by_ids(
                    wanted[offset:offset + 800],
                    project_id=project_id,
                    account_id=account_id,
                )
            )
        return [value for value in wanted if value in found]

    def mark_read(self, conversation_id: str, account_id: str) -> None:
        self.store.mark_read(conversation_id, account_id)

    def _public_plan_confirmation(self, value: dict[str, Any]) -> dict[str, Any] | None:
        """Expose only browser-safe Plan review data; never attachment paths."""
        candidate = plan_confirmation_candidate(value)
        if candidate is None:
            return None
        plan = candidate.get("plan") or {}
        binding = candidate.get("binding") or {}
        result = {
            "state": candidate.get("state"),
            "turn_id": binding.get("turn_id"),
            "item_id": binding.get("item_id"),
            "plan_digest": binding.get("plan_digest"),
            "text": plan.get("text") if isinstance(plan.get("text"), str) else "",
            "supplements": [],
            "reason": candidate.get("reason"),
        }
        if candidate.get("state") == "review":
            result["context_digest"] = candidate.get("context_digest")
        for command in candidate.get("supplements", []):
            attachments = command.get("payload", {}).get("attachments") or []
            safe = []
            for attachment in attachments:
                if isinstance(attachment, dict) and isinstance(attachment.get("id"), str):
                    try:
                        safe.append(public_attachment(attachment, conversation_id=value["conversation"].get("id")))
                    except (KeyError, TypeError, ValueError):
                        # A malformed durable reference is never a reason to
                        # leak its internal fallback fields to the browser.
                        continue
            result["supplements"].append({
                "id": command.get("id"),
                "scope": command.get("scope"),
                "text": command.get("text") or "",
                "attachments": safe,
            })
        return result

    def public_snapshot(
        self, conversation_id: str, *, include_text_previews: bool = False
    ) -> dict[str, Any]:
        value = self.store.snapshot(conversation_id)
        return self._public_snapshot_value(value, include_text_previews=include_text_previews)

    def public_browser_view(self, conversation_id: str, *, before: str = "", pages: list[str] | None = None,
                            include_text_previews: bool = False) -> dict[str, Any]:
        value = self.store.browser_view(conversation_id, before=before, pages=pages)
        result = self._public_snapshot_value(value, include_text_previews=include_text_previews)
        result["plan_confirmation"] = self._public_plan_confirmation(self.store.browser_plan_context(conversation_id))
        history = value["history"]
        result["history"] = {key: history[key] for key in ("mode", "cursor", "previous_turn_id", "token", "page_turn_ids")}
        result["history"]["replacements"] = [{
            "previous": page["previous"], "token": page["token"],
            "cursor": page["cursor"], "previous_turn_id": page["previous_turn_id"],
            "snapshot": self._public_snapshot_value(page["value"], include_text_previews=include_text_previews),
        } for page in history["replacements"]]
        return result

    def _public_snapshot_value(self, value: dict[str, Any], *, include_text_previews: bool = False) -> dict[str, Any]:
        conversation_id = value["conversation"]["id"]
        async_by_item = {
            (
                item.get("turn_id"), item.get("native_item_id"),
                item.get("question_index"),
            ): self._public_async_question(item)
            for item in value.get("async_questions", [])
            if isinstance(item, dict)
        }
        return {
            **(self.runtime.goals.store.public(conversation_id) if isinstance(self.runtime, ConversationRuntime) else {}),
            "capabilities": {"conversation_tree_stop": bool(
                isinstance(self.runtime, ConversationRuntime) and self.runtime._client
                and getattr(self.runtime._client, 'connected', False)
                and getattr(self.runtime._client, 'supports_conversation_tree_stop', False))},
            "stop": self.public_tree_stop(conversation_id),
            "plan_confirmation": self._public_plan_confirmation(value),
            "schema_version": value["schema_version"],
            "server_now": datetime.now(timezone.utc).isoformat(),
            "conversation": self._public_conversation(value["conversation"]),
            "turns": [
                self._public_turn(turn, include_text_previews, async_by_item)
                for turn in value["turns"]
            ],
            "messages": [
                self._public_command(item)
                for item in value["commands"]
                if item["kind"] in {"start", "steer"}
            ],
            "queue": [self._public_command(item) for item in value["queue"]],
            "artifacts": [
                self._public_artifact(item, include_text_previews)
                for item in value["artifacts"]
            ],
            "questions": [self._public_question(item) for item in value.get("questions", [])],
            "native_interactions": [
                {key: item.get(key) for key in (
                    "id", "method", "turn_id", "native_item_id", "fingerprint",
                    "display", "state", "attempt",
                )}
                for item in (self.runtime.interactions.list(conversation_id)
                             if isinstance(self.runtime, ConversationRuntime) else [])
            ],
        }

    def public_tree_stop(self, conversation_id: str) -> dict[str, Any] | None:
        import json
        from engine.conversation_stop import _RESULT_PREFIX
        command = self.store.effective_tree_stop(conversation_id)
        if not command:
            return None
        status = 'verified' if command['state'] == 'completed' else (
            'rejected' if command['state'] == 'rejected' else 'unconfirmed')
        if status == 'verified' and (not self.store.tree_stop_is_current(conversation_id)
                or self.store.get_conversation(conversation_id)['state'] == 'active'
                or (isinstance(self.runtime, ConversationRuntime) and self.runtime.goals.continuing(conversation_id))):
            status = 'historical'
        result = {'command_id': command['id'], 'status': status}
        reason = command.get('reason') or ''
        if reason.startswith(_RESULT_PREFIX):
            try:
                details = json.loads(reason[len(_RESULT_PREFIX):])
                result.update({key: details[key] for key in (
                    'threads', 'active_threads', 'active_goals', 'terminals', 'background_errors', 'errors')
                    if isinstance(details.get(key), int)})
            except (ValueError, TypeError):
                pass
        return result

    def export_transcript(
        self, conversation_id: str, *, exported_at: datetime | None = None
    ) -> dict[str, str]:
        """Format the existing public projection without touching runtime state."""
        snapshot = self.public_snapshot(conversation_id)
        conversation = snapshot["conversation"]
        project_name = self._export_project_name(conversation)
        return {
            "title": str(conversation.get("title") or "未命名对话"),
            "markdown": format_conversation_export(
                snapshot,
                project_name=project_name,
                exported_at=exported_at or datetime.now().astimezone(),
            ),
        }

    @staticmethod
    def _export_project_name(conversation: dict[str, Any]) -> str:
        project_id = conversation.get("project_id")
        if not project_id:
            return "临时对话"
        try:
            import projects

            name = projects.get(project_id).get("name")
            if isinstance(name, str) and name.strip():
                return name
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return "项目名称不可用"

    def artifact_record(
        self, conversation_id: str, artifact_id: str
    ) -> dict[str, Any]:
        record = self.store.artifact(artifact_id)
        if record["conversation_id"] != conversation_id:
            raise AppServerProtocolError("artifact does not belong to conversation")
        return record

    def _public_turn(
        self,
        value: dict[str, Any],
        include_text_previews: bool,
        async_by_item: dict[tuple[Any, Any, Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "id", "ordinal", "status", "kind", "started_at", "completed_at",
                "final_text", "error", "notification_state",
                "artifact_publish_failed",
            )
        } | {
            "commands": [self._public_command(item) for item in value["commands"]],
            "items": [
                self._public_item(item, async_by_item)
                for item in self._browser_agent_items(value["items"])
            ],
            "artifacts": [
                self._public_artifact(item, include_text_previews)
                for item in value["artifacts"]
            ],
        }

        # A content revision of the *public* projection, not a timestamp: an
        # older item can be corrected without changing its id or time. Clients
        # can keep unchanged history entities without serializing their bodies.
        result["presentation_revision"] = hashlib.sha256(json.dumps(
            result, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return result

    @classmethod
    def _public_conversation(cls, value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "id", "thread_id", "title", "project_id", "state",
                "active_turn_id", "thread_status", "settings", "recovery_state",
                "connection_state", "completion_notification", "pinned",
                "client_origin", "created_at", "updated_at", "activity_at",
                "terminal_generation", "last_terminal_turn_id",
                "last_terminal_at", "last_terminal_status", "unread", "revision",
                "context_compaction_completed_count",
                "context_compaction_manual_completed_count",
                "context_compaction_other_completed_count",
                "context_compaction_state",
                "context_switch_count",
                "context_switches",
            )
        }
        settings = value.get("settings")
        if isinstance(settings, dict):
            result["settings"] = {
                key: setting
                for key, setting in settings.items()
                if not str(key).startswith("_")
            }
        result["usage"] = cls._public_usage(value.get("token_usage"))
        switches = value.get("context_switches")
        switch_count = value.get("context_switch_count")
        result["context_switch_count"] = (
            switch_count
            if isinstance(switch_count, int) and not isinstance(switch_count, bool)
            and switch_count >= 0
            else 0
        )
        result["context_switches"] = [
            {
                "observed_at": item.get("observed_at"),
                "state": item.get("state"),
            }
            for item in switches
            if (
                isinstance(item, dict)
                and isinstance(item.get("observed_at"), str)
                and item.get("state") in {"call_seen", "output_seen"}
            )
        ] if isinstance(switches, list) else []
        return result

    @staticmethod
    def _public_usage(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        last = value.get("last")
        total = value.get("total")
        limit = value.get("modelContextWindow")
        if not isinstance(last, dict) or not isinstance(total, dict):
            return None
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return None
        context_used = last.get("inputTokens")
        if isinstance(context_used, bool) or not isinstance(context_used, int):
            return None
        fields = {
            "input_tokens": total.get("inputTokens"),
            "cached_input_tokens": total.get("cachedInputTokens"),
            "output_tokens": total.get("outputTokens"),
            "reasoning_output_tokens": total.get("reasoningOutputTokens"),
        }
        if any(isinstance(item, bool) or not isinstance(item, int) for item in fields.values()):
            return None
        updated_at = value.get("updatedAt")
        if not isinstance(updated_at, str) or not updated_at:
            return None
        return {
            "context_used": context_used,
            "context_limit": limit,
            "context_percent": context_used * 100 / limit,
            **fields,
            "updated_at": updated_at,
        }

    @staticmethod
    def _browser_agent_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expose authored messages and native plans, never internal activity."""
        visible = [
            item for item in items
            if item.get("is_current_source") is not False
            and (
                (item.get("kind") == "plan" and item.get("native_item_id") != "turn-plan")
                or (
                    item.get("kind") == "agent_message"
                    and item.get("phase") in {"commentary", "final_answer"}
                )
            )
        ]
        realtime_keys = {
            ConversationService._agent_text_key(item)
            for item in visible
            if ConversationService._realtime_message_id(item)
        }
        return [
            item for item in visible
            if not (
                ConversationService._thread_read_item_number(item)
                and ConversationService._agent_text_key(item) in realtime_keys
            )
        ]

    @staticmethod
    def _agent_text_key(item: dict[str, Any]) -> tuple[str, str, str] | None:
        return agent_message_text_key(item)

    @staticmethod
    def _realtime_message_id(item: dict[str, Any]) -> bool:
        return realtime_agent_message(item)

    @staticmethod
    def _thread_read_item_number(item: dict[str, Any]) -> bool:
        return thread_read_agent_copy(item)

    @staticmethod
    def _public_command(value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "id", "kind", "state", "target_turn_id", "result_turn_id",
                "text", "reason", "origin", "created_at", "updated_at",
            )
        }
        references = value.get("payload", {}).get("attachments") or []
        if references:
            result["attachments"] = [public_attachment(reference,
                conversation_id=value.get("conversation_id")) for reference in references]
        return result

    @classmethod
    def _public_item(
        cls,
        value: dict[str, Any],
        async_by_item: dict[tuple[Any, Any, Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "native_item_id", "kind", "status", "phase", "text", "detail",
                "delivery", "questions", "revision", "created_at", "updated_at",
            )
        }
        if value.get("kind") == "plan":
            # A plan is a user-facing deliverable, not an agent-message final
            # or an invitation to expose arbitrary native item metadata.
            result.update(phase=None, detail=None, delivery=None, questions=None)
            result["plan_digest"] = hashlib.sha256(str(value.get("text") or "").encode()).hexdigest()
        questions = result.get("questions")
        if type(value.get("is_current_source")) is bool:
            result["is_current_source"] = value["is_current_source"]
        result["dedup_role"] = (
            "realtime" if realtime_agent_message(value) else
            "legacy-copy" if thread_read_agent_copy(value) else "native"
        )
        if result.get("delivery") == "async" and isinstance(questions, list):
            decorated: list[dict[str, Any]] = []
            for index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                entry = dict(question)
                request = (async_by_item or {}).get((
                    value.get("turn_id"), value.get("native_item_id"), index,
                ))
                if request is not None:
                    entry["request"] = request
                decorated.append(entry)
            result["questions"] = decorated
        return result

    @staticmethod
    def _public_async_question(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in (
                "id", "turn_id", "native_item_id", "question_index", "state",
                "deadline_at", "answer_source", "command_state", "resolved_at",
            )
        }

    @staticmethod
    def _public_question(value: dict[str, Any]) -> dict[str, Any]:
        questions = value.get("questions") if isinstance(value.get("questions"), list) else []
        secret_ids = {
            item.get("id") for item in questions
            if isinstance(item, dict) and item.get("isSecret")
        }
        answers = value.get("answers")
        masked = None
        if isinstance(answers, dict):
            masked = {
                key: ("***" if key in secret_ids else answer)
                for key, answer in answers.items()
            }
        return {
            "id": value.get("id"), "turn_id": value.get("turn_id"),
            "attempt": value.get("attempt", 1),
            "questions": questions, "received_at": value.get("received_at"),
            "deadline_at": value.get("deadline_at"),
            "deadline_source": value.get("deadline_source"),
            "is_blocking": value.get("is_blocking"), "state": value.get("state"),
            "answer_source": value.get("answer_source"), "answers_masked": masked,
            "resolved_at": value.get("resolved_at"),
        }

    def _public_artifact(
        self, value: dict[str, Any], include_text_preview: bool
    ) -> dict[str, Any]:
        artifact_id = value["id"]
        conversation_id = value["conversation_id"]
        result = {
            key: value.get(key)
            for key in (
                "id", "turn_id", "name", "mime_type", "size", "preview_kind",
                "origin", "created_at",
            )
        }
        base = f"/conversation/{conversation_id}/artifact/{artifact_id}"
        result["content_url"] = base
        result["download_url"] = base + "?download=1"
        if self.artifacts.html_previewable(value):
            result["preview_kind"] = "html"
            result["preview_url"] = base + "/preview"
        if include_text_preview and value["preview_kind"] == "text":
            result["preview_text"] = self.artifacts.text_preview(value)
        return result

    @staticmethod
    def _message(value: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not value.strip() and not allow_empty):
            raise AppServerProtocolError("message must be non-empty")
        text = value.strip()
        return text

    @staticmethod
    def _title_from_message(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()[:72] or "新对话"


_service: ConversationService | None = None
_service_lock = asyncio.Lock()


async def startup_conversation_service() -> ConversationService:
    global _service
    async with _service_lock:
        if _service is None:
            service = ConversationService.configured()
            _service = service
            try:
                await service.startup()
            except BaseException:
                _service = None
                await service.close()
                raise
        return _service


async def shutdown_conversation_service() -> None:
    global _service
    async with _service_lock:
        service = _service
        _service = None
        if service is not None:
            await service.close()


def conversation_service() -> ConversationService:
    if _service is None:
        raise RuntimeError("conversation service has not started")
    return _service


async def _broadcast_status(
    conversation_id: str, snapshot: dict[str, Any]
) -> None:
    from routes.ws import manager

    conversation = snapshot["conversation"]
    await manager.broadcast("conversation.updated", {
        "conversation_id": conversation_id,
        "title": conversation.get("title") or "新对话",
        "revision": conversation["revision"],
        "state": conversation["state"],
        "active_turn_id": conversation.get("active_turn_id"),
        "boundary": bool(snapshot.get("status_boundary", True)),
    })


async def _handle_terminal(
    conversation_id: str,
    turn_id: str,
    snapshot: dict[str, Any],
) -> None:
    await conversation_service().handle_terminal(
        conversation_id, turn_id, snapshot
    )
