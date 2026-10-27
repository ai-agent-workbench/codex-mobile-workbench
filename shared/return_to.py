"""Strict post-login destinations for notifications and history-search hits."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}"
_TARGET = re.compile(
    r"^/conversation/(" + _ID + r")\?(?:turn=(" + _ID + r")"
    r"(?:&item=(" + _ID + r"))?|command=(" + _ID + r"))$"
)


def safe_conversation_return_to(value: str | None) -> str | None:
    """Return only a typed canonical conversation message target or ``None``.

    Reject percent escapes as well as all URL authority/scheme syntax.  This
    avoids accepting a value that would become a different URL after a later
    decode/redirect layer.
    """
    if not isinstance(value, str) or not value or len(value) > 520:
        return None
    if "\\" in value or "%" in value or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    if not _TARGET.fullmatch(value):
        return None
    return value
