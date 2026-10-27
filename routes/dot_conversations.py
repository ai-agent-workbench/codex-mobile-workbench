"""Existing admin sessions, a dedicated dot mailbox, and no native RPCs.

There is deliberately no HTTP reply endpoint or machine authentication here.
Production local replies need the separate default-off relay flag and an
explicitly approved owner-only scope file outside the source tree.
"""

from __future__ import annotations

import threading
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from engine.dot_mailbox import (
    DotConversationService, DotMailboxDenied, DotMailboxDisabled,
    DotMailboxError, DotScope,
)
from shared.config import (
    DOT_CONVERSATION_ENABLED, DOT_MAILBOX_DB, DOT_OWNER_ACCOUNT_ID, DOT_PARENT_REF, DOT_DELIVERY_DB,
)
from shared.html_lib import html, nav, templates
from shared.security import logged_in, require_auth
from shared.request_origin import require_same_https_origin
from shared.web_push import WebPushError


router = APIRouter(prefix="/settings")
_service = None
_service_lock = threading.Lock()
_delivery = None
_delivery_lock = threading.Lock()
_HEADERS = {"Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"}


def dot_available(owner):
    if not DOT_CONVERSATION_ENABLED or not owner or owner != DOT_OWNER_ACCOUNT_ID:
        return False
    try:
        DotScope(owner,"dot_binding",DOT_PARENT_REF)
    except DotMailboxError:
        return False
    return True


def dot_service():
    global _service
    if not DOT_CONVERSATION_ENABLED or not dot_available(DOT_OWNER_ACCOUNT_ID):
        raise DotMailboxDisabled("dot conversation is not enabled or bound")
    with _service_lock:
        if _service is None:
            _service = DotConversationService(DOT_MAILBOX_DB,enabled=True)
    return _service


def dot_delivery():
    global _delivery
    from engine.dot_delivery import DotDeliveryStore
    with _delivery_lock:
        if _delivery is None:
            _delivery = DotDeliveryStore(DOT_DELIVERY_DB)
    return _delivery


def _error(error):
    return JSONResponse({"error":{"code":getattr(error,"code","storage_unavailable"),
                                  "message":str(error) if isinstance(error,DotMailboxError) else "dot mailbox unavailable"}},
                        status_code=getattr(error,"status",503),headers=_HEADERS)


def _access(request, *, mutation=False, json_api=False):
    response = require_auth(request)
    if response is not None:
        if json_api and response.headers.get("x-codex-auth-required") != "1":
            denied = response.status_code == 403
            response = JSONResponse({"error":{"code":"scope_denied" if denied else "authentication_required"}},
                                    status_code=403 if denied else 401,headers=_HEADERS)
        return response,None
    if not DOT_CONVERSATION_ENABLED:
        raise DotMailboxDisabled("dot conversation is not enabled")
    owner = logged_in(request)
    if not dot_available(owner):
        raise DotMailboxDenied("this account is not the selected dot owner")
    if mutation:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise DotMailboxDenied("cross-site dot mutations are denied")
        try:
            require_same_https_origin(request,allow_form_referer=True)
        except WebPushError:
            raise DotMailboxDenied("dot mutation origin does not match this workbench") from None
    return None,owner


@router.get("/dot")
async def dot_entry(request: Request):
    try:
        response,owner = _access(request)
        if response is not None:
            return response
        conversation = dot_service().existing(owner,DOT_PARENT_REF)
        if conversation is not None:
            return RedirectResponse("/settings/dot/"+quote(conversation["id"]),303,headers=_HEADERS)
        response = html("dot",nav()+"<h1>dot</h1><p>dot 读取后，回复保存在这里。</p>"
                        "<form action='/settings/dot/open' method='post'><button>打开 dot 对话</button></form>")
        response.headers.update(_HEADERS)
        return response
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.post("/dot/open")
async def open_dot(request: Request):
    try:
        response,owner = _access(request,mutation=True)
        if response is not None:
            return response
        conversation = dot_service().open(owner,DOT_PARENT_REF)
        return RedirectResponse("/settings/dot/"+quote(conversation["id"]),303,headers=_HEADERS)
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.get("/dot/status")
async def dot_status(request: Request):
    try:
        response,owner = _access(request,json_api=True)
        if response is not None: return response
        service = dot_service()
        conversation = service.existing(owner,DOT_PARENT_REF)
        if conversation is None:
            return JSONResponse({"unread":False,"waiting":False,"reply_generation":0},headers=_HEADERS)
        cid=conversation["id"]
        summary=service.mailbox.delivery_summary(cid,owner,parent_dot_ref=DOT_PARENT_REF)
        return JSONResponse({"unread":summary["reply_generation"] > dot_delivery().seen(owner,cid),
            "waiting":summary["waiting"],"reply_generation":summary["reply_generation"]},headers=_HEADERS)
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.post("/dot/{conversation_id}/read")
async def dot_read_receipt(request: Request, conversation_id: str, generation: int = Form(...)):
    try:
        response,owner = _access(request,mutation=True,json_api=True)
        if response is not None: return response
        summary=dot_service().mailbox.delivery_summary(conversation_id,owner,parent_dot_ref=DOT_PARENT_REF)
        seen=dot_delivery().mark_read(owner,conversation_id,generation,latest=summary["reply_generation"])
        return JSONResponse({"seen_reply_generation":seen},headers=_HEADERS)
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.get("/dot/{conversation_id}")
async def dot_page(request: Request, conversation_id: str):
    try:
        response,owner = _access(request)
        if response is not None:
            return response
        snapshot = dot_service().public_snapshot(conversation_id,owner,parent_dot_ref=DOT_PARENT_REF)
        content = templates.get_template("conversation.html").render(
            conversation=snapshot["conversation"],initial_snapshot=snapshot,
            back_url="/projects/history",project_name="dot",nav_html=nav(),
            settings={},models=[],skills=[],catalog_error=False,
            upload_notice_html="",upload_button_html="",upload_panel_html="",
        )
        response = html("dot",content)
        response.headers.update(_HEADERS)
        return response
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.get("/dot/{conversation_id}/snapshot")
async def dot_snapshot(request: Request, conversation_id: str):
    try:
        response,owner = _access(request,json_api=True)
        if response is not None:
            return response
        return JSONResponse(dot_service().public_snapshot(conversation_id,owner,parent_dot_ref=DOT_PARENT_REF),headers=_HEADERS)
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)


@router.post("/dot/{conversation_id}/messages")
async def dot_message(request: Request, conversation_id: str,
                      message_id: str = Form(...), text: str = Form(...),
                      expected_revision: int = Form(...)):
    try:
        response,owner = _access(request,mutation=True,json_api=True)
        if response is not None:
            return response
        service = dot_service()
        service.send(conversation_id,owner,message_id=message_id,text=text,
                     expected_revision=expected_revision,origin="workbench_user",parent_dot_ref=DOT_PARENT_REF)
        return JSONResponse(service.public_snapshot(conversation_id,owner,parent_dot_ref=DOT_PARENT_REF),headers=_HEADERS)
    except (DotMailboxError,OSError,sqlite3.Error) as error:
        return _error(error)
