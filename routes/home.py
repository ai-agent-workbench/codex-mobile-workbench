"""Home, history, and conversation-management routes."""

from __future__ import annotations

import html as html_escape
import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from engine.codex_app_server import AppServerError
from engine.conversation_service import conversation_service
from shared.conversation_views import conversation_cards, manage_bar, pagination_controls
from shared.html_lib import html, management_script, nav, static_url
from shared.conversation_pagination import ConversationPageCursorError
from shared.security import audit, logged_in, require_auth
from engine.conversation_search import ConversationSearchQueryError
from shared.history_search import initial_search, search_cards, search_controls
from routes.dot_conversations import dot_available


router = APIRouter()
HISTORY_PATH = "/projects/history"
HISTORY_MANAGE_PATH = HISTORY_PATH + "/manage"
# Temporarily hide dot navigation without deleting its routes or mailbox.
_DOT_ENTRY_VISIBLE = False
_HISTORY_STATUS_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_HISTORY_STATUS_BATCH_MAX = 100


@router.get("/")
@router.get("/home")
def home(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    account_id = logged_in(request) if request is not None else None
    conversations = conversation_service().list_public(
        limit=8,
        account_id=account_id,
    )
    recent_projects = projects.recent()
    project_names = {
        str(project.get("id") or ""): str(project.get("name") or "项目对话")
        for project in recent_projects
    }
    project_cards = [
        "<a class='card home-project-card' href='/project/" + quote(project["id"]) + "'>"
        "<b>" + html_escape.escape(project["name"]) + "</b>"
        "</a>"
        for project in recent_projects[:6]
    ]
    form_id = "home-conversation-manage"
    dot_card = (
        "<a class='card home-action-card' href='/settings/dot'><b>dot</b>"
        "</a>"
        if _DOT_ENTRY_VISIBLE and dot_available(account_id) else ""
    )
    body = (
        nav("home")
        + "<div class='home-page'><p class='home-kicker'>Codex Workspace</p>"
        "<header class='home-hero'><div><h1>今天想继续什么？</h1>"
        "</div>"
        "<span class='home-brand-dot' aria-hidden='true'></span></header>"
        "<section class='cards home-actions' aria-label='快捷入口'>"
        "<a class='card home-action-card' href='/project/new'>"
        "<span class='home-action-icon' aria-hidden='true'>＋</span><b>新建项目</b>"
        "</a>"
        "<a class='card home-action-card primary' href='/quick'>"
        "<span class='home-action-icon' aria-hidden='true'>✦</span><b>新对话</b>"
        "</a>" + dot_card + "</section>"
        "<div class='home-section-head'><h2>最近项目</h2><a href='/projects'>查看全部</a></div>"
        "<section class='cards home-projects'>"
        + ("".join(project_cards) or "<p><small>还没有项目。</small></p>")
        + "</section><div class='home-section-head'><h2>最近对话</h2>"
        "<a href='" + HISTORY_PATH + "'>查看全部</a></div>"
        "<section class='task-list history-list home-recent'>"
        + (conversation_cards(
            conversations, form_id, show_project=True,
            project_names=project_names, visual_status=True,
        )
           or "<p><small>还没有对话。</small></p>")
        + "</section></div>"
        + manage_bar(form_id, HISTORY_MANAGE_PATH)
        + management_script(form_id)
    )
    return html("Codex 工作台", body)


@router.get("/history")
@router.get("/projects/history")
def history(request: Request, q: str = "", search_cursor: str | None = None):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects

    page = conversation_service().list_public_page(
        account_id=logged_in(request) if request is not None else None,
    )
    values = page["values"]
    project_names = {
        str(project.get("id") or ""): str(project.get("name") or "项目对话")
        for project in projects.recent()
    }
    search_page, search_error = initial_search(
        conversation_service(), q,
        account_id=logged_in(request) if request is not None else None,
        cursor=search_cursor,
    )
    form_id = "history-conversation-manage"
    # A fixed navigation entry, outside native paging/status/management. The
    # mailbox ID must never reach native conversation pin/delete operations.
    dot_entry = (
        "<section class='conversation-history dot-history-entry' aria-label='dot 对话入口'>"
        "<section class='conversation-date-group'><h2>置顶</h2><div class='conversation-date-cards'>"
        "<article class='conversation-index-card' data-dot-history-card><span class='conversation-state-rail' aria-hidden='true'></span>"
        "<a class='card' href='/settings/dot'><span class='conversation-card-copy'>"
        "<span class='conversation-card-title'><span class='pin-mark'>固定</span><b>dot</b></span>"
        "<small>继续 dot 对话</small></span><span class='conversation-card-status'>打开</span>"
        "</a></article></div></section></section><script src='/static/dot-history.js' defer></script>"
        if _DOT_ENTRY_VISIBLE and dot_available(logged_in(request) if request is not None else None) else ""
    )
    body = (
        nav("history")
        + "<div class='history-page'>"
        + search_controls(
            HISTORY_PATH, HISTORY_PATH + "/search", q, search_page,
            error=search_error, project_names=project_names, show_project=True,
            compact=True,
        )
        + "<div data-history-browse" + (" hidden" if q.strip() else "") + ">"
        + dot_entry
        + "<section class='task-list history-list conversation-history' data-status-endpoint='/projects/history/status' data-page-endpoint='/projects/history/page' data-order-version='"
        + html_escape.escape(page.get("order_version", ""), quote=True)
        + "' aria-label='历史对话列表'>"
        + (conversation_cards(
            values, form_id, show_project=True,
            project_names=project_names, group_dates=True, visual_status=True,
        )
           or "<div class='history-empty-card'><b>还没有历史对话</b></div>")
        + "</section>"
        + pagination_controls("/projects/history/page", page["next_cursor"], group_dates=True)
        + manage_bar(form_id, HISTORY_MANAGE_PATH) + "</div></div>"
        + management_script(form_id)
        + "<script src='" + static_url("/static/conversation-pagination.js") + "' defer></script>"
        + "<script src='" + static_url("/static/history-live.js") + "' defer></script>"
        + "<script src='" + static_url("/static/history-search.js") + "' defer></script>"
    )
    response = html("历史对话", body)
    response.headers["Cache-Control"] = "private, no-store"
    if search_error:
        response.status_code = 400
    return response


@router.get("/history/search")
@router.get("/projects/history/search")
def history_search(request: Request, q: str = "", cursor: str | None = None):
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        return JSONResponse(
            {"error": "登录已失效，请重新登录。", "code": "auth_required"},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    try:
        page = conversation_service().search_public_page(
            q, account_id=logged_in(request), cursor=cursor,
        )
    except (ConversationSearchQueryError, ConversationPageCursorError) as error:
        return JSONResponse(
            {"error": str(error), "code": error.code},
            status_code=400 if isinstance(error, ConversationSearchQueryError) else 409,
            headers={"Cache-Control": "no-store"},
        )
    import projects

    project_names = {
        str(project.get("id") or ""): str(project.get("name") or "项目对话")
        for project in projects.recent()
    }
    return JSONResponse({
        "html": search_cards(page["values"], project_names=project_names, show_project=True),
        "next_cursor": page["next_cursor"],
    }, headers={"Cache-Control": "private, no-store"})


@router.get("/history/page")
@router.get("/projects/history/page")
def history_page(request: Request, cursor: str | None = None):
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
        page = conversation_service().list_public_page(
            account_id=logged_in(request), cursor=cursor,
        )
    except ConversationPageCursorError as error:
        return JSONResponse(
            {"error": str(error), "code": error.code}, status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    project_names = {
        str(project.get("id") or ""): str(project.get("name") or "项目对话")
        for project in projects.recent()
    }
    return JSONResponse({
        "html": conversation_cards(
            page["values"], "history-conversation-manage", show_project=True,
            project_names=project_names, group_dates=True, visual_status=True,
        ),
        "next_cursor": page["next_cursor"],
        "order_version": page.get("order_version", ""),
        "pagination_html": pagination_controls(
            "/projects/history/page", page["next_cursor"], group_dates=True,
        ),
    }, headers={"Cache-Control": "no-store"})


@router.get("/history/status")
@router.get("/projects/history/status")
def history_status(
    request: Request, ids: list[str] | None = Query(default=None), check_order: bool = False,
):
    """Render current visual-state fragments for cards already on history page."""
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        return JSONResponse(
            {"error": "登录状态无效，请重新登录。", "code": "auth_required"},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    if (not ids and not check_order) or (ids and len(ids) > _HISTORY_STATUS_BATCH_MAX):
        return JSONResponse(
            {"error": "状态查询数量无效。", "code": "invalid_ids"},
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    ids = ids or []
    if any(not _HISTORY_STATUS_ID_RE.fullmatch(value) for value in ids):
        return JSONResponse(
            {"error": "状态查询参数无效。", "code": "invalid_ids"},
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    import projects

    values = conversation_service().public_index_by_ids(
        ids, account_id=logged_in(request),
    )
    project_names = {
        str(project.get("id") or ""): str(project.get("name") or "项目对话")
        for project in projects.recent()
    }
    payload = {
        "html": conversation_cards(
            values, "history-conversation-manage", show_project=True,
            project_names=project_names, visual_status=True,
        ),
    }
    if check_order:
        payload["order_version"] = conversation_service().public_order_version(
            account_id=logged_in(request),
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.post("/history/manage")
@router.post("/projects/history/manage")
async def history_manage(
    request: Request,
    ids: list[str] = Form(...),
    action: str = Form(...),
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    service = conversation_service()
    selected = service.scoped_public_ids(ids, account_id=logged_in(request))
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
            + html_escape.escape(str(error))
            + "</p><a href='" + HISTORY_PATH + "'>返回</a>",
        )
    audit(request, "conversations_managed", action + ":" + str(len(selected)))
    return RedirectResponse(HISTORY_PATH, 303)
