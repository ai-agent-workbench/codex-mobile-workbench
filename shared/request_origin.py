"""Strict mutation origins, including TLS-terminating gateways."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from shared import config
from shared.web_push import WebPushError, canonical_https_origin


def privacy_form_is_same_origin(request, expected_origin: str) -> bool:
    """Prove origin equality for privacy-redacted native browser forms only.

    no-referrer makes Chrome send Origin: null on a native navigation POST.
    Browser-controlled Fetch Metadata still compares full origins, including
    the public port which our gateway drops from Host. Never accept same-site,
    opaque/cross-site, fetch/JSON or WS on this alternative proof.
    """
    try:
        expected = urlsplit(canonical_https_origin(expected_origin))
        headers = request.headers
        if request.scope.get('type') != 'http' or request.scope.get('method') != 'POST':
            return False
        if headers.getlist('origin') not in ([], ['null']):
            return False
        if len(headers.getlist('host')) != 1 or request.url.hostname.lower() != expected.hostname:
            return False
        content_types = headers.getlist('content-type')
        if len(content_types) != 1 or content_types[0].split(';', 1)[0].strip().lower() not in {
            'application/x-www-form-urlencoded', 'multipart/form-data',
        }:
            return False
        return (headers.getlist('sec-fetch-site') == ['same-origin']
                and headers.getlist('sec-fetch-mode') == ['navigate']
                # Service Worker fetch(event.request) retains navigate/site,
                # but Chromium forwards the destination as "empty".
                and headers.getlist('sec-fetch-dest') in (['document'], ['iframe'], ['empty']))
    except (AttributeError, ValueError):
        return False


def require_same_https_origin(request, *, allow_form_referer=False, allow_navigation_referer=False) -> str:
    """Use the deployed public origin, never untrusted forwarding headers.

    The existing gateway drops the external port from Host. The configured
    origin is still authoritative when Passkey is disabled; it is a deployment
    address, not an authentication gate. Only an absent configuration permits
    direct HTTPS fallback. A malformed nonempty configuration fails closed.
    """
    expected = config.PASSKEY_ORIGIN
    if not expected:
        if len(request.headers.getlist('host')) != 1:
            raise WebPushError('origin rejected')
        base = request.base_url
        # Browsers send an HTTPS Origin on a secure WebSocket handshake.
        expected = str(base.replace(scheme='https' if base.scheme == 'wss' else base.scheme,
                                    path='', query='', fragment=''))
    expected = canonical_https_origin(expected)
    if allow_form_referer and privacy_form_is_same_origin(request, expected):
        return expected
    origins = request.headers.getlist('origin')
    if len(origins) > 1:
        raise WebPushError('origin rejected')
    if origins:
        origin = canonical_https_origin(origins[0])
    elif allow_navigation_referer or (allow_form_referer and request.headers.get('content-type', '').split(';', 1)[0].strip().lower() in {
        'application/x-www-form-urlencoded', 'multipart/form-data',
    }):
        # Explicit compatibility for forms/GET navigation only, never fallback
        # from invalid Origin or for JSON/WS requests.
        referers = request.headers.getlist('referer')
        if len(referers) != 1 or len(referers[0]) > 8192 or any(
            ord(char) <= 32 or ord(char) == 127 or char == '\\' for char in referers[0]
        ):
            raise WebPushError('origin rejected')
        parsed = urlsplit(referers[0])
        origin = canonical_https_origin(parsed.scheme + '://' + parsed.netloc)
    else:
        raise WebPushError('origin rejected')
    if not secrets.compare_digest(origin, expected):
        raise WebPushError('origin rejected')
    return origin
