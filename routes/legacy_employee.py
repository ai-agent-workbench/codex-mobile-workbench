"""Tombstones for retired employee business portal endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


router = APIRouter()
_REASON_CODE = "legacy_employee_portal_retired"
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_HTML = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>员工业务入口已迁移</title></head><body><main><h1>员工业务入口已迁移</h1><p>员工业务请使用钉钉办理。</p></main></body></html>"""
_HEADERS = {"Cache-Control": "no-store, max-age=0", "X-Content-Type-Options": "nosniff"}


def _wants_json(request: Request) -> bool:
    """Use HTML only for an ordinary browser navigation.

    This handler intentionally never reads the request body: retired write
    endpoints must not parse or retain an upload before refusing it.
    """
    return "text/html" not in request.headers.get("accept", "")


def _retired(request: Request) -> Response:
    if _wants_json(request):
        return JSONResponse({"reason_code": _REASON_CODE}, status_code=410,
                            headers=_HEADERS)
    return HTMLResponse(_HTML, status_code=410, headers=_HEADERS)


@router.api_route("/upload", methods=_METHODS)
@router.api_route("/upload/{path:path}", methods=_METHODS)
@router.api_route("/operations", methods=_METHODS)
@router.api_route("/operations/{path:path}", methods=_METHODS)
async def retired_employee_portal(request: Request) -> Response:
    return _retired(request)
