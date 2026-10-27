"""Administrator Passkey authentication and compatible installed-PWA entry."""
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import shared.html_lib as hlib
from shared.config import (
    PASSKEY_CHALLENGE_MAX_AGE,
    PASSKEY_ENABLED,
    PASSKEY_FLOW_COOKIE,
    PASSKEY_SESSION_ABSOLUTE_AGE,
    PASSKEY_SESSION_COOKIE,
)
from shared.passkey_auth import (
    PasskeyError,
    active_credential_exists,
    begin_authentication,
    begin_registration,
    configuration_error,
    consume_registration_authorization,
    cookie_binding,
    finish_authentication,
    finish_registration,
    request_is_same_origin,
    session_from_request,
    source_fingerprint,
)
from shared.return_to import safe_conversation_return_to
from shared.security import audit, current_legacy_account, load_accounts, logged_in, totp

router = APIRouter()
_MAX_BODY = 128 * 1024
_CANDIDATE_ENTRY = re.compile(r"/[A-Za-z0-9_-]{16,160}(?:/recover)?")


def _gate():
    if not PASSKEY_ENABLED:
        return PlainTextResponse("Not Found", status_code=404)
    error = configuration_error()
    if error:
        return PlainTextResponse("Passkey 尚未配置", status_code=503)
    return None


def _legacy_admin(request: Request) -> str | None:
    account = current_legacy_account(request)
    if not account:
        return None
    entry = load_accounts().get(account, {})
    if entry.get("role") != "admin" or not entry.get("enabled"):
        return None
    return account


def _legacy_binding(request: Request) -> str | None:
    token = request.cookies.get("codex_web_session", "")
    return cookie_binding(token) if token else None


async def _json_body(request: Request) -> dict:
    raw_length = request.headers.get("content-length", "")
    try:
        if raw_length and int(raw_length) > _MAX_BODY:
            raise PasskeyError("request_too_large")
    except ValueError as exc:
        raise PasskeyError("invalid_request") from exc
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise PasskeyError("request_too_large")
    try:
        value = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PasskeyError("invalid_request") from exc
    if not isinstance(value, dict):
        raise PasskeyError("invalid_request")
    return value


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _set_flow_cookie(response: Response, selector: str) -> None:
    response.set_cookie(
        PASSKEY_FLOW_COOKIE, selector,
        max_age=PASSKEY_CHALLENGE_MAX_AGE,
        secure=True, httponly=True, samesite="strict", path="/",
    )


def _clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(
        PASSKEY_FLOW_COOKIE, secure=True, httponly=True, samesite="strict", path="/",
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        PASSKEY_SESSION_COOKIE, token,
        max_age=PASSKEY_SESSION_ABSOLUTE_AGE,
        expires=datetime.now(timezone.utc) + timedelta(seconds=PASSKEY_SESSION_ABSOLUTE_AGE),
        secure=True, httponly=True, samesite="lax", path="/",
    )


def _close_revoked(session_ids) -> None:
    if session_ids:
        from routes.ws import close_passkey_sessions
        close_passkey_sessions(session_ids)


def _error(request: Request, event: str, error: Exception, account: str | None = None):
    detail = error.code if isinstance(error, PasskeyError) else type(error).__name__
    audit(request, event, "reason=" + detail[:80], account=account)
    status_code = 429 if detail == "finish_rate_limited" else 400
    message = "请求过于频繁，请稍后再试。" if status_code == 429 else "验证失败，请重试。"
    response = _json({"error": message}, status_code)
    _clear_flow_cookie(response)
    return response


def _render_page(request: Request, mode: str, allow_authenticated: bool = False, return_to: str = ""):
    blocked = _gate()
    if blocked:
        return blocked
    target = safe_conversation_return_to(return_to)
    passkey_session = session_from_request(request)
    if passkey_session and logged_in(request) and not allow_authenticated:
        return RedirectResponse(target or "/home", 303)
    if mode == "auto":
        legacy = _legacy_admin(request)
        mode = "register" if legacy and not active_credential_exists(legacy) else "login"
    title = "绑定指纹" if mode in {"register", "recover"} else "管理员登录"
    action = "轻触绑定" if mode in {"register", "recover"} else "轻触验证"
    response = HTMLResponse(
        hlib.templates.get_template("passkey_login.html").render(
            title=title, mode=mode, action=action, return_to=target or "",
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _pwa_manifest(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    entry = request.query_params.get("entry", "")
    if entry != "/login" and not _CANDIDATE_ENTRY.fullmatch(entry):
        return PlainTextResponse("Not Found", status_code=404)
    if entry.endswith("/recover"):
        entry = entry[:-len("/recover")]
    content = {
        "id": "/auth/passkey-pwa",
        "name": "Codex 管理员工作台",
        "short_name": "Codex 管理员",
        "start_url": entry,
        "scope": "/",
        "display": "standalone",
        "background_color": "#111620",
        "theme_color": "#111620",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {
                "src": "/static/icon-maskable-192.png", "sizes": "192x192",
                "type": "image/png", "purpose": "maskable",
            },
            {
                "src": "/static/icon-maskable-512.png", "sizes": "512x512",
                "type": "image/png", "purpose": "maskable",
            },
        ],
    }
    return Response(
        json.dumps(content, ensure_ascii=False, indent=2),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/auth/passkey")
def passkey_page(request: Request, return_to: str = ""):
    if request.query_params.get("pwa") == "manifest":
        return _pwa_manifest(request)
    return _render_page(
        request, "auto", allow_authenticated=request.query_params.get("pwa") == "install", return_to=return_to,
    )


@router.get("/auth/passkey/recover")
def passkey_recovery_page(request: Request, return_to: str = ""):
    return _render_page(request, "recover", return_to=return_to)


@router.post("/auth/passkey/register/options")
async def registration_options(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    if not request_is_same_origin(request):
        return _error(request, "passkey_registration_failed", PasskeyError("origin_rejected"))
    account = _legacy_admin(request)
    binding = None
    try:
        body = await _json_body(request)
        if account:
            code = str(body.get("code", ""))
            if not code or not totp().verify(code, valid_window=1):
                raise PasskeyError("recent_authentication_required")
            binding = _legacy_binding(request)
        else:
            account = consume_registration_authorization(
                str(body.get("recoveryCode", "")),
            )
        start = begin_registration(account, binding)
        audit(request, "passkey_registration_started", account=account)
        response = _json(start.options)
        _set_flow_cookie(response, start.selector)
        return response
    except Exception as exc:
        return _error(request, "passkey_registration_failed", exc, account)


@router.post("/auth/passkey/register/finish")
async def registration_finish(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    if not request_is_same_origin(request):
        return _error(request, "passkey_registration_failed", PasskeyError("origin_rejected"))
    selector = request.cookies.get(PASSKEY_FLOW_COOKIE, "")
    try:
        body = await _json_body(request)
        credential = body.get("credential")
        if not isinstance(credential, dict):
            raise PasskeyError("invalid_credential")
        result = finish_registration(
            selector, credential, _legacy_binding(request), source_fingerprint(request),
        )
        audit(request, "passkey_registration_succeeded", account=result.account)
        response = _json({"ok": True, "redirect": safe_conversation_return_to(str(body.get("return_to", ""))) or "/home"})
        _clear_flow_cookie(response)
        _set_session_cookie(response, result.token)
        _close_revoked(result.revoked_session_ids)
        return response
    except Exception as exc:
        return _error(request, "passkey_registration_failed", exc)


@router.post("/auth/passkey/login/options")
async def authentication_options(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    if not request_is_same_origin(request):
        return _error(request, "passkey_login_failed", PasskeyError("origin_rejected"))
    try:
        await _json_body(request)
        start = begin_authentication()
        audit(request, "passkey_login_started")
        response = _json(start.options)
        _set_flow_cookie(response, start.selector)
        return response
    except Exception as exc:
        return _error(request, "passkey_login_failed", exc)


@router.post("/auth/passkey/login/finish")
async def authentication_finish(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    if not request_is_same_origin(request):
        return _error(request, "passkey_login_failed", PasskeyError("origin_rejected"))
    selector = request.cookies.get(PASSKEY_FLOW_COOKIE, "")
    try:
        body = await _json_body(request)
        credential = body.get("credential")
        if not isinstance(credential, dict):
            raise PasskeyError("invalid_credential")
        result = finish_authentication(selector, credential, source_fingerprint(request))
        audit(request, "passkey_login_succeeded", account=result.account)
        response = _json({"ok": True, "redirect": safe_conversation_return_to(str(body.get("return_to", ""))) or "/home"})
        _clear_flow_cookie(response)
        _set_session_cookie(response, result.token)
        _close_revoked(result.revoked_session_ids)
        return response
    except Exception as exc:
        return _error(request, "passkey_login_failed", exc)


@router.get("/internal/admin-session")
def internal_admin_session(request: Request):
    blocked = _gate()
    if blocked:
        return blocked
    client = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client).is_loopback:
            return PlainTextResponse("Forbidden", status_code=403)
    except ValueError:
        return PlainTextResponse("Forbidden", status_code=403)
    session = session_from_request(request)
    if not session:
        return Response(status_code=401)
    entry = load_accounts().get(session.account, {})
    if entry.get("role") != "admin" or not entry.get("enabled"):
        return Response(status_code=401)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
