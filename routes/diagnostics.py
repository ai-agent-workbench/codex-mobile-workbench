"""Authenticated intake for browser diagnostics; accepts projections only."""
from __future__ import annotations
import re
import json
import asyncio
import threading
from collections import deque
from urllib.parse import urlsplit
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from shared.diagnostics import emit, redact
from shared.security import logged_in
from shared.config import PASSKEY_ORIGIN
from shared.passkey_auth import request_is_same_origin

router = APIRouter()
# Best-effort duplicate suppression covers client retries in this process. IDs are
# also retained in each event, so a restart favors evidence over false dedupe.
_SEEN: set[str] = set()
_SEEN_ORDER: deque[str] = deque()
_INGEST_LOCK = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_TYPES = {"js_error","unhandled_rejection","resource_error","page_load","page_show","page_hide","visibility","online","offline","click","navigation","submit","fetch","ws","gap"}

def _id(value: Any) -> str | None: return value if isinstance(value, str) and _ID.fullmatch(value) else None

def _event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("type") not in _TYPES: return None
    event_id = _id(value.get("event_id"))
    page, action = _id(value.get("page_id")), _id(value.get("action_id"))
    if not page or not event_id: return None
    client_ts = value.get("ts")
    if not isinstance(client_ts, str) or len(client_ts) > 64: return None
    seq = value.get("seq")
    if not isinstance(seq, int) or not 0 <= seq <= 2**53: return None
    data = value.get("data", {})
    if not isinstance(data, dict): return None
    # Browser only sends this narrow schema; no DOM/form text or request bodies.
    allowed = {"path","target","tag","id","x","y","hit","duration_ms","status","method","url","request_id","code","message","stack","online","visibility","reason","count","error","phase","operation_id","hit_id","was_clean","width","height","default_prevented","release","persisted","script_version","user_agent","service_worker"}
    allowed.update({"context","edge_id","wsl_id","page_id","client_id","resulting_client_id","worker_version","response_stage",
                    "effective_type","connection_type","rtt","downlink","save_data","time_origin_ms","initiator_type",
                    "start_ms","worker_start_ms","dns_start_ms","dns_end_ms","connect_start_ms","connect_end_ms",
                    "tls_start_ms","request_start_ms","response_start_ms","response_end_ms","transfer_size",
                    "encoded_body_size","decoded_body_size","next_hop_protocol","delivery_type","retry_ms","accepted","previous_phase","previous_ts","line_number","column_number","version","was_discarded"})
    projection = {k: data[k] for k in allowed & data.keys() if isinstance(data[k], (str,int,float,bool,type(None)))}
    return {"event_id": event_id, "client_ts": client_ts, "client_type": value["type"], "page_id": page, "action_id": action, "client_tab_id": _id(value.get("tab_id")), "seq": seq, "data": redact(projection, limit=48_000)}

@router.post("/settings/diagnostics/client-events")
async def ingest(request: Request):
    if not logged_in(request):
        return JSONResponse({"accepted": 0}, status_code=401)
    # Keep intake bounded even if a malicious authenticated page sends a giant body.
    try: declared = int(request.headers.get("content-length", "0") or 0)
    except ValueError: declared = 1_100_001
    if declared > 1_100_000:
        return JSONResponse({"accepted": 0, "error": "body_too_large"}, status_code=413)
    origin = request.headers.get("origin", "")
    # Passkey deployments have an explicit canonical origin. Legacy fixtures
    # may not, so compare normalized hostnames (not backend Host ports, which
    # nginx commonly removes) without creating a new authentication mechanism.
    if PASSKEY_ORIGIN:
        same_origin = request_is_same_origin(request)
    else:
        parsed = urlsplit(origin)
        same_origin = bool(origin and parsed.scheme in {"http", "https"} and parsed.netloc == request.headers.get("host", "") and not parsed.username and not parsed.password)
    if not same_origin:
        return JSONResponse({"accepted": 0, "error": "origin_required"}, status_code=403)
    try:
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > 1_048_576: return JSONResponse({"accepted": 0, "error": "body_too_large"}, status_code=413)
            chunks.append(chunk)
        raw = json.loads(b"".join(chunks))
    except Exception:
        return JSONResponse({"accepted": 0, "error": "invalid_json"}, status_code=400)
    values = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(values, list) or len(values) > 250:
        return JSONResponse({"accepted": 0, "error": "invalid_events"}, status_code=400)
    events = [_event(value) for value in values]
    if any(value is None for value in events):
        return JSONResponse({"accepted": 0, "error": "invalid_event"}, status_code=400)
    result, status = await asyncio.to_thread(_persist_batch, events)
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})


def _persist_batch(events):
    # Serialize dedupe + disk append off the event loop; acknowledge only after
    # the writer completed. A crash may replay duplicates, never suppress evidence.
    accepted_ids = []
    with _INGEST_LOCK:
        for event in events:
            assert event is not None
            if event["event_id"] in _SEEN:
                accepted_ids.append(event["event_id"])
                continue
            data = event["data"]
            error = (event["client_type"] in {"js_error", "unhandled_rejection", "resource_error", "gap"}
                     or data.get("phase") in {"error", "message_parse_or_handle_failed", "construct_failed", "send_failed", "close_failed"}
                     or data.get("context") == "diagnostics_delivery" and data.get("phase") != "recovered"
                     or isinstance(data.get("status"), (int, float)) and data["status"] >= 400)
            if not emit("browser", level="error" if error else "info", **event):
                return {"accepted":len(accepted_ids), "accepted_event_ids":accepted_ids, "error":"diagnostics_unavailable"}, 503
            _SEEN.add(event["event_id"])
            _SEEN_ORDER.append(event["event_id"])
            if len(_SEEN_ORDER) > 20000:
                _SEEN.discard(_SEEN_ORDER.popleft())
            accepted_ids.append(event["event_id"])
    return {"accepted":len(accepted_ids), "accepted_event_ids":accepted_ids}, 200
