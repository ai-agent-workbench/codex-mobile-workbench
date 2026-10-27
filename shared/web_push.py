"""Small, deliberately defensive Web Push boundary.

The browser-facing routes validate all untrusted input before it reaches this
module.  This module separately validates subscriptions before delivery so a
corrupt database row can never turn the service into an arbitrary HTTP client.
No private key bytes are logged or returned by its public functions.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shared.config import WEB_PUSH_VAPID_PRIVATE_KEY

_FCM_HOST = "fcm.googleapis.com"
_MAX_ENDPOINT = 2048
_MAX_PAYLOAD = 3072
_MAX_PREVIEW_CHARS = 48


class WebPushError(ValueError):
    """A safe Web Push configuration or subscription error."""


def _b64url_decode(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WebPushError("invalid " + label)
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise WebPushError("invalid " + label)
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise WebPushError("invalid " + label) from exc


def canonical_https_origin(value: str) -> str:
    """Accept only a literal HTTPS web origin, never a path or credentials."""
    if not isinstance(value, str) or len(value) > 300 or any(
        ord(char) <= 32 or ord(char) == 127 or char == '\\' for char in value
    ):
        raise WebPushError("invalid origin")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise WebPushError("invalid origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise WebPushError("invalid origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebPushError("invalid origin") from exc
    host = parsed.hostname.lower()
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise WebPushError("invalid origin") from exc
    if host.endswith(".") or parsed.netloc.endswith(':') or port == 0:
        raise WebPushError("invalid origin")
    if ':' in host:
        try:
            host = '[' + str(ipaddress.IPv6Address(host)) + ']'
        except ValueError as exc:
            raise WebPushError('invalid origin') from exc
    elif any(not (char.isalnum() or char in '.-') for char in host):
        raise WebPushError('invalid origin')
    return "https://" + host + (":" + str(port) if port and port != 443 else "")


def validate_subscription(value: Any) -> dict[str, str]:
    """Validate the Android Chrome/FCM subscription envelope without fetching it."""
    if not isinstance(value, dict):
        raise WebPushError("invalid subscription")
    endpoint = value.get("endpoint")
    keys = value.get("keys")
    if not isinstance(endpoint, str) or not isinstance(keys, dict) or len(endpoint) > _MAX_ENDPOINT:
        raise WebPushError("invalid subscription")
    if any(ord(char) < 32 for char in endpoint):
        raise WebPushError("invalid subscription")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise WebPushError("invalid subscription") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _FCM_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/fcm/send/")
        or len(parsed.path) <= len("/fcm/send/")
    ):
        raise WebPushError("unsupported push endpoint")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    public = _b64url_decode(p256dh, "p256dh")
    secret = _b64url_decode(auth, "auth")
    if len(public) != 65 or public[:1] != b"\x04" or len(secret) != 16:
        raise WebPushError("invalid subscription keys")
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


def _read_private_key(path: Path = WEB_PUSH_VAPID_PRIVATE_KEY) -> bytes:
    """Read a service-owned regular 0600 PEM without a symlink race."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise WebPushError("web push signing key cannot be opened safely")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise WebPushError("web push signing key is not configured") from exc
    except OSError as exc:
        # O_NOFOLLOW reports ELOOP for a symlink; keep the public diagnostic
        # intentionally non-specific.
        raise WebPushError("web push signing key is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WebPushError("web push signing key has an unsafe type")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise WebPushError("web push signing key permissions are unsafe")
        chunks = []
        remaining = 8193
        while remaining:
            value = os.read(descriptor, min(4096, remaining))
            if not value:
                break
            chunks.append(value)
            remaining -= len(value)
        value = b"".join(chunks)
    except OSError as exc:
        raise WebPushError("web push signing key is unavailable") from exc
    finally:
        os.close(descriptor)
    if not (value.startswith(b"-----BEGIN PRIVATE KEY-----") or value.startswith(b"-----BEGIN EC PRIVATE KEY-----")) or len(value) > 8192:
        raise WebPushError("web push signing key is invalid")
    return value


def runtime_available() -> bool:
    """Check the optional sender dependency before accepting subscriptions."""
    try:
        import pywebpush  # noqa: F401
        import py_vapid  # noqa: F401
        import cryptography  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def _load_private_key(value: bytes):
    """Parse and validate one P-256 private key already read securely."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = serialization.load_pem_private_key(value, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise WebPushError("web push signing key must be P-256")
    except WebPushError:
        raise
    except Exception as exc:
        raise WebPushError("web push signing key is invalid") from exc
    return key


def public_key_b64url() -> str:
    """Return the VAPID P-256 public point, not the private key."""
    try:
        from cryptography.hazmat.primitives import serialization
    except (ImportError, OSError) as exc:
        raise WebPushError(
            "web push runtime dependency is not installed"
        ) from exc

    key = _load_private_key(_read_private_key())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")


def configured_public_key_b64url() -> str:
    """Return the public key only when the complete sender is usable."""
    if not runtime_available():
        raise WebPushError("web push runtime dependency is not installed")
    return public_key_b64url()


def _vapid_subject(origin: str) -> str:
    """Return a py-vapid-compatible HTTPS contact URI for this deployment."""
    canonical = canonical_https_origin(origin)
    parsed = urlsplit(canonical)
    host = parsed.hostname or ""
    # py-vapid's strict subject validator accepts an HTTPS hostname but not a
    # non-default port. The push audience still comes from the exact FCM
    # endpoint, while subscription origin validation retains the full port.
    return "https://" + host


def build_payload(*, title: str, final_text: str, conversation_id: str, turn_id: str) -> str:
    """Build bounded data with no URL supplied by a remote sender."""
    title_text = " ".join(str(title or "").split())[:80]
    text = " ".join(str(final_text or "").split())
    if len(text) > _MAX_PREVIEW_CHARS:
        text = text[: _MAX_PREVIEW_CHARS - 1].rstrip() + "…"
    payload = {
        "title": title_text or "回复已完成",
        "final_text": text or "请打开工作台查看详细结果。",
        "conversation_id": str(conversation_id),
        "turn_id": str(turn_id),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD:
        raise WebPushError("web push payload is too large")
    return encoded


def send(subscription: dict[str, Any], payload: str) -> tuple[str, str]:
    """Send one delivery.  The caller persists the returned safe state.

    Returns ``(sent|invalid|unknown, short_detail)``.  Network and 5xx outcomes
    intentionally remain unknown: re-sending could duplicate a browser alert.
    """
    # Store delivery rows keep the browser keys in separate columns. Rebuild
    # the browser envelope before applying the same strict boundary validation
    # used at subscription time.
    clean = validate_subscription({
        "endpoint": subscription.get("endpoint"),
        "keys": {
            "p256dh": subscription.get("p256dh"),
            "auth": subscription.get("auth"),
        },
    })
    origin = canonical_https_origin(str(subscription.get("origin") or ""))
    key = _read_private_key()
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > _MAX_PAYLOAD:
        raise WebPushError("web push payload is too large")
    if not runtime_available():
        raise WebPushError("web push runtime dependency is not installed")
    from pywebpush import WebPushException, webpush
    from py_vapid import Vapid
    vapid = Vapid(private_key=_load_private_key(key))
    try:
        # Pass a parsed VAPID object so pywebpush never has to reopen a path or
        # reinterpret raw key bytes. Imports remain lazy until actual delivery.
        response = webpush(
            subscription_info={"endpoint": clean["endpoint"], "keys": {
                "p256dh": clean["p256dh"], "auth": clean["auth"],
            }},
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": _vapid_subject(origin)},
            ttl=300,
            timeout=10,
        )
        status = int(getattr(response, "status_code", getattr(response, "status", 0)) or 0)
        if 200 <= status < 300:
            return "sent", "http_" + str(status)
        if status in {404, 410}:
            return "invalid", "http_" + str(status)
        return "unknown", "http_" + str(status) if status else "delivery_unknown"
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", getattr(response, "status", 0)) or 0)
        if status in {404, 410}:
            return "invalid", "http_" + str(status)
        return "unknown", "http_" + str(status) if status else "delivery_unknown"
    except Exception as exc:
        # Do not leak endpoint/key/network internals into the database or logs.
        return "unknown", type(exc).__name__[:80]
