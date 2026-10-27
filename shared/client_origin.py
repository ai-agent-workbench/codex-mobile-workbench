"""Non-authoritative browser-origin classification for UI diagnostics."""

_MOBILE_HINTS = ("android", "mobile", "iphone", "ipad", "ipod")


def classify_client_origin(request, explicit=""):
    """Return ``(origin, evidence, None)`` without changing authority."""
    explicit = str(explicit or "").strip().lower()
    if explicit in {"mobile", "mobile_web"}:
        return "mobile_web", "explicit_mobile", None
    if explicit in {"desktop", "desktop_web"}:
        return "desktop_web", "explicit_desktop", None
    headers = getattr(request, "headers", {}) or {}
    hint = str(headers.get("sec-ch-ua-mobile", "")).strip()
    agent = str(headers.get("user-agent", "")).lower()[:512]
    hint_mobile = hint == "?1"
    hint_desktop = hint == "?0"
    ua_mobile = any(token in agent for token in _MOBILE_HINTS)
    if hint_mobile or ua_mobile:
        if hint_desktop and ua_mobile:
            return "unknown", "conflicting_headers", None
        evidence = "client_hint_mobile" if hint_mobile else "mobile_user_agent"
        return "mobile_web", evidence, None
    if hint_desktop or agent:
        evidence = "client_hint_desktop" if hint_desktop else "desktop_user_agent"
        return "desktop_web", evidence, None
    return "unknown", "unknown", None
