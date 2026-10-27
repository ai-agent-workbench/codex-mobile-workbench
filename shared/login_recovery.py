"""Public, non-authenticating login recovery responses for the workbench."""
from urllib.parse import quote, urlsplit

from fastapi.responses import JSONResponse, RedirectResponse

from shared.config import PASSKEY_ENABLED, PASSKEY_ORIGIN
from shared.return_to import safe_conversation_return_to


NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def login_url(return_to=""):
    target = safe_conversation_return_to(return_to)
    return "/login" + ("?return_to=" + quote(target, safe="") if target else "")


def canonical_login_redirect(request, *, path="/login", return_to=""):
    """Keep origin-bound credentials on the configured host, never a supplied URL.

    Loopback is retained for startup/health checks. The proxy historically omits
    the external port from Host, so compare hostnames rather than reconstructing
    an origin from untrusted forwarding headers.
    """
    if not PASSKEY_ENABLED and path != "/login/legacy":
        return None
    expected = urlsplit(PASSKEY_ORIGIN)
    hostname = getattr(request.url, "hostname", None)
    if not hostname or hostname in {"localhost", "127.0.0.1", "::1"}:
        return None
    if (expected.scheme != "https" or not expected.hostname or expected.path not in {"", "/"}
            or expected.username or expected.password or expected.query or expected.fragment):
        return None  # The Passkey configuration gate returns the real error.
    if hostname.lower() == expected.hostname.lower():
        return None
    target = safe_conversation_return_to(return_to)
    suffix = "?return_to=" + quote(target, safe="") if target else ""
    return RedirectResponse(
        PASSKEY_ORIGIN.rstrip("/") + path + suffix, 303, headers=NO_STORE,
    )


def legacy_request_is_same_origin(request):
    """The explicit fallback keeps CSRF protection even when Passkey is off."""
    expected = urlsplit(PASSKEY_ORIGIN)
    if (expected.scheme != "https" or not expected.hostname or expected.username
            or expected.password or expected.query or expected.fragment
            or expected.path not in {"", "/"}):
        return False
    from shared.request_origin import privacy_form_is_same_origin
    if privacy_form_is_same_origin(request, PASSKEY_ORIGIN):
        return True
    origin = request.headers.get("origin", "")
    if origin:
        return origin == PASSKEY_ORIGIN.rstrip("/")
    referer = request.headers.get("referer", "")
    if not referer:
        return False
    parsed = urlsplit(referer)
    return parsed.scheme + "://" + parsed.netloc == PASSKEY_ORIGIN.rstrip("/")


def authentication_required(request, *, gateway=False):
    """Never authenticate, replay a body or turn an API denial into login HTML.

    The gateway overwrites the original-request headers and changes its internal
    recovery request to GET with an empty body. Direct callers may spoof these
    headers but can obtain only a safe same-origin redirect or a generic denial.
    403 is intentional: nginx preserves its old WWW-Authenticate header during
    error_page processing; returning another 401 would prompt for Basic again.
    """
    method = getattr(request, "method", "GET")
    original_uri = request.url.path
    if getattr(request.url, "query", ""):
        original_uri += "?" + request.url.query
    upgrade = request.headers.get("upgrade")
    if gateway:
        method = request.headers.get("x-codex-original-method", method)
        original_uri = request.headers.get("x-codex-original-uri", "")
        upgrade = request.headers.get("x-codex-original-upgrade", upgrade)
    target = login_url(original_uri)
    if (
        method == "GET"
        and "text/html" in request.headers.get("accept", "").lower()
        and not upgrade
        and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
    ):
        return RedirectResponse(target, 303, headers=NO_STORE)
    return JSONResponse(
        {
            "error": "登录已失效，请用指纹重新登录。",
            "reason_code": "authentication_required",
            "login_url": target,
        },
        status_code=403, headers={**NO_STORE, "X-Codex-Auth-Required": "1"},
    )
