"""Loopback-only, disposable real WebAuthn fixture; never imports the live app."""
import json
import os
from pathlib import Path
import sys


root = Path(sys.argv[1]).resolve()
port = int(sys.argv[2])
if root.parent != Path("/tmp") or not root.name.startswith("codex-pwa-browser-"):
    raise SystemExit("disposable /tmp fixture required")
sys.path.insert(0, str(Path(__file__).parents[1]))
os.environ.update({
    "CODEX_WEB_STATE_DIR": str(root / "state"),
    "CODEX_WEB_PASSKEY_ENABLED": "1", "CODEX_WEB_PASSKEY_RP_ID": "localhost",
    "CODEX_WEB_PASSKEY_ORIGIN": f"https://localhost:{port}",
})
state = root / "state"
state.mkdir(mode=0o700)
(state / "accounts.json").write_text(json.dumps({"fixture-admin": {"role": "admin", "enabled": True}}))
(state / "totp_enabled").touch()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from shared import security
from shared.html_lib import html, nav
from shared.passkey_auth import issue_registration_authorization
from routes.auth import router as auth_router
from routes.passkey import router as passkey_router
from routes.misc import service_worker
from routes.ws import router as ws_router, manager as ws_manager

security.notify_message = lambda *args, **kwargs: (False, "fixture-no-egress")
recovery_code, _ = issue_registration_authorization("fixture-admin")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
from middleware.request_origin import RequestOriginMiddleware
app.add_middleware(RequestOriginMiddleware, router=app.router)
attempts = []


class ObserveFixtureRequests:
    """Observe only disposable test traffic; never retain cookie values."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        observation = None
        if scope.get('path') in {'/fixture/change', '/ws'}:
            headers = dict(scope['headers'])
            observation = {
                'kind': scope['type'],
                'foreign': headers.get(b'origin') != f'https://localhost:{port}'.encode(),
                'cookie': b'__Host-codex_admin_session=' in headers.get(b'cookie', b''),
                'privacy_redacted': headers.get(b'origin') == b'null' and not headers.get(b'referer'),
                'site': headers.get(b'sec-fetch-site', b'').decode(),
                'mode': headers.get(b'sec-fetch-mode', b'').decode(),
                'dest': headers.get(b'sec-fetch-dest', b'').decode(),
                'content_type': headers.get(b'content-type', b'').decode(),
            }
            attempts.append(observation)
        async def observed_send(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []),
                                                  (b'referrer-policy', b'no-referrer')]}
            if observation is not None:
                if message['type'] == 'http.response.start':
                    observation['status'] = message['status']
                if message['type'] == 'websocket.accept':
                    observation['accepted'] = True
                if message['type'] == 'websocket.close':
                    observation['close'] = message['code']
            await send(message)
        await self.app(scope, receive, observed_send)


app.add_middleware(ObserveFixtureRequests)
app.include_router(auth_router)
app.include_router(passkey_router)
app.include_router(ws_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parents[1] / "static"))
app.add_api_route("/sw.js", service_worker)


@app.get("/fixture/recovery-code")
def fixture_code():
    return JSONResponse({"code": recovery_code}, headers={"Cache-Control": "no-store"})


changes = []


@app.post('/fixture/change')
async def fixture_change(request: Request):
    blocked = security.require_auth(request)
    if blocked is not None:
        return blocked
    changes.append(len(await request.body()))
    return JSONResponse({'ok': True})


@app.get('/fixture/effects')
def fixture_effects(request: Request):
    blocked = security.require_auth(request)
    if blocked is not None:
        return blocked
    return JSONResponse({'changes': len(changes), 'sockets': len(ws_manager._connections), 'attempts': attempts})


@app.get("/home")
@app.get("/conversation/cold-start")
def fixture_home(request: Request):
    response = security.require_auth(request)
    if response is not None:
        return response
    return html("PWA 登录验证夹具", nav("home") + """
      <main id="native-conversation"><h1 id="fixture-authenticated">已通过真实管理员会话校验</h1>
      <textarea id="fixture-draft" data-draft-key="fixture-draft" aria-label="草稿"></textarea></main>
    """)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, ssl_certfile=str(root / "cert.pem"),
                ssl_keyfile=str(root / "key.pem"), access_log=False, log_level="error")
