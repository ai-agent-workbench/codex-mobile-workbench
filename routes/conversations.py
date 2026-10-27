"""Server-rendered App Server-only conversation pages and form entry points."""

from __future__ import annotations

import html as html_escape
import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from engine.codex_app_server import AppServerError, AppServerProtocolError
from engine.conversation_service import conversation_service
from shared.client_origin import classify_client_origin
from shared.conversation_defaults import new_defaults
from shared.conversation_catalog import (
    catalog_models, catalog_skills,
)
from shared.conversation_paths import conversation_cwd
from shared.html_lib import (
    html,
    nav,
    templates,
    upload_button,
    upload_notice,
    upload_panel,
)
from shared.security import audit, logged_in, require_auth


router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _checked(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


@router.get("/quick")
async def quick_conversation(
    request: Request,
    workdir: str = "",
    project_id: str = "",
    prompt: str = "",
    quoted: str = "",
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        normalized_project = project_id.strip() or None
        if normalized_project:
            import projects

            cwd = projects.get(normalized_project)["path"]
        else:
            cwd = str(conversation_cwd(workdir)) if workdir else None
        origin, _evidence, _policy = classify_client_origin(request)
        service = conversation_service()
        account_id = logged_in(request) if hasattr(request, 'cookies') else None
        defaults = await new_defaults(service, account_id, normalized_project, cwd)
        draft = service.create_draft(
            cwd=cwd, project_id=normalized_project, client_origin=origin,
            **defaults,
            owner_account_id=(logged_in(request) if hasattr(request, "cookies") else None),
        )
    except (AppServerError, OSError, ValueError) as error:
        return html(
            "无法新建对话",
            nav() + "<h1>无法新建对话</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/home'>返回首页</a>",
        )
    query = []
    if prompt:
        query.append("prefill=" + quote(prompt))
    if quoted:
        query.append("quoted=1")
    suffix = "?" + "&".join(query) if query else ""
    return RedirectResponse("/conversation/" + draft["id"] + suffix, 303)


@router.get("/conversation/{conversation_id}")
async def conversation_page(
    request: Request,
    conversation_id: str,
    uploaded: str = "",
    uploaded_batch: str = "",
    attachment_batch: str = "",
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    service = conversation_service()
    try:
        snapshot = service.public_browser_view(
            conversation_id, include_text_previews=True
        )
        context = service.upload_context(conversation_id)
    except (AppServerError, OSError, ValueError):
        return html(
            "对话不存在",
            nav() + "<h1>对话不存在</h1><a href='/home'>返回首页</a>",
        )
    account_id = logged_in(request)
    if account_id:
        try:
            service.mark_read(conversation_id, account_id)
        except (AppServerError, OSError, ValueError):
            # A read receipt is secondary state.  Never turn an otherwise
            # usable conversation into a false "not found" page.
            LOGGER.warning(
                "conversation_read_receipt_failed conversation=%s",
                conversation_id,
                exc_info=True,
            )
    try:
        catalog = await service.catalog(context["cwd"])
        catalog_error = False
    except (AppServerError, OSError, ValueError) as error:
        LOGGER.warning(
            "conversation_catalog_unavailable conversation=%s error=%s",
            conversation_id,
            type(error).__name__,
        )
        catalog = {}
        catalog_error = True
    conversation = snapshot["conversation"]
    project_name = "临时对话"
    upload_directory = context["cwd"]
    if conversation.get("project_id"):
        try:
            import projects

            project_name = projects.get(conversation["project_id"])["name"]
            upload_directory = projects.upload_directory(
                conversation["project_id"]
            )
        except (OSError, ValueError, KeyError):
            return html(
                "项目不可用",
                nav() + "<h1>项目不可用</h1>"
                "<p class='error'>无法打开该项目的固定上传目录。</p>"
                "<a href='/projects/history'>返回历史</a>",
            )
    back_url = (
        "/project/" + quote(conversation["project_id"])
        if conversation.get("project_id") else "/home"
    )
    from shared.message_attachment_uploads import upload_context, initial_attachments
    attachment_upload_error = ""
    try:
        attachment_token, attachment_scope = upload_context(request, conversation_id=conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        # An attachment's owner restriction is not a new authorization gate
        # for an existing readable conversation/history or ordinary text turn.
        attachment_token, attachment_scope = "", None
        attachment_upload_error = "附件上传暂不可用，请使用所属账号或重新打开页面；历史和文字对话仍可使用。"
        LOGGER.warning("conversation_attachment_upload_unavailable conversation=%s error=%s",
                       conversation_id, type(error).__name__)

    content = templates.get_template("conversation.html").render(
        conversation=conversation,
        initial_snapshot=snapshot,
        back_url=back_url,
        project_name=project_name,
        nav_html=nav(),
        upload_button_html=upload_button("conversation-upload") if not attachment_upload_error else "",
        upload_panel_html=upload_panel(
            upload_directory, "/conversation/" + conversation_id,
            "conversation-upload",
            attachment_context=attachment_token,
        ) if not attachment_upload_error else "",
        upload_notice_html=upload_notice(uploaded, uploaded_batch),
        models=catalog_models(catalog, conversation.get("settings") or {}),
        skills=catalog_skills(catalog),
        settings=conversation.get("settings") or {},
        catalog_error=catalog_error,
        attachment_initial=initial_attachments(service, attachment_batch, attachment_scope),
        attachment_owner=account_id or "",
        attachment_upload_error=attachment_upload_error,
    )
    response = html(conversation["title"] or "Codex 对话", content)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _existing_first_request(service, command_id, owner, request_input):
    with service.store._lock:
        exists = service.store._db.execute("SELECT 1 FROM commands WHERE id=?", (command_id,)).fetchone()
        if not exists:
            return None
        command = service.store.command(command_id)
        conversation = service.store.get_conversation(command["conversation_id"])
        if (command["kind"] != "start" or conversation.get("owner_account_id") != owner
                or command.get("payload", {}).get("initial_request") != request_input):
            raise AppServerProtocolError("首问投递标识已被使用且内容不同，请先核对原对话。")
        return conversation["id"]


@router.post("/conversation/new")
async def create_and_start_conversation(
    request: Request,
    prompt: str = Form(""),
    project_id: str = Form(""),
    model: str = Form(""),
    effort: str | None = Form(None),
    permission: str | None = Form(None),
    service_tier: str | None = Form(None),
    collaboration_mode: str | None = Form(None),
    completion_notification: str | None = Form(None),
    skills: list[str] = Form([]),
    skills_explicit: str = Form(""),
    goal_objective: str = Form(""),
    goal_status: str = Form("active"),
    attachment_ids: list[str] = Form([]),
    client_command_id: str = Form(""),
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    normalized_project = project_id.strip() or None
    try:
        requested_goal = goal_objective.strip() if isinstance(goal_objective, str) else ""
        if requested_goal:
            from engine.conversation_goals import validate_intent
            validate_intent(requested_goal, goal_status)
            if goal_status not in {"active", "paused"}:
                raise AppServerProtocolError("新目标只能开始或暂停。")
        cwd = None
        if normalized_project:
            import projects

            cwd = projects.get(normalized_project)["path"]
        origin, _evidence, _policy = classify_client_origin(request)
        service = conversation_service()
        account_id = logged_in(request) if hasattr(request, 'cookies') else None
        client_command_id = client_command_id if isinstance(client_command_id, str) else ""
        request_input = None
        if client_command_id:
            service.store._validate_id(client_command_id, "command")
            # Persist the full first-form intent. Retrying an uncertain POST
            # returns its original conversation, never creates/sends a new one.
            request_input = {
                "prompt": prompt.strip(), "project_id": normalized_project,
                "attachments": attachment_ids if isinstance(attachment_ids, list) else [],
                "skills": skills if isinstance(skills, list) else [],
                "skills_explicit": skills_explicit if isinstance(skills_explicit, str) else "",
                "goal_objective": requested_goal,
                "goal_status": goal_status if isinstance(goal_status, str) else "active",
                **{key: value if isinstance(value, str) else None for key, value in {
                    "model": model, "effort": effort, "permission": permission,
                    "service_tier": service_tier, "collaboration_mode": collaboration_mode,
                    "completion_notification": completion_notification,
                }.items()},
            }
            existing = _existing_first_request(service, client_command_id, account_id, request_input)
            if existing:
                return RedirectResponse("/conversation/" + existing, 303)
        if isinstance(attachment_ids, list) and attachment_ids:
            # Reject foreign/missing refs before creating a new draft/thread.
            service.attachments.resolve(attachment_ids, owner=account_id,
                project_id=normalized_project)
        defaults = await new_defaults(service, account_id, normalized_project, cwd, {
            'model': model if isinstance(model, str) and model else None,
            'effort': effort if isinstance(effort, str) else None,
            'permission': permission if isinstance(permission, str) else None,
            'service_tier': service_tier if isinstance(service_tier, str) else None,
            'collaboration_mode': collaboration_mode if isinstance(collaboration_mode, str) else None,
            'completion_notification': _checked(completion_notification) if isinstance(completion_notification, str) else None,
            'skills': skills if isinstance(skills, list) and (skills or skills_explicit == '1') else None,
        })
        if client_command_id:
            # Recheck after the catalog await; admission/start is synchronous
            # up to durable begin_command, so concurrent retries cannot start
            # a second native turn under the same command identity.
            existing = _existing_first_request(service, client_command_id, account_id, request_input)
            if existing:
                return RedirectResponse("/conversation/" + existing, 303)
        draft = service.create_draft(
            cwd=cwd,
            project_id=normalized_project,
            **defaults,
            client_origin=origin,
            owner_account_id=(logged_in(request) if hasattr(request, "cookies") else None),
        )
        if requested_goal:
            service.runtime.goals.save_draft(draft["id"], requested_goal, goal_status)
        result = await service.start(
            draft["id"],
            command_id=client_command_id or "m" + uuid.uuid4().hex,
            text=prompt,
            skill_selectors=skills if skills or skills_explicit == "1" else None,
            **({"attachment_ids": attachment_ids, "attachment_owner_id": account_id}
               if isinstance(attachment_ids, list) and attachment_ids else {}),
            **({"initial_request": request_input} if request_input is not None else {}),
        )
        command = result.get("command") if isinstance(result, dict) else None
        command_state = command.get("state") if isinstance(command, dict) else None
        if command_state not in {
            "accepted", "completed", "rejected", "delivery_unknown"
        }:
            raise AppServerError("conversation start returned an invalid command state")
        if command_state not in {"rejected", "delivery_unknown"} and normalized_project:
            import projects

            projects.touch(normalized_project, draft["id"], prompt)
    except (AppServerError, OSError, ValueError) as error:
        back = "/project/" + quote(normalized_project) if normalized_project else "/home"
        return html(
            "无法开始对话",
            nav() + "<h1>无法开始对话</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='" + back + "'>返回</a>",
        )
    if command_state == "rejected":
        audit(request, "conversation_start_rejected", draft["id"])
    elif command_state == "delivery_unknown":
        audit(request, "conversation_start_delivery_unknown", draft["id"])
    else:
        audit(request, "conversation_started", draft["id"])
    return RedirectResponse("/conversation/" + draft["id"], 303)
