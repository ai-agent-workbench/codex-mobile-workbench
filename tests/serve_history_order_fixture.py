"""Loopback-only ordering regression with real routes and disposable SQLite."""
import asyncio
from datetime import datetime, timezone
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from routes import home, projects, conversations, conversation_api
from tests.history_search_fixtures import search_fixture


def main():
    with search_fixture() as (store, service):
        app = FastAPI()
        for module in (home, projects, conversations, conversation_api):
            app.include_router(module.router)
        app.mount("/static", StaticFiles(directory="static"), name="static")
        requests = []
        flags = {"delay": 0, "fail_pages": 0, "fail_cursor": False, "auth": False}

        @app.middleware("http")
        async def controls(request, call_next):
            prefix = "/" + "a" * 40
            if request.scope["path"].startswith(prefix + "/"):
                request.scope["path"] = request.scope["path"][len(prefix):]
                request.scope["raw_path"] = request.scope["path"].encode()
            path = request.scope["path"]
            if path in {"/projects/history/page", "/projects/history/status",
                        "/project/fixture/conversations/page", "/project/fixture/conversations/status"}:
                cursor = request.query_params.get("cursor")
                requests.append({"path": path, "cursor": cursor,
                                 "check_order": request.query_params.get("check_order")})
                if flags["auth"]:
                    return JSONResponse({"code": "auth_required"}, status_code=401)
                if path.endswith("/page"):
                    if flags["delay"]:
                        await asyncio.sleep(flags["delay"])
                    if flags["fail_pages"] or (flags["fail_cursor"] and cursor):
                        flags["fail_pages"] = max(0, flags["fail_pages"] - 1)
                        return JSONResponse({"error": "synthetic failure"}, status_code=503)
            return await call_next(request)

        @app.post("/_fixture/reset")
        def reset():
            flags.update(delay=0, fail_pages=0, fail_cursor=False, auth=False)
            requests.clear()
            store._db.execute("DELETE FROM conversation_reads")
            for n, row in enumerate(store._db.execute("SELECT id FROM conversations ORDER BY id")):
                stamp = f"2026-10-01T00:{n:02d}:00+00:00"
                store._db.execute(
                    "UPDATE conversations SET pinned=0,state='idle',terminal_generation=0,"
                    "last_terminal_turn_id=NULL,last_terminal_at=NULL,activity_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
            return {"ok": True}

        @app.post("/_fixture/complete/{cid}")
        def complete(cid: str):
            turn = "turn-" + cid
            stamp = datetime.now(timezone.utc).isoformat()
            store._db.execute("UPDATE turns SET status='completed',completed_at=? WHERE id=?", (stamp, turn))
            store._db.execute(
                "UPDATE conversations SET state='idle',last_terminal_turn_id=?,last_terminal_at=?,"
                "activity_at=?,terminal_generation=terminal_generation+1,revision=revision+1 WHERE id=?",
                (turn, stamp, stamp, cid),
            )
            return {"ok": True}

        @app.post("/_fixture/pin/{cid}")
        def pin(cid: str):
            store.set_pinned([cid], True)
            return {"ok": True}

        @app.post("/_fixture/flags")
        async def set_flags(request: Request):
            flags.update(await request.json())
            return {"ok": True}

        @app.get("/_fixture/requests")
        def get_requests():
            return requests

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            print("FIXTURE_PORT=" + str(listener.getsockname()[1]), flush=True)
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
