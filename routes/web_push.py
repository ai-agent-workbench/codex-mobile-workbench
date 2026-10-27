"""Authenticated Web Push subscription endpoints.

These endpoints never receive an account identifier from the browser.  The
signed session determines ownership, and an endpoint already owned by another
account cannot be claimed by posting it again.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_service import conversation_service
from shared.security import audit, load_accounts, logged_in, require_auth
from shared.request_origin import require_same_https_origin
from shared.web_push import (
    WebPushError,
    configured_public_key_b64url,
    validate_subscription,
)

router = APIRouter()
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}
_MAX_BODY = 12 * 1024


def _admin(request: Request) -> tuple[str | None, Any | None]:
    denied = require_auth(request)
    if denied is not None:
        return None, denied
    account = logged_in(request)
    entry = load_accounts().get(account or "", {})
    if not account or entry.get("role") != "admin" or not entry.get("enabled"):
        return None, JSONResponse({"error": "当前账号无权管理浏览器通知。"}, status_code=403, headers=_HEADERS)
    return account, None


def _reply(value: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers=_HEADERS)


async def _body(request: Request) -> dict[str, Any]:
    raw_length = request.headers.get("content-length", "")
    try:
        if raw_length and int(raw_length) > _MAX_BODY:
            raise WebPushError("request too large")
    except ValueError as exc:
        raise WebPushError("invalid request") from exc
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise WebPushError("request too large")
    try:
        value = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebPushError("invalid request") from exc
    if not isinstance(value, dict):
        raise WebPushError("invalid request")
    return value


def _change_origin(request: Request) -> str:
    # Keep the subscription bound to the exact public HTTPS origin, including
    # its external port, independently of whether Passkey login is enabled.
    return require_same_https_origin(request)


@router.get("/settings/web-push/public-key")
def public_key(request: Request):
    account, denied = _admin(request)
    if denied is not None:
        return denied
    try:
        key = configured_public_key_b64url()
    except WebPushError as exc:
        return _reply({"error": str(exc)}, 503)
    return _reply({"public_key": key})


@router.post("/settings/web-push/subscribe")
async def subscribe(request: Request):
    account, denied = _admin(request)
    if denied is not None:
        return denied
    try:
        origin = _change_origin(request)
    except WebPushError as exc:
        return _reply({"error": str(exc)}, 400)
    try:
        # A direct or stale client must not register a subscription when this
        # server cannot actually sign and send it.
        configured_public_key_b64url()
    except WebPushError as exc:
        return _reply({"error": str(exc)}, 503)
    try:
        subscription = validate_subscription(await _body(request))
        record = conversation_service().store.upsert_web_push_subscription(
            account_id=account,
            origin=origin,
            **subscription,
        )
    except (WebPushError, ValueError) as exc:
        return _reply({"error": str(exc)}, 400)
    except AppServerProtocolError:
        return _reply({"error": "无法登记此浏览器订阅。"}, 409)
    audit(request, "web_push_subscription_enabled", account=account)
    return _reply({"enabled": bool(record["enabled"])})


@router.post("/settings/web-push/unsubscribe")
async def unsubscribe(request: Request):
    account, denied = _admin(request)
    if denied is not None:
        return denied
    try:
        _change_origin(request)
        subscription = validate_subscription(await _body(request))
        changed = conversation_service().store.disable_web_push_subscription(
            account_id=account, endpoint=subscription["endpoint"]
        )
    except (WebPushError, ValueError) as exc:
        return _reply({"error": str(exc)}, 400)
    except AppServerProtocolError:
        return _reply({"error": "无法停用此浏览器订阅。"}, 409)
    if changed:
        audit(request, "web_push_subscription_disabled", account=account)
    return _reply({"enabled": False})
