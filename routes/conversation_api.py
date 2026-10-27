"""Authenticated App Server conversation and artifact HTTP API."""

from __future__ import annotations

import json
from shared.diagnostics import record_exception
from typing import Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse

from engine.codex_app_server import AppServerError, AppServerProtocolError
from engine.conversation_service import conversation_service
from shared.client_origin import classify_client_origin
from shared.conversation_defaults import new_defaults
from shared.conversation_export import content_disposition_filename
from shared.html_lib import templates
from shared.security import audit, logged_in, require_auth


router = APIRouter()
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}
_HTML_PREVIEW_HEADERS = {
    **_PRIVATE_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data: blob:; "
        "media-src data: blob:; font-src data:; connect-src 'none'; "
        "object-src 'none'; frame-src 'none'; child-src 'none'; "
        "worker-src 'none'; manifest-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'; navigate-to 'none'; "
        "sandbox allow-scripts"
    ),
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "serial=(), clipboard-read=(), clipboard-write=()"
    ),
    "Referrer-Policy": "no-referrer",
}
_HTML_VIEWER_HEADERS = {
    **_PRIVATE_HEADERS,
    # The viewer is trusted, server-rendered chrome. The artifact itself stays
    # in its existing opaque, script-only iframe sandbox below.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; frame-src 'self'; child-src 'self'; "
        "img-src data:; object-src 'none'; connect-src 'none'; "
        "worker-src 'none'; manifest-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'; navigate-to 'self'"
    ),
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "serial=(), clipboard-read=(), clipboard-write=()"
    ),
    "Referrer-Policy": "no-referrer",
}


def _auth(request: Request, *, json_api: bool = True):
    response = require_auth(request)
    if response is None or not json_api or response.headers.get("x-codex-auth-required") == "1":
        return response
    forbidden = response.status_code == 403
    location = str(response.headers.get("location") or "")
    setup_required = not forbidden and location.rstrip("/").endswith("/setup")
    return JSONResponse(
        {
            "error": {
                "code": (
                    "authorization_denied" if forbidden else
                    "setup_required" if setup_required else
                    "authentication_required"
                ),
                "message": (
                    "当前账号无权执行此操作。"
                    if forbidden else
                    "工作台尚未完成初始化，请刷新页面继续设置。"
                    if setup_required else
                    "登录已失效，请重新登录；输入仍会保留。"
                ),
            }
        },
        status_code=403 if forbidden else 401,
        headers=_PRIVATE_HEADERS,
    )


def _error(error: Exception, status_code: int = 400) -> JSONResponse:
    record_exception("conversation_api_error", error, status=status_code)
    return JSONResponse(
        {"error": str(error)[:300]},
        status_code=status_code,
        headers=_PRIVATE_HEADERS,
    )


def _public_command(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in (
            "id", "kind", "state", "target_turn_id", "result_turn_id",
            "text", "reason", "origin", "created_at", "updated_at",
        )
    }
    for key in ('message_state', 'activation_command_id'):
        if key in value:
            result[key] = value[key]

    if value.get('kind') == 'interrupt' and value.get('payload', {}).get('scope') == 'conversation_tree':
        result['reason'] = 'tree_stop_unconfirmed' if value.get('state') == 'delivery_unknown' else 'tree_stop_rejected'
    return result


def _command_error(
    service: Any, conversation_id: str, result: dict[str, Any]
) -> JSONResponse | None:
    """Keep an uncertain or rejected command visible to the browser."""
    command = result.get("command") if isinstance(result, dict) else None
    state = command.get("state") if isinstance(command, dict) else None
    if state not in {"rejected", "delivery_unknown"}:
        return None
    return JSONResponse(
        {
            "error": {
                "code": "command_" + state,
                "message": (
                    ("计划已变化或当前无法实施，请同步后核对最新计划。"
                     if str(command.get("reason") or "").startswith("plan_") else
                     "首轮消息已送达，但目标启用被拒绝；输入已保留，请核对目标状态。"
                     if command.get('reason') == 'goal_activation_rejected' else
                     "目标已变化或无法执行，请重新打开目标面板核对；编辑内容仍保留。"
                     if command.get("kind") in {"goal", "goal_start"} or command.get("reason") == "goal_start_stale"
                     else "命令已被拒绝。")
                    if state == "rejected"
                    else "首轮消息已送达，但目标是否启用尚待核验；不会重发首轮消息。"
                    if command.get('reason') == 'goal_activation_unknown' else
                    "命令是否送达无法确认，请先重新核对对话状态。"
                ),
            },
            "command": _public_command(command),
            "snapshot": service.public_snapshot(conversation_id),
        },
        status_code=409,
        headers=_PRIVATE_HEADERS,
    )


def _checked(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


# Keep direct-backend compatibility, but publish links under the existing
# conversation gateway namespace. Extra segments avoid /conversation/{id}.
@router.get("/message-attachments", include_in_schema=False)
@router.get("/conversation/attachments/list")
def message_attachment_list(request: Request, ids: list[str] = Query([]),
                            conversation_id: str = "", project_id: str = ""):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        from shared.message_attachments import public_attachment
        service = conversation_service()
        owner = logged_in(request)
        conversation = service.store.get_conversation(conversation_id) if conversation_id else None
        if not conversation and not project_id:
            raise AppServerProtocolError("请选择附件所属对话或项目。")
        values, missing = [], []
        for attachment_id in dict.fromkeys(ids if isinstance(ids, list) else []):
            try:
                record = service.attachments.resolve([attachment_id], owner=owner,
                    conversation=conversation, project_id=project_id)[0]
                values.append(public_attachment(record, conversation_id=conversation_id or None))
            except (AppServerError, OSError, ValueError):
                missing.append(attachment_id)
        return JSONResponse({"attachments": values, "missing_ids": missing}, headers=_PRIVATE_HEADERS)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.get("/message-attachments/{attachment_id}", include_in_schema=False)
@router.get("/conversation/attachments/content/{attachment_id}")
def message_attachment_content(request: Request, attachment_id: str,
                               conversation_id: str = "", project_id: str = "",
                               download: bool = Query(False)):
    redirect = _auth(request, json_api=False)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        conversation = service.store.get_conversation(conversation_id) if conversation_id else None
        record = service.attachments.resolve([attachment_id], owner=logged_in(request),
            conversation=conversation, project_id=project_id)[0]
        kind = record["preview_kind"]
        if kind == "text" and download is not True:
            return PlainTextResponse(service.attachments.text_preview(record),
                headers=_PRIVATE_HEADERS, media_type="text/plain; charset=utf-8")
        inline = download is not True and kind in {"image", "pdf"}
        return FileResponse(service.attachments.path_for(record), filename=record["name"],
            media_type=record["mime_type"] if inline else "application/octet-stream",
            content_disposition_type="inline" if inline else "attachment", headers=_PRIVATE_HEADERS)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.post("/conversation/draft")
async def create_conversation_draft(
    request: Request,
    project_id: str = Form(""),
    model: str = Form(""),
    effort: str | None = Form(None),
    permission: str | None = Form(None),
    service_tier: str | None = Form(None),
    collaboration_mode: str | None = Form(None),
    completion_notification: str | None = Form(None),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        cwd = None
        normalized_project = project_id.strip() or None
        if normalized_project is not None:
            import projects

            cwd = projects.get(normalized_project)["path"]
        origin, _evidence, _policy = classify_client_origin(request)
        service = conversation_service()
        account_id = logged_in(request) if hasattr(request, 'cookies') else None
        defaults = await new_defaults(service, account_id, normalized_project, cwd, {
            'model': model if isinstance(model, str) and model else None,
            'effort': effort if isinstance(effort, str) else None,
            'permission': permission if isinstance(permission, str) else None,
            'service_tier': service_tier if isinstance(service_tier, str) else None,
            'collaboration_mode': collaboration_mode if isinstance(collaboration_mode, str) else None,
            'completion_notification': _checked(completion_notification) if isinstance(completion_notification, str) else None,
        })
        value = service.create_draft(
            cwd=cwd,
            project_id=normalized_project,
            **defaults,
            client_origin=origin,
            owner_account_id=(logged_in(request) if hasattr(request, "cookies") else None),
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_draft_created", value["id"])
    return JSONResponse(
        {
            "conversation": conversation_service().public_snapshot(
                value["id"]
            )["conversation"],
            "url": "/conversation/" + value["id"],
        },
        status_code=201,
        headers=_PRIVATE_HEADERS,
    )


@router.get("/conversation/{conversation_id}/snapshot")
async def conversation_snapshot(
    request: Request,
    conversation_id: str,
    text_previews: bool = Query(False),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        value = conversation_service().public_snapshot(
            conversation_id, include_text_previews=text_previews
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.get("/conversation/{conversation_id}/view")
async def conversation_browser_view(
    request: Request, conversation_id: str, before: str = "", pages: str = "",
    text_previews: bool = Query(False),
):
    """Explicit bounded browser projection; /snapshot remains complete."""
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        value = conversation_service().public_browser_view(conversation_id, before=before,
            pages=pages.split(",") if pages else [], include_text_previews=text_previews)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 409)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.get("/conversation/{conversation_id}/export")
def conversation_export(request: Request, conversation_id: str):
    """Download the persisted, browser-safe conversation transcript."""
    redirect = _auth(request, json_api=False)
    if redirect:
        return redirect
    try:
        value = conversation_service().export_transcript(conversation_id)
        return PlainTextResponse(
            value["markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={
                **_PRIVATE_HEADERS,
                "Content-Disposition": content_disposition_filename(value["title"]),
            },
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.post("/conversation/{conversation_id}/read")
async def mark_conversation_read(request: Request, conversation_id: str):
    redirect = _auth(request)
    if redirect:
        return redirect
    account_id = logged_in(request)
    if not account_id:
        return _error(ValueError("登录已失效，请重新登录。"), 401)
    try:
        conversation_service().mark_read(conversation_id, account_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)
    return JSONResponse({"read": True}, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/start")
async def start_conversation(
    request: Request,
    conversation_id: str,
    command_id: str = Form(...),
    text: str = Form(""),
    skills: list[str] = Form([]),
    skills_explicit: str = Form(""),
    attachment_ids: list[str] = Form([]),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.start(
            conversation_id,
            command_id=command_id,
            text=text,
            skill_selectors=skills if skills or skills_explicit == "1" else None,
            **({"attachment_ids": attachment_ids, "attachment_owner_id": logged_in(request)}
               if isinstance(attachment_ids, list) and attachment_ids else {}),
        )
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_started", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/messages")
async def send_conversation_message(
    request: Request,
    conversation_id: str,
    command_id: str = Form(...),
    text: str = Form(""),
    force_new_turn: str = Form(""),
    skills: list[str] = Form([]),
    skills_explicit: str = Form(""),
    attachment_ids: list[str] = Form([]),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.send(
            conversation_id,
            command_id=command_id,
            text=text,
            skill_selectors=skills if skills or skills_explicit == "1" else None,
            force_new_turn=_checked(force_new_turn) if isinstance(force_new_turn, str) and force_new_turn else None,
            **({"attachment_ids": attachment_ids, "attachment_owner_id": logged_in(request)}
               if isinstance(attachment_ids, list) and attachment_ids else {}),
        )
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_message_sent", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/plan/implement")
async def implement_conversation_plan(
    request: Request, conversation_id: str, command_id: str = Form(...),
    turn_id: str = Form(...), item_id: str = Form(...), plan_digest: str = Form(...),
    context_digest: str = Form(""), skills: list[str] = Form([]), skills_explicit: str = Form(""),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.implement_plan(conversation_id, command_id=command_id,
            turn_id=turn_id, item_id=item_id, plan_digest=plan_digest, context_digest=context_digest,
            skill_selectors=skills if skills or skills_explicit == "1" else None)
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_plan_implementation", conversation_id)
    return JSONResponse({"snapshot": value, "command": _public_command(result["command"])},
                        headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/goal/start")
async def start_conversation_goal(
    request: Request, conversation_id: str, command_id: str = Form(...),
    objective: str = Form(...), goal_version: str = Form(""),
    skills: list[str] = Form([]), skills_explicit: str = Form(""),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.start_goal(conversation_id, command_id=command_id,
            objective=objective, goal_version=goal_version,
            skill_selectors=skills if skills or skills_explicit == "1" else None)
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_goal_started", conversation_id)
    return JSONResponse({"snapshot": value, "command": _public_command(result["command"])},
                        headers=_PRIVATE_HEADERS)


@router.get("/conversation/{conversation_id}/actions/{command_id}")
async def conversation_action_receipt(request: Request, conversation_id: str, command_id: str):
    """Read one local receipt; never retry the native action after an unknown POST."""
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        try:
            command = service.store.command(command_id)
        except AppServerProtocolError:
            command = service.runtime.goals.store.command(command_id)
        if command["conversation_id"] != conversation_id:
            raise AppServerProtocolError("command does not belong to this conversation")
        from engine.conversation_actions import ConversationActions
        command = ConversationActions(service.runtime).action_command(conversation_id, command)
        return JSONResponse({"command": _public_command(command),
            "snapshot": service.public_snapshot(conversation_id)}, headers=_PRIVATE_HEADERS)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.post("/conversation/{conversation_id}/native-interactions/{interaction_id}/answer")
async def answer_native_interaction(
    request: Request, conversation_id: str, interaction_id: str,
    command_id: str = Form(...), attempt: int = Form(...),
    fingerprint: str = Form(...), response_json: str = Form(...),
):
    """Answer the exact live native callback; never audit its sensitive content."""
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        try:
            response = json.loads(response_json)
        except (TypeError, json.JSONDecodeError):
            return _error(ValueError("原生回答 JSON 格式无效。"))
        service = conversation_service()
        await service.answer_interaction(
            conversation_id, interaction_id=interaction_id, command_id=command_id,
            attempt=attempt, fingerprint=fingerprint, response=response,
            answered_by=logged_in(request),
        )
        value = service.public_snapshot(conversation_id)
    except PermissionError:
        return _error(ValueError("当前账号无权回答此原生请求。"), 403)
    except AppServerProtocolError:
        return _error(ValueError("原生请求状态已变化或回答不符合请求范围，请同步后再试。"), 409)
    except (AppServerError, OSError, ValueError, TypeError):
        return _error(ValueError("原生回答未确认送达，请同步核对；不会自动重发。"), 409)
    audit(request, "native_interaction_answered", conversation_id + ":" + interaction_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/questions/{question_id}/answer")
async def answer_conversation_question(
    request: Request,
    conversation_id: str,
    question_id: str,
    command_id: str = Form(...),
    answers_json: str = Form(...),
):
    """Submit one native structured-input response through the existing Form CSRF/auth path."""
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        try:
            answers = json.loads(answers_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("问题回答格式无效") from error
        service = conversation_service()
        await service.answer_question(
            conversation_id, question_id=question_id, command_id=command_id,
            answers=answers,
        )
        value = service.public_snapshot(conversation_id)
    except AppServerProtocolError:
        return _error(ValueError("问题状态已变化或回答无效，请同步后再试。"), 409)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    # Never audit question text or answers; the opaque local request ID is
    # sufficient for operational tracing.
    audit(request, "conversation_question_answered", conversation_id + ":" + question_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/async-questions/{request_id}/answer")
async def answer_async_conversation_question(
    request: Request,
    conversation_id: str,
    request_id: str,
    command_id: str = Form(...),
    text: str = Form(...),
):
    """Submit one durable AgentMessage async-input answer."""
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        await service.answer_async_question(
            conversation_id, request_id=request_id, command_id=command_id,
            answer_text=text,
        )
        value = service.public_snapshot(conversation_id)
    except AppServerProtocolError:
        return _error(ValueError("问题状态已变化或回答无效，请同步后再试。"), 409)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    # The request ID is sufficient for audit; never record an answer body.
    audit(request, "conversation_async_question_answered", conversation_id + ":" + request_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/interrupt")
async def interrupt_conversation(
    request: Request,
    conversation_id: str,
    command_id: str = Form(...),
    expected_turn_id: str = Form(""),
    scope: str = Form("root_turn"),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.interrupt(
            conversation_id,
            command_id=command_id,
            expected_turn_id=expected_turn_id,
            **({"scope": scope} if scope != "root_turn" else {}),
        )
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_interrupt_requested", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/reconcile")
async def reconcile_conversation(request: Request, conversation_id: str):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        await conversation_service().reconcile(conversation_id)
        value = conversation_service().public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_reconciled", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/settings")
async def update_conversation_settings(
    request: Request,
    conversation_id: str,
    command_id: str = Form(...),
    model: str | None = Form(None),
    effort: str | None = Form(None),
    permission: str | None = Form(None),
    service_tier: str | None = Form(None),
    collaboration_mode: str | None = Form(None),
    completion_notification: str | None = Form(None),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    updates: dict[str, Any] = {
        key: value
        for key, value in {
            "model": model,
            "effort": effort,
            "permission": permission,
            "collaboration_mode": collaboration_mode,
        }.items()
        if isinstance(value, str)
    }
    # FastAPI normalizes an empty form string to the optional field default.
    # Preserve the distinction between an omitted tier (leave it unchanged)
    # and an explicitly empty tier (switch back to Standard).
    service_tier_submitted = service_tier is not None
    if not service_tier_submitted:
        service_tier_submitted = "service_tier" in await request.form()
    if service_tier_submitted:
        updates["service_tier"] = service_tier or None
    try:
        service = conversation_service()
        result = await service.update_settings(
            conversation_id,
            command_id=command_id,
            updates=updates,
            completion_notification=(
                _checked(completion_notification)
                if completion_notification is not None else None
            ),
        )
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_settings_updated", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/metadata")
async def update_conversation_metadata(
    request: Request,
    conversation_id: str,
    title: str | None = Form(None),
    pinned: str | None = Form(None),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        value = await conversation_service().update_metadata(
            conversation_id,
            title=title,
            pinned=_checked(pinned) if pinned is not None else None,
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_metadata_updated", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/compact")
async def compact_conversation(
    request: Request,
    conversation_id: str,
    command_id: str = Form(...),
):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        result = await service.compact(
            conversation_id, command_id=command_id
        )
        rejected = _command_error(service, conversation_id, result)
        if rejected is not None:
            return rejected
        value = service.public_snapshot(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_compaction_requested", conversation_id)
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.post("/conversation/{conversation_id}/delete")
async def delete_conversation(request: Request, conversation_id: str):
    redirect = _auth(request)
    if redirect:
        return redirect
    try:
        await conversation_service().delete(conversation_id)
    except (AppServerError, OSError, ValueError) as error:
        return _error(error)
    audit(request, "conversation_deleted", conversation_id)
    return JSONResponse({"deleted": True}, headers=_PRIVATE_HEADERS)


@router.get("/conversation/{conversation_id}/artifact/{artifact_id}")
def conversation_artifact(
    request: Request,
    conversation_id: str,
    artifact_id: str,
    download: bool = Query(False),
):
    redirect = _auth(request, json_api=False)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        record = service.artifact_record(conversation_id, artifact_id)
        path = service.artifacts.path_for(record)
        preview_kind = record["preview_kind"]
        if preview_kind == "text" and not download:
            return PlainTextResponse(
                service.artifacts.text_preview(record),
                media_type="text/plain; charset=utf-8",
                headers=_PRIVATE_HEADERS,
            )
        inline = not download and preview_kind in {"image", "pdf"}
        return FileResponse(
            path,
            filename=record["name"],
            media_type=(
                record["mime_type"] if inline else "application/octet-stream"
            ),
            content_disposition_type="inline" if inline else "attachment",
            headers=_PRIVATE_HEADERS,
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.get("/conversation/{conversation_id}/artifact/{artifact_id}/preview")
def conversation_html_artifact_preview(
    request: Request,
    conversation_id: str,
    artifact_id: str,
    viewer: bool = Query(False),
):
    """Render only managed HTML inside a script-capable opaque sandbox."""
    redirect = _auth(request, json_api=False)
    if redirect:
        return redirect
    try:
        service = conversation_service()
        record = service.artifact_record(conversation_id, artifact_id)
        if not service.artifacts.html_previewable(record):
            raise ValueError("artifact is not HTML-previewable")
        # Direct unit calls receive FastAPI's Query object when the optional
        # argument is omitted; only the parsed boolean True selects the shell.
        if viewer is True:
            return HTMLResponse(
                templates.get_template("html_artifact_viewer.html").render(
                    artifact_name=record["name"],
                ),
                headers=_HTML_VIEWER_HEADERS,
            )
        path = service.artifacts.path_for(record)
        return FileResponse(
            path,
            filename=record["name"],
            media_type="text/html; charset=utf-8",
            content_disposition_type="inline",
            headers=_HTML_PREVIEW_HEADERS,
        )
    except (AppServerError, OSError, ValueError) as error:
        return _error(error, 404)


@router.get("/conversation/{conversation_id}/goal")
async def get_conversation_goal(request: Request, conversation_id: str):
    redirect = _auth(request)
    if redirect:
        return redirect
    service = conversation_service()
    try:
        await service.runtime.goals.refresh(conversation_id)
        return JSONResponse({"snapshot": service.public_snapshot(conversation_id)}, headers=_PRIVATE_HEADERS)
    except AppServerError as error:
        return _error(error, 409)


@router.post("/conversation/{conversation_id}/goal")
async def update_conversation_goal(request: Request, conversation_id: str,
        command_id: str = Form(...), action: str = Form(...),
        objective: str | None = Form(None), status: str | None = Form(None),
        token_budget: int | None = Form(None), goal_version: str = Form("")):
    redirect = _auth(request)
    if redirect:
        return redirect
    service = conversation_service()
    try:
        result = await service.runtime.goals.change(conversation_id, command_id=command_id,
            action=action, objective=objective, status=status, token_budget=token_budget,
            goal_version=goal_version)
        failure = _command_error(service, conversation_id, result)
        if failure:
            return failure
        return JSONResponse({"snapshot": service.public_snapshot(conversation_id),
                             "command": _public_command(result["command"])}, headers=_PRIVATE_HEADERS)
    except AppServerError as error:
        return _error(error)
