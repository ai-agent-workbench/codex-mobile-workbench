"""Private, structured diagnostics for the single-user workbench.

This module deliberately has no import-time filesystem or logging side effects.
Call :func:`configure_diagnostics` from ASGI startup only.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, parse_qsl, urlsplit, urlunsplit

from shared.config import STATE

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("diagnostic_request_id", default=None)
page_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("diagnostic_page_id", default=None)

# Values of these keys must never reach a diagnostic file.  The string rules
# cover exception messages and URLs where there is no structural key.
_SECRET_KEY = re.compile(r"(?:authorization|proxy[_-]?authorization|cookies?|set[_-]?cookie|password|passwd|passphrase|entry|[a-z_-]*(?:secret|token|credential|password)|api[_-]?key|ntfy(?:[_-].*)?|session(?:[_-]?id|[_-]?key)?)", re.I)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET = re.compile(r"(?i)(?:(?:^|[?&])(?:token|auth|password|secret|key|code|signature|session|credential)=[^&#\s]+|\b(?:token|auth(?:orization)?|password|passwd|secret|api[_-]?key|cookie)\s*[=:]\s*[^\s,;]+)")
_NTFY_URL = re.compile(r"(?i)(?:https?|wss?)://[^\s'\"]*ntfy[^\s'\"]*")
# Only a leading opaque entry is a credential. A hash in /files/<hash> or an
# ordinary query value is useful evidence, not automatically a secret.
_ENTRY_PATH = re.compile(r"(^|(?:https?|wss?)://[^\s/?#]+|\s(?:GET|POST|HEAD|PUT|DELETE|PATCH|OPTIONS)\s)/+(?:[A-Za-z0-9_-]|%[0-9a-f]{2}){24,}(?=/|[\s?#]|$)", re.I)
_QUERY_PAIR = re.compile(r"(^|[?&])([^\s?&#=]+)=([^\s&#]*)")
_QUERY_KEY = re.compile(r"(?:.*(?:token|secret|password|passwd|authorization|cookie|credential)|auth|apikey|key|code|signature|session(?:id|key)?|entry|ntfy.*)", re.I)
_URL = re.compile(r"(?:https?|wss?)://[^\s'\"<>]+",re.I)


def _decoded(value: str) -> str:
    for _ in range(3):
        decoded = unquote_plus(value)
        if decoded == value: break
        value = decoded
    return value


def _query_pair(match: re.Match) -> str:
    separator, raw_key, raw_value = match.groups()
    key = _decoded(raw_key).replace('_', '').replace('-', '')
    if _QUERY_KEY.fullmatch(key) or '%' in key or _sensitive_value(raw_value):
        return separator + raw_key + '=[REDACTED]'
    return match.group(0)


def _sensitive_value(value: str, depth: int = 0) -> bool:
    """Inspect nested URL/query semantics, never persist a decoded secret.

    Ordinary values are returned byte-for-byte by the caller. Unresolved deep
    encoding or excessive nesting is ambiguous and is not useful raw evidence.
    """
    value = _decoded(value)
    if (_ENTRY_PATH.search(value) or _NTFY_URL.search(value) or _BEARER.search(value) or _QUERY_SECRET.search(value)):
        return True
    try:
        parsed=urlsplit(value)
        if parsed.scheme.lower() in {'http','https','ws','wss'} and (parsed.username is not None or parsed.password is not None):return True
    except ValueError:
        return True
    if re.search(r'%[0-9a-f]{2}',value,re.I): return True
    query = value.split('?',1)[1] if '?' in value else value
    if '=' not in query: return False
    if depth >= 4: return True
    try:
        pairs = parse_qsl(query,keep_blank_values=True,max_num_fields=250)
    except ValueError:
        return True
    for key, nested in pairs:
        if _QUERY_KEY.fullmatch(_decoded(key).replace('_','').replace('-','')) or _sensitive_value(nested,depth+1):
            return True
    return False


def _url_userinfo(match: re.Match) -> str:
    raw=match.group(0)
    try:
        parsed=urlsplit(raw)
        if parsed.username is None and parsed.password is None:return raw
        return urlunsplit(parsed._replace(netloc='[REDACTED]@'+parsed.netloc.rsplit('@',1)[-1]))
    except ValueError:
        return '[REDACTED_URL]' if '@' in raw else raw


def utc_ms() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(value: Any, *, limit: int = 48_000) -> Any:
    """Return a JSON-safe projection without credential values or huge fields."""
    if isinstance(value, dict):
        correlation = {"request_id", "client_request_id", "page_id", "action_id", "event_id", "client_tab_id", "operation_id", "conversation_id", "turn_id", "release"}
        return {str(k)[:160]: "[REDACTED]" if _SECRET_KEY.fullmatch(str(k)) else (str(v)[:160] if str(k) in correlation and isinstance(v, str) else redact(v, limit=limit)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, limit=limit) for v in value[:200]] + ([{"truncated": True, "original_items": len(value)}] if len(value) > 200 else [])
    if isinstance(value, bytes):
        return {"bytes": len(value), "content_omitted": True}
    if isinstance(value, str):
        clean = _QUERY_PAIR.sub(_query_pair, value)
        clean = _BEARER.sub("[REDACTED_AUTH]", clean)
        clean = _QUERY_SECRET.sub("[REDACTED_QUERY_SECRET]", clean)
        clean = _NTFY_URL.sub("[REDACTED_NTFY_URL]", clean)
        clean = _ENTRY_PATH.sub(r"\1/[ENTRY]", clean)
        clean = _URL.sub(_url_userinfo,clean)
        clean = re.sub(r"(?i)((?:token|password|secret|cookie|api[_-]?key)[\"']?\s*[=:]\s*)[\"']?[^\s,;\"']+", r"\1[REDACTED]", clean)
        if len(clean) > limit:
            return {"value": clean[:limit], "truncated": True, "original_chars": len(clean)}
        return clean
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value), limit=limit)


class DiagnosticsWriter:
    def __init__(self, directory: Path, *, normal_max_bytes: int = 128 * 1024 * 1024,
                 error_max_bytes: int = 64 * 1024 * 1024, normal_backups: int = 7,
                 error_backups: int = 8):
        self.directory, self.normal_max_bytes, self.error_max_bytes = directory, normal_max_bytes, error_max_bytes
        self.normal_backups, self.error_backups = normal_backups, error_backups
        self._lock = threading.RLock()
        self._ready = False

    def _prepare(self) -> None:
        if self._ready:
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: os.chmod(self.directory, 0o700)
        except OSError: pass
        self._ready = True

    def _rotate(self, path: Path, maximum: int, backups: int, incoming: int) -> None:
        if path.exists() and path.stat().st_size + incoming <= maximum:
            return
        for n in range(backups - 1, 0, -1):
            src, dst = path.with_name(path.name + f".{n}"), path.with_name(path.name + f".{n + 1}")
            if src.exists(): src.replace(dst)
        if path.exists(): path.replace(path.with_name(path.name + ".1"))

    def write(self, event: dict[str, Any], *, error: bool = False) -> bool:
        # This must not affect the request being diagnosed. stderr is picked up
        # by systemd journal and intentionally does not call logging.
        try:
            data = json.dumps(redact(event), ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            encoded = data.encode("utf-8", "replace")
            with self._lock:
                self._prepare()
                path = self.directory / ("errors.jsonl" if error else "events.jsonl")
                self._rotate(path, self.error_max_bytes if error else self.normal_max_bytes,
                             self.error_backups if error else self.normal_backups, len(encoded))
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    offset = 0
                    while offset < len(encoded):
                        written = os.write(fd, encoded[offset:])
                        if written <= 0:
                            raise OSError("short diagnostics write")
                        offset += written
                finally:
                    os.close(fd)
            return True
        except Exception as exc:
            try: print("codex-web diagnostics write failed: " + redact(repr(exc)), file=sys.stderr, flush=True)
            except Exception: pass
            return False


_WRITER: DiagnosticsWriter | None = None
_CONFIGURED = False
_RELEASE = "unknown"


def get_writer() -> DiagnosticsWriter | None:
    return _WRITER


def current_release() -> str:
    """The once-configured release identifier, without consulting mutable state."""
    return _RELEASE


def emit(kind: str, /, *, level: str = "info", **fields: Any) -> bool:
    writer = _WRITER
    if writer is None:
        return False
    event = {"ts": utc_ms(), "kind": kind, "level": level, "process_id": os.getpid(), "release": _RELEASE, **fields}
    event.setdefault("request_id", request_id_var.get())
    event.setdefault("page_id", page_id_var.get())
    return writer.write(event, error=level in {"warning", "error", "critical"})


def record_exception(kind: str, error: BaseException, **fields: Any) -> bool:
    return emit(kind, level="error", exception_type=type(error).__name__,
                message=str(error), stack="".join(traceback.format_exception(error)), **fields)


class _DiagnosticsLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("shared.diagnostics") or record.levelno < logging.WARNING:
            return
        try:
            event = {"logger": record.name, "message": record.getMessage()}
            if record.exc_info:
                event["stack"] = "".join(traceback.format_exception(*record.exc_info))
            emit("python_log", level="error" if record.levelno >= logging.ERROR else "warning", **event)
        except Exception:
            pass


def configure_diagnostics() -> DiagnosticsWriter:
    global _WRITER, _CONFIGURED, _RELEASE
    if _CONFIGURED and _WRITER is not None:
        return _WRITER
    directory = Path(os.environ.get("CODEX_WEB_DIAGNOSTICS_DIR", str(STATE / "diagnostics")))
    # Release metadata is local deployment state; failures deliberately leave a
    # visible "unknown" rather than reading secrets or running subprocesses.
    _RELEASE = os.environ.get("CODEX_WEB_RELEASE", "").strip() or "unknown"
    if _RELEASE == "unknown":
        try:
            releases = STATE / "releases"
            pending = releases / "pending-publish"
            if pending.is_file():
                candidate = pending.read_text(encoding="ascii").split()[0]
                if re.fullmatch(r"[0-9]{8}-[0-9]{6}", candidate):
                    _RELEASE = candidate
            else:
                from shared.release_state import latest_healthy_release
                _RELEASE = latest_healthy_release(releases, releases / "publish.log") or "unknown"
        except (OSError, IndexError, ValueError): pass
    _WRITER = DiagnosticsWriter(directory,
        normal_max_bytes=int(os.environ.get("CODEX_WEB_DIAGNOSTICS_NORMAL_MAX_BYTES", 128 * 1024 * 1024)),
        error_max_bytes=int(os.environ.get("CODEX_WEB_DIAGNOSTICS_ERROR_MAX_BYTES", 64 * 1024 * 1024)))
    # Preparation failures are reported through stderr on first actual output.
    logging.getLogger().addHandler(_DiagnosticsLogHandler())
    _CONFIGURED = True
    emit("diagnostics_started")
    return _WRITER


class DiagnosticsMiddleware:
    """ASGI-only observer; does not buffer request/response bodies."""
    _id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
    def __init__(self, app: Any): self.app = app
    @classmethod
    def _valid(cls, value: str | None) -> str | None:
        return value if value and cls._id.fullmatch(value) else None
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        typ = scope.get("type")
        if typ not in {"http", "websocket"}:
            await self.app(scope, receive, send); return
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        client_req = self._valid(headers.get("x-request-id"))
        req = uuid.uuid4().hex
        scope["diagnostics_request_id"] = req
        page = self._valid(headers.get("x-diagnostics-page-id"))
        action = self._valid(headers.get("x-diagnostics-action-id"))
        operation = self._valid(headers.get("x-diagnostics-operation-id"))
        edge = self._valid(headers.get("x-diagnostics-edge-id"))
        wsl = self._valid(headers.get("x-diagnostics-wsl-id"))
        path = scope.get("path", "")
        started = time.perf_counter(); status = None; sent_bytes = 0; close_code = None
        received_bytes = 0; disconnected = False; accepted = False; failure = None
        close_sent_code = disconnect_code = None
        close_sent_ms = disconnect_ms = None
        response_complete = False
        correlation = dict(operation_id=operation, edge_id=edge, wsl_id=wsl)
        tokens = (request_id_var.set(req), page_id_var.set(page))
        emit("request_start" if typ == "http" else "ws_connect", request_id=req, page_id=page, action_id=action,
             method=scope.get("method"), path=path, protocol=typ, client_request_id=client_req,
             http_version=scope.get("http_version"), client=scope.get("client"),
             query=scope.get("query_string", b"").decode("utf-8", "replace"), **correlation)
        async def observed_send(message: dict[str, Any]) -> None:
            nonlocal status, sent_bytes, close_code, accepted, close_sent_code, close_sent_ms, response_complete
            if typ == "http" and message["type"] == "http.response.start":
                status = message.get("status")
                hs = list(message.get("headers", []))
                hs.append((b"x-request-id", req.encode()))
                hs.append((b"server-timing", ("diagnostics;desc=\"request-id=" + req + "\"").encode()))
                if edge: hs.append((b"x-diagnostics-edge-id", edge.encode()))
                if wsl: hs.append((b"x-diagnostics-wsl-id", wsl.encode()))
                message = dict(message, headers=hs)
            elif typ == "http" and message["type"] == "http.response.body":
                sent_bytes += len(message.get("body", b""))
            elif typ == "websocket" and message["type"] == "websocket.close":
                close_code = close_sent_code = message.get("code")
                close_sent_ms = round((time.perf_counter() - started) * 1000, 3)
                emit("ws_close_sent", request_id=req, page_id=page, path=path, code=close_sent_code,
                     reason=message.get("reason"), elapsed_ms=close_sent_ms, **correlation)
            elif typ == "websocket" and message["type"] == "websocket.accept":
                accepted = True
                emit("ws_accepted", request_id=req, path=path)
            await send(message)
            if typ == "http" and message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
        try:
            async def observed_receive():
                nonlocal close_code, received_bytes, disconnected, disconnect_code, disconnect_ms
                message = await receive()
                if message.get("type") == "http.request":
                    received_bytes += len(message.get("body", b""))
                if message.get("type") in {"http.disconnect", "websocket.disconnect"}:
                    disconnected = True
                if typ == "websocket" and message.get("type") == "websocket.disconnect":
                    close_code = disconnect_code = message.get("code")
                    disconnect_ms = round((time.perf_counter() - started) * 1000, 3)
                    emit("ws_disconnect_received", request_id=req, page_id=page, path=path, code=disconnect_code,
                         reason=message.get("reason"), elapsed_ms=disconnect_ms, **correlation)
                return message
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            failure = type(exc).__name__
            emit("request_exception" if typ == "http" else "ws_exception", level="error", request_id=req, page_id=page,
                 action_id=action, path=path, exception_type=type(exc).__name__, message=str(exc),
                 stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            raise
        finally:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            emit("request_end" if typ == "http" else "ws_close", level="error" if status and status >= 500 else "info",
                 request_id=req, page_id=page, action_id=action, path=path, status=status, duration_ms=elapsed,
                 response_bytes=sent_bytes, request_bytes=received_bytes, close_code=close_code,
                 close_sent_code=close_sent_code, disconnect_code=disconnect_code,
                 close_sent_ms=close_sent_ms, disconnect_ms=disconnect_ms,
                 response_complete=response_complete if typ == "http" else None,
                 disconnected=disconnected, accepted=accepted, failure=failure, **correlation)
            request_id_var.reset(tokens[0]); page_id_var.reset(tokens[1])
