"""Loopback-only real-route fixture for the history-search browser regression."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

from routes import home, projects, conversations, conversation_api
from tests.history_search_fixtures import search_fixture


def main():
    with search_fixture():
        app = FastAPI()
        for module in (home, projects, conversations, conversation_api):
            app.include_router(module.router)
        app.mount("/static", StaticFiles(directory="static"), name="static")

        @app.middleware("http")
        async def fixture_prefix(request, call_next):
            prefix = "/" + "a" * 40
            if request.scope["path"].startswith(prefix + "/"):
                request.scope["path"] = request.scope["path"][len(prefix):]
                request.scope["raw_path"] = request.scope["path"].encode()
            return await call_next(request)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            print("FIXTURE_PORT=" + str(listener.getsockname()[1]), flush=True)
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
