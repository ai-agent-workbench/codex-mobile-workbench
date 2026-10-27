"""Administrator-only Passkey login, explicit TOTP recovery, and logout."""
import time

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from shared.config import ACCOUNT, ENABLED_FILE, PASSKEY_ENABLED, MANAGED_INSTALL
import shared.html_lib as hlib
from shared.html_lib import html
from shared.return_to import safe_conversation_return_to
from shared.login_recovery import (
    NO_STORE, authentication_required, canonical_login_redirect, legacy_request_is_same_origin,
)
from shared.security import (
    audit, check_account_locked, clear_failed_logins, load_accounts, logged_in,
    record_failed_login, signer, totp,
)

router = APIRouter()


# ── TOTP setup routes (unchanged logic) ───────────────────────────────────

@router.get("/setup")
def setup():
    if MANAGED_INSTALL:
        return PlainTextResponse("请在这台电脑的本地面板完成实例初始化，再用手机进行本人验证。", 403, headers=NO_STORE)
    if ENABLED_FILE.exists():
        return RedirectResponse("/login", 303)
    uri = totp().provisioning_uri(name=ACCOUNT, issuer_name="Codex Workstation")
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    return html("\u7ed1\u5b9a\u8eab\u4efd\u9a8c\u8bc1\u5668",
        "<h1>\u9996\u6b21\u7ed1\u5b9a\u817e\u8baf\u8eab\u4efd\u9a8c\u8bc1\u5668</h1>"
        "<p>\u626b\u63cf\u4e8c\u7ef4\u7801\u540e\u8f93\u5165\u5f53\u524d 6 \u4f4d\u9a8c\u8bc1\u7801\u3002</p>"
        "<div class='qr'>" + image.to_string(encoding="unicode") + "</div>"
        "<p>\u65e0\u6cd5\u626b\u63cf\u65f6\uff0c\u5728\u817e\u8baf\u8eab\u4efd\u9a8c\u8bc1\u5668\u4e2d\u9009\u62e9\u201c\u624b\u52a8\u8f93\u5165\u5bc6\u94a5\u201d\uff1a\u8d26\u6237\u540d\u4e3a <b>"
        + ACCOUNT + "</b>\uff0c\u7c7b\u578b\u4e3a\u57fa\u4e8e\u65f6\u95f4\uff08TOTP\uff09\uff0c\u5bc6\u94a5\u4e3a\uff1a</p>"
        "<input value='" + totp().secret + "' readonly>"
        "<form action='setup' method='post'>"
        "<input name='code' inputmode='numeric' autocomplete='one-time-code' "
        "pattern='[0-9]{6}' maxlength='6' placeholder='6 \u4f4d\u9a8c\u8bc1\u7801' required>"
        "<button>\u9a8c\u8bc1\u5e76\u542f\u7528</button></form>",
    )


@router.post("/setup")
def finish_setup(request: Request, code: str = Form(...)):
    if MANAGED_INSTALL:
        return PlainTextResponse("此实例只允许本机初始化；未修改登录配置。", 403, headers=NO_STORE)
    if not totp().verify(code, valid_window=1):
        audit(request, "totp_setup_failed")
        return html("\u7ed1\u5b9a\u5931\u8d25",
            "<h1>\u9a8c\u8bc1\u7801\u65e0\u6548</h1>"
            "<p class='error'>\u8bf7\u786e\u8ba4\u624b\u673a\u65f6\u95f4\u6b63\u786e\u540e\u91cd\u8bd5\u3002</p>"
            "<a href='setup'>\u8fd4\u56de\u4e8c\u7ef4\u7801</a>",
        )
    ENABLED_FILE.touch(mode=0o600, exist_ok=True)
    from account_migration import migrate_accounts
    migrate_accounts()
    audit(request, "totp_enabled")
    return RedirectResponse("/login", 303)


# ── Normal login is Passkey; the old password/TOTP path is explicit only. ──

@router.get("/login")
def login(request: Request, return_to: str = ""):
    canonical = canonical_login_redirect(request, return_to=return_to)
    if canonical is not None:
        return canonical
    if PASSKEY_ENABLED:
        from routes.passkey import passkey_page
        return passkey_page(request, return_to)
    return PlainTextResponse("指纹登录尚未启用，请使用管理员单独保存的应急入口。", 503, headers=NO_STORE)


@router.get("/login/required")
def login_required(request: Request):
    return authentication_required(request, gateway=True)


@router.get("/login/status")
def login_status(request: Request):
    from shared.passkey_auth import session_from_request
    account = logged_in(request)
    locked = check_account_locked(account, request.client.host if request.client else "")[0] if account else False
    method = "none"
    if account and not locked:
        method = "passkey" if session_from_request(request) else "legacy"
    return JSONResponse(
        {"authenticated": method != "none", "method": method, "login_url": "/login", "passkey_enabled": PASSKEY_ENABLED},
        headers=NO_STORE,
    )


@router.get("/login/legacy")
def legacy_login(request: Request, return_to: str = ""):
    canonical = canonical_login_redirect(request, path="/login/legacy", return_to=return_to)
    if canonical is not None:
        return canonical
    if not ENABLED_FILE.exists():
        return RedirectResponse("/setup", 303, headers=NO_STORE)
    target = safe_conversation_return_to(return_to)
    if logged_in(request):
        return RedirectResponse(target or "/home", 303, headers=NO_STORE)
    return HTMLResponse(
        hlib.templates.get_template("login.html").render(
            title="管理员应急登录", return_to=target or "",
        ), headers=NO_STORE,
    )


@router.post("/login")
def finish_login(
    request: Request,
    account: str = Form(""),
    password: str = Form(""),
    remember: str = Form(""),
    code: str = Form(""),
    return_to: str = Form(""),
):
    # Old employee forms cannot create cookies, even if they include a code.
    if account or password or remember:
        from routes.legacy_employee import _retired
        return _retired(request)
    # Never silently downgrade the ordinary Passkey entry to TOTP.
    return RedirectResponse("/login", 303, headers=NO_STORE)


@router.post("/login/legacy")
def finish_legacy_login(request: Request, code: str = Form(""), return_to: str = Form("")):
    if not ENABLED_FILE.exists():
        return RedirectResponse("/setup", 303, headers=NO_STORE)
    if not legacy_request_is_same_origin(request):
        return PlainTextResponse("请求来源无效", status_code=403, headers=NO_STORE)
    return _handle_admin_login(request, code, load_accounts(), safe_conversation_return_to(return_to))


def _handle_admin_login(request: Request, code: str, accounts: dict, return_to: str | None = None):
    """TOTP login."""
    admin_name = next((name for name, entry in accounts.items()
                       if entry.get("role") == "admin" and entry.get("enabled")), None)
    if not admin_name:
        return PlainTextResponse("管理员账号不可用", status_code=403, headers=NO_STORE)
    ip = request.client.host if request.client else ""
    locked, reason = check_account_locked(admin_name, ip)
    if locked:
        return PlainTextResponse(reason, status_code=403, headers=NO_STORE)
    if not code or not totp().verify(code, valid_window=1):
        record_failed_login(admin_name, ip)
        audit(request, "totp_login_failed")
        return HTMLResponse(
            "<h1>验证码无效</h1><a href='/login/legacy'>返回应急登录</a>",
            status_code=403, headers=NO_STORE,
        )
    clear_failed_logins(admin_name, ip)
    audit(request, "totp_login_succeeded", account=admin_name)
    token_payload = signer().dumps({
        "n": admin_name,
        "r": "admin",
        "ts": int(time.time()),
    })
    from shared.config import ADMIN_MAX_AGE
    response = RedirectResponse(return_to or "/home", 303, headers=NO_STORE)
    response.set_cookie(
        "codex_web_session", token_payload,
        max_age=ADMIN_MAX_AGE, secure=True, httponly=True, samesite="lax", path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request):
    account = logged_in(request)
    from shared.passkey_auth import (
        PASSKEY_SESSION_COOKIE, request_is_same_origin, revoke_request_session,
        session_id_from_request,
    )
    passkey_session_id = session_id_from_request(request)
    if passkey_session_id and not request_is_same_origin(request):
        audit(request, "passkey_logout_failed", "origin rejected", account=account)
        return PlainTextResponse("请求来源无效", status_code=403)
    revoked_session_id = revoke_request_session(request, "logout")
    if revoked_session_id:
        from routes.ws import close_passkey_sessions
        close_passkey_sessions((revoked_session_id,))
    audit(request, "logout", account=account)
    response = RedirectResponse("/login", 303)
    response.delete_cookie("codex_web_session", path="/")
    response.delete_cookie(
        PASSKEY_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict",
    )
    return response
