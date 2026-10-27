"""One approved dot's notification worker, hosted only by the Web service."""
from __future__ import annotations
import asyncio
import re
from urllib.parse import urlsplit
from engine.dot_delivery import deliver_pending
from engine.dot_mcp import APPROVED_REPLY_CONVERSATION_ID

_task = None
_stop = None


def notification_link(origin, configured=""):
    # Existing origin is used by the service, never extracted by maintenance.
    # Match the existing canonical login redirect: configured origin + fixed
    # workbench route. No maintenance process extracts environment or secrets.
    parsed=urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("invalid configured workbench origin")
    if not configured:
        return origin.rstrip("/") + "/settings/dot"
    link=urlsplit(configured)
    if link.scheme != "https" or link.netloc != parsed.netloc or link.username or link.password or link.query or link.fragment or not re.fullmatch(r"/(?:[0-9a-f]{40,}/)?settings/dot/?",link.path):
        raise ValueError("dot notification link is outside the configured workbench")
    return configured.rstrip("/")


def check_notification_link(link):
    """Anonymous, bounded GET; never sends cookies, credentials or reads a body."""
    import urllib.request
    import urllib.error
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request=urllib.request.Request(link,headers={"Accept":"text/html","Sec-Fetch-Mode":"navigate"})
    try:
        response=urllib.request.build_opener(NoRedirect).open(request,timeout=10)
        with response:
            return response.status == 200, response.status
    except urllib.error.HTTPError as error:
        # The existing authentication recovery redirects navigation to /login.
        target=urlsplit(error.headers.get("Location", ""))
        valid=(error.code in (302,303,307,308) and target.path == "/login"
               and (not target.netloc or target.netloc == urlsplit(link).netloc)
               and target.scheme in ("", "https") and not target.username and not target.password)
        return valid,error.code
    except Exception:
        return False,0


def _tick(*, arm_only=False):
    from shared.config import DOT_NTFY_ENABLED, DOT_OWNER_ACCOUNT_ID, DOT_PARENT_REF, PASSKEY_ORIGIN, DOT_NTFY_LINK
    from routes.dot_conversations import dot_available, dot_service, dot_delivery
    from shared.security import notify_task_completion
    if not DOT_NTFY_ENABLED or not dot_available(DOT_OWNER_ACCOUNT_ID): return
    service=dot_service()
    conversation=service.existing(DOT_OWNER_ACCOUNT_ID,DOT_PARENT_REF)
    if not conversation or conversation["id"] != APPROVED_REPLY_CONVERSATION_ID: return
    summary=service.mailbox.delivery_summary(conversation["id"],DOT_OWNER_ACCOUNT_ID,parent_dot_ref=DOT_PARENT_REF)
    store=dot_delivery()
    link=notification_link(PASSKEY_ORIGIN,DOT_NTFY_LINK)
    if arm_only:
        store.arm(DOT_OWNER_ACCOUNT_ID,conversation["id"],DOT_PARENT_REF,summary["reply_generation"])
        valid,status=check_notification_link(link)
        store.record_link_status(DOT_OWNER_ACCOUNT_ID,conversation["id"],valid,status)
        return
    if not (store.notice_state(DOT_OWNER_ACCOUNT_ID,conversation["id"],"link:canonical") or "").startswith("verified_"):
        return
    deliver_pending(store,summary,owner=DOT_OWNER_ACCOUNT_ID,cid=conversation["id"],parent=DOT_PARENT_REF,
                    sender=notify_task_completion,link=link)


async def startup_dot_notifications():
    global _task, _stop
    from shared.config import DOT_NTFY_ENABLED, DOT_NTFY_TEST_ONCE_20261004
    if not DOT_NTFY_ENABLED or _task is not None: return
    _stop=asyncio.Event()
    async def run():
        from shared.diagnostics import emit
        try:
            await asyncio.to_thread(_tick,arm_only=True)
            if DOT_NTFY_TEST_ONCE_20261004:
                await asyncio.to_thread(send_approved_test_notice)
        except Exception:
            emit("dot_notification_unavailable")
        while not _stop.is_set():
            try:
                await asyncio.wait_for(_stop.wait(),timeout=3)
            except asyncio.TimeoutError:
                try: await asyncio.to_thread(_tick)
                except Exception: emit("dot_notification_unavailable")
    _task=asyncio.create_task(run())


async def shutdown_dot_notifications():
    global _task, _stop
    if _task is not None:
        _stop.set()
        await _task
        _task=None
        _stop=None


def send_approved_test_notice():
    """One explicitly approved test; no mailbox mutation and no reply fabrication."""
    from shared.config import DOT_NTFY_ENABLED, DOT_OWNER_ACCOUNT_ID, DOT_PARENT_REF, PASSKEY_ORIGIN, DOT_NTFY_LINK
    from routes.dot_conversations import dot_available, dot_service, dot_delivery
    from shared.security import notify_task_completion
    if not DOT_NTFY_ENABLED or not dot_available(DOT_OWNER_ACCOUNT_ID):
        raise ValueError("dot notifications are disabled")
    conversation=dot_service().existing(DOT_OWNER_ACCOUNT_ID,DOT_PARENT_REF)
    if not conversation or conversation["id"] != APPROVED_REPLY_CONVERSATION_ID:
        raise ValueError("dot notification conversation is not approved")
    link=notification_link(PASSKEY_ORIGIN,DOT_NTFY_LINK)
    store=dot_delivery()
    cid=conversation["id"]
    notice_id="test:approved-20261004"
    if store.notice_state(DOT_OWNER_ACCOUNT_ID,cid,notice_id) is not None:
        return {"attempted":False,"state":store.notice_state(DOT_OWNER_ACCOUNT_ID,cid,notice_id)}
    link_state=store.notice_state(DOT_OWNER_ACCOUNT_ID,cid,"link:canonical") or ""
    if link_state.startswith("unverified_"):
        return {"attempted":False,"state":"link_unverified","http_status":int(link_state.rsplit("_",1)[1])}
    if not link_state.startswith("verified_"):
        valid,status=check_notification_link(link)
        store.record_link_status(DOT_OWNER_ACCOUNT_ID,cid,valid,status)
        if not valid:
            return {"attempted":False,"state":"link_unverified","http_status":status}
    if not store.claim(DOT_OWNER_ACCOUNT_ID,cid,notice_id):
        return {"attempted":False,"state":store.notice_state(DOT_OWNER_ACCOUNT_ID,cid,notice_id)}
    try:
        success,_=notify_task_completion("dot 通知测试", "dot 通知测试\n\n[打开工作台]("+link+")",tags="white_check_mark")
    except Exception:
        success=False
    store.finish(DOT_OWNER_ACCOUNT_ID,cid,notice_id,success)
    return {"attempted":True,"state":store.notice_state(DOT_OWNER_ACCOUNT_ID,cid,notice_id)}
