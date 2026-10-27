"""Manual, authenticated browser fault-report endpoint."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import UploadFile

from shared import config
from shared.diagnostic_reports import (MAX_REQUEST_BYTES, ReportError, image_info,
    new_report_id, parse_fault_time, persist_report, update_log_receipt, validate_fields, validate_report_id)
from shared.diagnostics import emit, utc_ms
from shared.html_lib import templates
from shared.request_origin import require_same_https_origin
from shared.security import logged_in, require_auth
from shared.web_push import WebPushError

router = APIRouter()
_FIELDS = {"report_id", "category", "fault_time", "client_page_id", "source_path", "last_phase", "client_time", "client_version", "screenshot"}
MAX_TEXT_PART_BYTES = 1024


def _utc_input(value: str | None = None) -> str:
    """datetime-local markup has no zone; server defaults are deliberately UTC."""
    try:
        instant = parse_fault_time(value) if value else utc_ms()
    except ReportError:
        instant = utc_ms()
    return instant[:16]


def _context(request: Request, *, result=None, error=None, report_id=None, fault_time=None):
    return {"request": request, "result": result, "error": error,
            "report_id": report_id or new_report_id(), "fault_time": _utc_input(fault_time)}


def _error_text(code: str) -> str:
    return {
        "authentication_required": "登录已失效，请重新登录后再试。",
        "origin_required": "此请求必须从当前工作台页面提交。",
        "body_too_large": "提交内容超过允许大小。",
        "image_too_large": "截图超过 3 MiB，未保存。",
        "invalid_image": "截图必须是手动选择的 PNG 或 JPEG 文件。",
        "report_capacity_exhausted": "本机故障报告存储已满，未保存；不会自动删除已有报告。",
        "report_id_conflict": "此报告编号已用于不同内容。请刷新页面建立新的报告，避免覆盖原报告。",
        "report_storage_unavailable": "本机报告存储暂不可用，未确认保存。",
        "invalid_form": "提交格式无效，请检查后手动再试。",
        "invalid_fields": "提交字段无效，请刷新页面后手动再试。",
    }.get(code, "提交内容无效，未保存；请检查后手动再试。")


def _same_origin(request: Request) -> bool:
    if config.PASSKEY_ORIGIN:
        try:
            require_same_https_origin(request, allow_form_referer=True)
            return True
        except (WebPushError, ValueError):
            return False
    origin = request.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return bool(origin and len(request.headers.getlist("origin")) == 1
                and len(request.headers.getlist("host")) == 1
                and parsed.scheme in {"http", "https"}
                and parsed.netloc == request.headers.get("host", "")
                and not parsed.username and not parsed.password)


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _result(request: Request, status: int, payload: dict[str, Any]) -> HTMLResponse | JSONResponse:
    if _wants_json(request):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})
    return HTMLResponse(templates.get_template("diagnostic_report.html").render(**_context(request, result=payload)),
                        status_code=status, headers={"Cache-Control": "no-store"})


def _failure(request: Request, error: ReportError, *, report_id: str | None = None,
             fault_time: str | None = None) -> HTMLResponse | JSONResponse:
    payload = {"saved": False, "error": error.code}
    if _wants_json(request):
        return JSONResponse(payload, status_code=error.status, headers={"Cache-Control": "no-store"})
    return HTMLResponse(templates.get_template("diagnostic_report.html").render(**_context(
        request, error={"code": error.code, "message": _error_text(error.code)}, report_id=report_id,
        fault_time=fault_time)), status_code=error.status, headers={"Cache-Control": "no-store"})


@router.get("/settings/diagnostics/report")
async def report_form(request: Request):
    denied = require_auth(request)
    if denied:
        return denied
    return HTMLResponse(templates.get_template("diagnostic_report.html").render(**_context(request)),
                        headers={"Cache-Control": "no-store"})


async def _bounded_form(request: Request):
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        raise ReportError("body_too_large", 413)
    if declared > MAX_REQUEST_BYTES:
        raise ReportError("body_too_large", 413)
    chunks: list[bytes] = []; total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise ReportError("body_too_large", 413)
            chunks.append(chunk)
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError("invalid_form") from exc
    body = b"".join(chunks)
    delivered = False
    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}
    replay = Request(request.scope, receive=receive)
    try:
        return await replay.form(max_files=1, max_fields=16, max_part_size=MAX_TEXT_PART_BYTES)
    except Exception as exc:
        raise ReportError("invalid_form") from exc


@router.post("/settings/diagnostics/report")
async def submit_report(request: Request):
    owner = logged_in(request)
    if not owner:
        return _failure(request, ReportError("authentication_required", 401))
    if not _same_origin(request):
        return _failure(request, ReportError("origin_required", 403))
    form = None
    retry_report_id: str | None = None
    retry_fault_time: str | None = None
    try:
        form = await _bounded_form(request)
        items = list(form.multi_items())
        names = [key for key, _ in items]
        if any(name not in _FIELDS for name in names) or len(names) != len(set(names)):
            raise ReportError("invalid_fields")
        raw: dict[str, Any] = {}
        upload: UploadFile | None = None
        for key, value in items:
            if key == "screenshot":
                if not isinstance(value, UploadFile):
                    raise ReportError("invalid_image")
                upload = value
            elif not isinstance(value, str):
                raise ReportError("invalid_fields")
            else:
                raw[key] = value
        if isinstance(raw.get("report_id"), str) and raw["report_id"]:
            try:
                retry_report_id = validate_report_id(raw["report_id"])
            except ReportError:
                pass
        retry_fault_time = raw.get("fault_time") if isinstance(raw.get("fault_time"), str) else None
        report_id = validate_report_id(raw.get("report_id"))
        fields = validate_fields(raw)
        screenshot = None
        content_type = None
        if upload is not None:
            content_type = upload.content_type
            screenshot = await upload.read(MAX_REQUEST_BYTES + 1)
            # Browsers encode an untouched file input as a zero-byte UploadFile
            # with an empty name. It is absence, not an image. A named empty
            # file or any payload with an empty name remains invalid.
            if upload.filename == "" and not screenshot:
                screenshot = None
                content_type = None
            elif upload.filename == "":
                raise ReportError("invalid_image")
            elif len(screenshot) > 3 * 1024 * 1024:
                raise ReportError("image_too_large", 413)
        image = image_info(content_type, screenshot)
        receipt, metadata, existing = await asyncio.to_thread(persist_report, report_id, owner, fields, screenshot, image)
        # Existing receipt is deliberately returned unchanged: a browser that
        # lost a prior response may retry manually with the same identifier.
        if existing:
            return _result(request, 200, receipt)
        logged = await asyncio.to_thread(emit, "browser_fault_report", level="error", report_id=report_id,
                                         category=fields["category"], fault_time=fields["fault_time"], client_page_id=fields["client_page_id"],
                                         source_path=fields["source_path"], last_phase=fields["last_phase"],
                                         client_time=fields["client_time"])
        try:
            receipt = await asyncio.to_thread(update_log_receipt, report_id, logged)
        except Exception:
            # The evidence itself is durable; do not turn that fact into a false
            # failure merely because recording the receipt bit failed.
            receipt = {"report_id": report_id, "saved": True, "diagnostics_logged": bool(logged)}
        return _result(request, 201, receipt)
    except ReportError as error:
        return _failure(request, error, report_id=retry_report_id, fault_time=retry_fault_time)
    finally:
        if form is not None:
            await form.close()
