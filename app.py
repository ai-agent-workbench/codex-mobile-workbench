"""Codex Web Workbench -- FastAPI application entry point."""
from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# -- Middleware ---------------------------------------------------------------

from middleware.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)
from middleware.request_origin import RequestOriginMiddleware
app.add_middleware(RequestOriginMiddleware, router=app.router)
from shared.diagnostics import DiagnosticsMiddleware
app.add_middleware(DiagnosticsMiddleware)

# Keep the standard opaque 500 body while exposing the same correlation ID
# when ServerErrorMiddleware handles an exception outside user middleware.
@app.exception_handler(Exception)
async def diagnostic_server_error(request, error):
    from fastapi.responses import PlainTextResponse
    request_id = request.scope.get("diagnostics_request_id", "")
    return PlainTextResponse("Internal Server Error", status_code=500, headers={
        "X-Request-ID": request_id, "Cache-Control": "no-store",
    })


# -- Route registration ------------------------------------------------------

from routes.home import router as home_router
from routes.conversations import router as conversations_router
from routes.files import router as files_router
from routes.projects import router as projects_router
from routes.auth import router as auth_router
from routes.misc import router as misc_router
from routes.ws import router as ws_router
from routes.passkey import router as passkey_router
from routes.legacy_employee import router as legacy_employee_router
from routes.conversation_api import router as conversation_api_router
from routes.web_push import router as web_push_router
from routes.settings_center import router as settings_center_router
from routes.diagnostics import router as diagnostics_router
from routes.diagnostic_reports import router as diagnostic_reports_router
from routes.automations import router as automations_router
from routes.dot_conversations import router as dot_conversations_router
from routes.maintenance_admission import router as maintenance_admission_router

app.include_router(home_router)
app.include_router(conversations_router)
app.include_router(files_router)
app.include_router(projects_router)
app.include_router(auth_router)
app.include_router(misc_router)
app.include_router(ws_router)
app.include_router(passkey_router)
# The specific recovery routes must stay ahead of the retired /operations catch-all.
app.include_router(legacy_employee_router)
app.include_router(conversation_api_router)
app.include_router(web_push_router)
app.include_router(settings_center_router)
app.include_router(diagnostics_router)
app.include_router(diagnostic_reports_router)
app.include_router(automations_router)
app.include_router(dot_conversations_router)
app.include_router(maintenance_admission_router)


# -- Startup -----------------------------------------------------------------

@app.on_event("startup")
async def startup_conversations():
    from engine.conversation_service import startup_conversation_service
    from shared.diagnostics import configure_diagnostics, emit

    configure_diagnostics()
    emit("app_starting")
    await startup_conversation_service()
    from engine.dot_notifications import startup_dot_notifications
    await startup_dot_notifications()


@app.on_event("shutdown")
async def shutdown_conversations():
    from engine.conversation_service import shutdown_conversation_service
    from shared.diagnostics import emit

    stop_stall_monitor()
    emit("app_stopping")
    from engine.dot_notifications import shutdown_dot_notifications
    await shutdown_dot_notifications()
    await shutdown_conversation_service()

@app.on_event("startup")
def startup_cleanup():
    from account_migration import migrate_accounts
    from engine.conversation_service import conversation_service
    from shared.file_utils import clean_staging

    conversation_service().cleanup_abandoned_drafts()
    clean_staging()
    migrate_accounts()


def stop_stall_monitor():
    """Diagnostic shutdown must neither delay nor prevent service shutdown."""
    monitor = getattr(app.state, "web_stall_monitor", None)
    app.state.web_stall_monitor = None
    if monitor is not None:
        try:
            monitor.stop()
        except Exception:
            from shared.diagnostics import emit
            emit("web_stall_monitor_stop_failed", level="warning", reason="stop_error")


@app.on_event("startup")
async def startup_stall_monitor():
    # This is deliberately the last startup hook: no watcher survives a failed
    # conversation/dot/migration startup. It observes Web scheduling, not turn age.
    import asyncio
    import os
    from shared.diagnostics import current_release, emit, get_writer

    if getattr(app.state, "web_stall_monitor", None) is not None:
        return
    enabled = os.environ.get("CODEX_WEB_STALL_MONITOR", "1").strip().lower()
    if enabled not in {"1", "true", "on", "yes"}:
        emit("web_stall_monitor_disabled", reason="configuration" if enabled in {"0", "false", "off", "no"} else "invalid_configuration")
        return
    writer = get_writer()
    if writer is None:
        emit("web_stall_monitor_disabled", level="warning", reason="diagnostics_unavailable")
        return
    monitor = None
    try:
        from shared.web_stall_monitor import WebStallMonitor
        monitor = WebStallMonitor(writer.directory, release=current_release())
        if not monitor.start(asyncio.get_running_loop()):
            raise RuntimeError("monitor_start_failed")
        app.state.web_stall_monitor = monitor
        emit("web_stall_monitor_started", **monitor.status())
    except Exception:
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:
                pass
        app.state.web_stall_monitor = None
        emit("web_stall_monitor_disabled", level="warning", reason="start_error")
