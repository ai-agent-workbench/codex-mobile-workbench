"""Local control plane attestation; remote clients have no control endpoint."""
import os
from functools import lru_cache
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from engine.conversation_service import conversation_service
from shared.maintenance_admission import busy_counts
from shared.maintenance_native import busy_counts as native_busy_counts

router = APIRouter()


@lru_cache(maxsize=1)
def web_started_at(pid):
    # Key by PID so even a fork cannot reuse its parent's lifetime. No service
    # command is run: the existing helper pins /proc identity around the read.
    from shared.instance_primitives import process_identity, process_started_at
    return process_started_at({'pid': pid, 'starttime': process_identity(pid)})


@router.get('/internal/maintenance-admission')
async def status(request: Request):
    # Native inventory runs before the synchronous local handoff snapshot.
    # It includes native descendants absent from the SQLite projection.
    # A sync FastAPI threadpool endpoint could interleave background→ingress
    # counts and falsely see both sides of a handoff as zero.
    # Use the transport peer, never a client-provided forwarded header. The
    # trusted proxy must not expose /internal/ paths (bootstrap precondition).
    if request.client is None or request.client.host not in {'127.0.0.1', '::1'}:
        return JSONResponse({'error': 'forbidden'}, status_code=403)
    service = conversation_service()
    if not service.store.maintenance_admission.authorized(request.headers.get('x-workbench-maintenance-token')):
        return JSONResponse({'error': 'forbidden'}, status_code=403)
    try:
        client = await service.runtime._ensure_client()
        native_counts = await native_busy_counts(client)
    except Exception:
        # An unavailable/unsupported/unstable native runtime cannot prove idle.
        native_counts = {'native_inventory_unavailable': 1}
    counts = local_busy_counts(service)
    counts.update(native_counts)
    return JSONResponse({**service.store.maintenance_admission.identity(),
        'database': str(service.store.path.resolve()),
        'state': service.store.maintenance_admission.read(),
        'busy': counts, 'idle': not any(counts.values())},
        headers={'Cache-Control': 'no-store'})


def local_busy_counts(service):
    """No await: counter/queue→DB handoffs cannot interleave this local sample."""
    runtime = service.runtime
    with service.store._lock:
        counts = busy_counts(service.store._db, web_started_at=web_started_at(os.getpid()))
    counts['native_reviews'] = sum(not task.done() for task in runtime._review_tasks.values())
    counts['background'] = sum(not task.done() for task in runtime._background)
    counts['notification_handlers'] = runtime._notification_handlers
    counts['native_request_callbacks'] = sum(not task.done() for task in
        getattr(runtime._client, '_callback_tasks', ()))
    queue = getattr(runtime._client, '_notifications', None)
    counts['native_notifications_queued'] = queue.qsize() if queue is not None else 0
    return counts
