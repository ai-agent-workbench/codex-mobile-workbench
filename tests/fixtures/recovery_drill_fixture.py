"""Loopback TLS fixture using one driver-owned isolated instance root."""
import asyncio, json, os, sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); port=int(sys.argv[2]); uds=Path(sys.argv[3]).resolve()
if not (root/'.recovery-drill-fixture').is_file() or uds.parent != root.parent: raise SystemExit('driver-owned fixture root required')
os.environ.clear(); os.environ.update({'HOME':str(root/'fixture-home'),'PATH':'/usr/bin:/bin','CODEX_WEB_STATE_DIR':str(root/'state'),'CODEX_WEB_PROJECTS_DIR':str(root/'workspace/projects'),'CODEX_WEB_INBOX_DIR':str(root/'workspace/inbox'),'CODEX_WEB_CONVERSATION_DIR':str(root/'workspace/conversations'),'CODEX_HOME':str(root/'native-home'),'CODEX_WEB_PASSKEY_ENABLED':'1','CODEX_WEB_PASSKEY_RP_ID':'localhost','CODEX_WEB_PASSKEY_ORIGIN':f'https://localhost:{port}','CODEX_WEB_DOT_ENABLED':'0','CODEX_WEB_AUTOMATIONS_ENABLED':'0','CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED':'0','NO_PROXY':'localhost,127.0.0.1'})
(root/'fixture-home').mkdir(mode=0o700,exist_ok=True)
if 'fixture-owner' not in json.loads((root/'state'/'accounts.json').read_text()):
 raise SystemExit('fixture account missing')
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import projects
from engine.codex_app_server import CodexAppServerClient
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from shared.passkey_auth import issue_registration_authorization
from shared import security
from routes.auth import router as auth_router
from routes.passkey import router as passkey_router
from routes.conversations import router as conversations_router
from routes.conversation_api import router as api_router
from routes.ws import router as ws_router
import engine.conversation_service as service_module
projects.ROOT=root/'workspace/projects'
security.notify_message=lambda *a,**k:(False,'fixture-no-egress')
store=ConversationStore(root/'state'/'conversations.sqlite3'); artifacts=ConversationArtifactService(store,root/'state'/'conversation-artifacts')
def client_factory(**handlers): return CodexAppServerClient(uds,**handlers)
runtime=ConversationRuntime(store,artifacts,client_factory); service=ConversationService(store,artifacts,runtime,enable_automations=False,enable_completion_notifications=False); service_module._service=service
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
from middleware.request_origin import RequestOriginMiddleware
app.add_middleware(RequestOriginMiddleware, router=app.router)
for router in (auth_router,passkey_router,conversations_router,api_router,ws_router): app.include_router(router)
app.mount('/static',StaticFiles(directory=Path(__file__).resolve().parents[2]/'static'))
@app.on_event('startup')
async def startup(): await service.startup()
@app.on_event('shutdown')
async def close(): await service.close(); store.close(); service_module._service=None
@app.get('/fixture/recovery-code')
def code():
 value,_=issue_registration_authorization('fixture-owner'); return JSONResponse({'code':value},headers={'Cache-Control':'no-store'})
@app.get('/fixture/seed')
@app.post('/fixture/seed')
async def seed(request:Request):
 try:
  blocked=security.require_auth(request)
  if blocked:return blocked
  existing=store._db.execute('SELECT id FROM conversations LIMIT 1').fetchone()
  if existing:return RedirectResponse('/conversation/'+existing[0],303)
  conversation=service.create_draft(cwd=str(root/'workspace/projects/project'),model='fixture',effort='medium',permission='read-only',owner_account_id='fixture-owner')
  await service.start(conversation['id'],command_id='start-drill',text='合成恢复演练任务')
  # Observe real runtime ingestion; never force a command into a success state.
  async with asyncio.timeout(8):
   while store.command('start-drill')['state'] != 'completed':
    await asyncio.sleep(.025)
  source=root/'workspace/projects/project'/'fixture-output.txt';source.write_bytes(b'recovery-drill-artifact-v1\n')
  artifacts.publish(conversation['id'],turn_id=None,source_path=str(source),idempotency_key='recovery-drill-output',origin='fixture')
  return RedirectResponse('/conversation/'+conversation['id'],303)
 except Exception as error:
  return JSONResponse({'error':type(error).__name__},status_code=500)
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=port,ssl_certfile=str(root/'fixture-cert.pem'),ssl_keyfile=str(root/'fixture-key.pem'),access_log=False,log_level='error')
