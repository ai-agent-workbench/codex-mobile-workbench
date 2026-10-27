"""Project management backed by native App Server conversations."""

from __future__ import annotations

import html as html_escape
import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from engine.codex_app_server import AppServerError
from engine.conversation_service import conversation_service
from engine.conversation_search import ConversationSearchQueryError
from shared.history_search import initial_search, search_cards, search_controls
from shared.conversation_catalog import catalog_models, catalog_skills, new_conversation_model
from shared.workbench_settings import settings_store
from shared.conversation_views import conversation_cards, manage_bar, pagination_controls
from shared.html_lib import (
    html,
    management_script,
    static_url,
    nav,
    templates,
    upload_button,
    upload_notice,
    upload_panel,
)
from shared.conversation_pagination import ConversationPageCursorError
from shared.security import audit, logged_in, require_auth


router = APIRouter()
LOGGER = logging.getLogger(__name__)
_HISTORY_STATUS_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_HISTORY_STATUS_BATCH_MAX = 100


@router.get("/project/new")
def project_new_form(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return html(
        "新建项目",
        nav() + "<h1>新建项目</h1><form action='/project/new' method='post'>"
        "<label>项目名称</label><input name='name' required placeholder='例如：Web 工作台改进'>"
        "<label>模板</label><select name='template'>"
        "<option value='blank'>空白项目</option><option value='document'>文档项目</option>"
        "<option value='organize'>整理项目</option></select><button>创建项目</button></form>",
    )


@router.post("/project/new")
def project_new(
    request: Request,
    name: str = Form(...),
    template: str = Form("blank"),
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    try:
        project = projects.create(name, template)
    except ValueError as error:
        return html(
            "创建项目失败",
            nav() + "<h1>创建项目失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/project/new'>返回</a>",
        )
    audit(request, "project_created", project["id"])
    return RedirectResponse("/project/" + quote(project["id"]), 303)


@router.get("/projects")
def project_list(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    cards = [
        "<label class='manage-card' data-id='" + html_escape.escape(project["id"], quote=True) + "'>"
        "<input class='manage-check' type='checkbox' form='projects-manage' name='ids' value='"
        + html_escape.escape(project["id"], quote=True) + "'>"
        "<a class='card' href='/project/" + quote(project["id"]) + "'><b>"
        + html_escape.escape(project["name"]) + "</b></a></label>"
        for project in projects.recent()
    ]
    body = (
        nav() + "<h1>项目</h1><a class='button' href='/project/new'>＋ 新建项目</a>"
        "<section class='cards'>" + ("".join(cards) or "<p><small>还没有项目。</small></p>")
        + "</section>" + manage_bar("projects-manage", "/projects/manage")
        + management_script("projects-manage")
    )
    return html("项目", body)


@router.post("/projects/manage")
def projects_manage(
    request: Request,
    ids: list[str] = Form(...),
    action: str = Form(...),
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    known = {item["id"] for item in projects.recent()}
    selected = list(dict.fromkeys(value for value in ids if value in known))
    try:
        if action == "delete":
            conversation_service().delete_projects(selected, projects.delete)
        elif action in {"pin", "unpin"}:
            projects.update_order(selected, action == "pin")
        else:
            raise ValueError("操作无效。")
    except ValueError as error:
        return html(
            "无法整理项目",
            nav() + "<h1>无法整理项目</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/projects'>返回</a>",
        )
    audit(request, "projects_managed", action + ":" + str(len(selected)))
    return RedirectResponse("/projects", 303)


@router.get("/project/{project_id}")
async def project_view(request: Request, project_id: str, uploaded: str = "", q: str = "", search_cursor: str | None = None, uploaded_batch: str = "", attachment_batch: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    try:
        project = projects.get(project_id)
        service = conversation_service()
        page = service.list_public_page(
            project_id=project_id, account_id=logged_in(request),
        )
        values = page["values"]
    except (OSError, ValueError):
        return html(
            "项目不存在",
            nav() + "<h1>项目不存在</h1><a href='/home'>返回首页</a>",
        )
    form_id = "project-conversation-manage"
    project_url = "/project/" + quote(project_id)
    search_page, search_error = initial_search(
        service, q, account_id=logged_in(request), project_id=project_id, cursor=search_cursor,
    )
    resolved = settings_store().resolve(logged_in(request), project_id)
    default_settings = resolved['values']
    try:
        catalog = await service.catalog(project["path"])
        catalog_error = False
    except (AppServerError, OSError, ValueError) as error:
        LOGGER.warning(
            "project_catalog_unavailable project=%s error=%s",
            project_id,
            type(error).__name__,
        )
        catalog = {}
        catalog_error = True
    if resolved["sources"]["model"] == "system":
        default_settings["model"] = new_conversation_model(catalog)
    from shared.message_attachment_uploads import upload_context, initial_attachments
    attachment_token, attachment_scope = upload_context(request, project_id=project_id)
    composer = templates.get_template("project_conversation_composer.html").render(
        project_id=project_id,
        skills=catalog_skills(catalog),
        models=catalog_models(catalog, default_settings),
        settings=default_settings,
        catalog_error=catalog_error,
        upload_button_html=upload_button("project-upload"),
        upload_panel_html=upload_panel(
            projects.upload_directory(project_id), project_url, "project-upload",
            attachment_context=attachment_token,
        ),
        attachment_initial=initial_attachments(service, attachment_batch, attachment_scope),
        attachment_owner=logged_in(request) or "",
    )
    body = (
        nav() + "<h1>" + html_escape.escape(project["name"]) + "</h1>"
        + upload_notice(uploaded, uploaded_batch)
        + "<div class='project-quick-links' aria-label='项目快捷操作'><a href='/files?path="
        + quote(project["path"]) + "'>文件</a>"
        + "<a href='/edit?path=" + quote(project["path"] + "/进度与待办.md") + "'>待办</a>"
        + "<details class='project-quick-menu'><summary aria-label='更多项目操作'>⋯</summary><div>"
        + "<a href='/quick?project_id=" + quote(project_id) + "'>配置后开始</a>"
        + "<a href='/settings?project_id=" + quote(project_id) + "#models'>项目设置</a>"
        + "</div></details></div>"
        + composer
        + "<h2>项目对话</h2>"
        + search_controls(
            project_url, project_url + "/conversations/search", q, search_page,
            error=search_error,
        )
        + "<div data-history-browse" + (" hidden" if q.strip() else "") + ">"
        + "<section class='task-list history-list conversation-history' data-status-endpoint='"
        + project_url + "/conversations/status' data-page-endpoint='"
        + project_url + "/conversations/page' data-order-version='"
        + html_escape.escape(page.get("order_version", ""), quote=True)
        + "' aria-label='项目对话列表'>"
        + (conversation_cards(
            values, form_id, group_dates=True, visual_status=True,
        ) or "<p><small>还没有对话。</small></p>")
        + "</section>"
        + pagination_controls(
            project_url + "/conversations/page", page["next_cursor"], group_dates=True,
        )
        + manage_bar(form_id, project_url + "/manage")
        + "</div>"
        + management_script(form_id)
        + "<script src='" + static_url("/static/conversation-pagination.js") + "' defer></script>"
        + "<script src='" + static_url("/static/history-live.js") + "' defer></script>"
        + "<script src='" + static_url("/static/history-search.js") + "' defer></script>"
    )
    response = html(project["name"], body)
    response.headers["Cache-Control"] = "private, no-store"
    if search_error:
        response.status_code = 400
    return response


@router.get("/project/{project_id}/conversations/search")
def project_conversation_search(
    request: Request, project_id: str, q: str = "", cursor: str | None = None,
):
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        return JSONResponse(
            {"error": "登录已失效，请重新登录。", "code": "auth_required"},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    import projects

    try:
        projects.get(project_id)
    except (OSError, ValueError):
        return JSONResponse(
            {"error": "项目不存在。", "code": "project_missing"},
            status_code=404, headers={"Cache-Control": "no-store"},
        )
    try:
        page = conversation_service().search_public_page(
            q, account_id=logged_in(request), project_id=project_id, cursor=cursor,
        )
    except (ConversationSearchQueryError, ConversationPageCursorError) as error:
        return JSONResponse(
            {"error": str(error), "code": error.code},
            status_code=400 if isinstance(error, ConversationSearchQueryError) else 409,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({
        "html": search_cards(page["values"]), "next_cursor": page["next_cursor"],
    }, headers={"Cache-Control": "private, no-store"})


@router.get("/project/{project_id}/conversations/page")
def project_conversation_page(request: Request, project_id: str, cursor: str | None = None):
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        return JSONResponse(
            {"error": "登录状态无效，请重新登录。", "code": "auth_required"},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    import projects
    try:
        projects.get(project_id)
        page = conversation_service().list_public_page(
            project_id=project_id, account_id=logged_in(request), cursor=cursor,
        )
    except ConversationPageCursorError as error:
        return JSONResponse(
            {"error": str(error), "code": error.code}, status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    except (OSError, ValueError):
        return JSONResponse(
            {"error": "项目不存在。", "code": "project_missing"}, status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({
        "html": conversation_cards(
            page["values"], "project-conversation-manage", group_dates=True,
            visual_status=True,
        ),
        "next_cursor": page["next_cursor"],
        "order_version": page.get("order_version", ""),
        "pagination_html": pagination_controls(
            "/project/" + quote(project_id) + "/conversations/page",
            page["next_cursor"], group_dates=True,
        ),
    }, headers={"Cache-Control": "no-store"})


@router.get("/project/{project_id}/conversations/status")
def project_conversation_status(
    request: Request,
    project_id: str,
    ids: list[str] | None = Query(default=None),
    check_order: bool = False,
):
    """Render live-state fragments only for cards in this project's scope."""
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        return JSONResponse(
            {"error": "登录状态无效，请重新登录。", "code": "auth_required"},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    if (not ids and not check_order) or (ids and len(ids) > _HISTORY_STATUS_BATCH_MAX) or any(
        not _HISTORY_STATUS_ID_RE.fullmatch(value) for value in (ids or [])
    ):
        return JSONResponse(
            {"error": "状态查询参数无效。", "code": "invalid_ids"},
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    import projects
    try:
        projects.get(project_id)
    except (OSError, ValueError):
        return JSONResponse(
            {"error": "项目不存在。", "code": "project_missing"},
            status_code=404, headers={"Cache-Control": "no-store"},
        )
    values = conversation_service().public_index_by_ids(
        ids or [], account_id=logged_in(request), project_id=project_id,
    )
    payload = {
        "html": conversation_cards(
            values, "project-conversation-manage", visual_status=True,
        ),
    }
    if check_order:
        payload["order_version"] = conversation_service().public_order_version(
            account_id=logged_in(request), project_id=project_id,
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.post("/project/{project_id}/manage")
async def project_conversation_manage(
    request: Request,
    project_id: str,
    ids: list[str] = Form(...),
    action: str = Form(...),
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    try:
        projects.get(project_id)
    except (OSError, ValueError):
        return html("项目不存在", nav() + "<h1>项目不存在</h1><a href='/home'>返回首页</a>")
    service = conversation_service()
    selected = service.scoped_public_ids(
        ids, project_id=project_id, account_id=logged_in(request),
    )
    try:
        if action == "delete":
            await service.delete_many(selected)
        elif action in {"pin", "unpin"}:
            service.set_pinned(selected, action == "pin")
        else:
            raise ValueError("操作无效。")
    except (AppServerError, OSError, ValueError) as error:
        return html(
            "无法整理对话",
            nav() + "<h1>无法整理对话</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/project/"
            + quote(project_id) + "'>返回</a>",
        )
    audit(request, "project_conversations_managed", action + ":" + str(len(selected)))
    return RedirectResponse("/project/" + quote(project_id), 303)
