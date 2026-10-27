"""Loopback-only TLS fixture, real APIs/storage, isolated fake native runtime."""
import json
from datetime import datetime, timezone
import copy
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn
import projects
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from shared.workbench_settings import SettingsStore
from shared import project_rules, conversation_defaults
from shared import config as shared_config
from shared.skill_catalog import LABELS
from routes import settings_center, misc, conversations, conversation_api, automations, projects as project_routes
from tests.test_conversation_runtime import FakeFactory
from tests.test_settings_center import CATALOG
from itsdangerous import URLSafeTimedSerializer


def main():
    with tempfile.TemporaryDirectory(prefix='settings-browser-',dir=Path.home()) as temporary:
        root=Path(temporary);projects.ROOT=root/'projects';projects.create('fixture')
        project_rules.STATE=root/'state'
        settings=SettingsStore(root/'state'/'settings.sqlite3')
        settings_center.settings_store=lambda:settings
        conversation_defaults.settings_store=lambda:settings
        project_routes.settings_store=lambda:settings
        store=ConversationStore(root/'state'/'conversations.sqlite3')
        artifacts=ConversationArtifactService(store,root/'artifacts')
        factory=FakeFactory()
        native=ConversationRuntime(store,artifacts,factory)
        service=ConversationService(store,artifacts,native,enable_automations=True)
        service._automation_owner_enabled=lambda owner:owner=='browser'
        service.scheduler.owner_enabled=service._automation_owner_enabled
        catalog=copy.deepcopy(CATALOG)
        catalog['skills'] += [
            {'name':name,'description':'Fixture skill description','enabled':True,
             'scope':'repo' if name.startswith('daily-report-') else 'user'} for name in LABELS
        ] + [{'name':'fixture-long-skill-'+str(i)+'-with-a-long-technical-name',
              'description':'用于验证长列表与搜索的测试技能。','scope':'user','enabled':True} for i in range(25)]
        catalog['skills'].append({'name':'disabled-skill','description':'已停用的测试技能','scope':'user','enabled':False})
        service.catalog=mock.AsyncMock(return_value=catalog)
        import engine.conversation_service as service_module
        service_module.CONVERSATION_WORKSPACES=root/'temporary-conversations'
        for module in (settings_center,misc,conversations,conversation_api,automations,project_routes):
            module.require_auth=lambda request:None
            module.logged_in=lambda request:'browser'
            module.conversation_service=lambda:service
            module.audit=mock.Mock()
        conversations.classify_client_origin=lambda _:('mobile_web',{},{} )
        conversation_api.classify_client_origin=conversations.classify_client_origin
        settings_center.confirmation_signer=lambda:URLSafeTimedSerializer('isolated-browser-only')
        misc._read_codex_weekly_quota=mock.AsyncMock(return_value={'remaining_percent':72,'resets_at':1893456000,'plan_type':'pro'})
        class DiagnosticClient:
            server_version='fixture-version'
            async def connect(self):return None
            async def list_all_models(self,**kwargs):return CATALOG['models']
            async def close(self):return None
        settings_center.CodexAppServerClient=lambda _:DiagnosticClient()
        settings_center.STATE=root
        app=FastAPI()
        from middleware.request_origin import RequestOriginMiddleware
        app.add_middleware(RequestOriginMiddleware, router=app.router)
        @app.middleware('http')
        async def privacy_policy(request, call_next):
            response = await call_next(request)
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response
        for module in (settings_center,misc,conversations,conversation_api,automations,project_routes):app.include_router(module.router)
        app.mount('/static',StaticFiles(directory=Path(__file__).resolve().parents[1]/'static'))
        @app.on_event('startup')
        async def start_automations(): service.scheduler.start()
        @app.on_event('shutdown')
        async def stop_automations(): await service.scheduler.close()
        async def finish_fixture(conversation_id,text):
            thread_id=store.get_conversation(conversation_id)['thread_id'];thread=factory.threads[thread_id]
            turn=thread['turns'][-1];turn.update(status='completed',items=[{'id':'answer-'+turn['id'],'type':'agentMessage','phase':'final_answer','text':text}]);thread['status']={'type':'idle'}
            await native.handle_notification({'method':'item/completed','params':{'threadId':thread_id,'turnId':turn['id'],'item':copy.deepcopy(turn['items'][0])}})
            await native.handle_notification({'method':'turn/completed','params':{'threadId':thread_id,'turn':copy.deepcopy(turn)}})
        @app.post('/fixture/automation-seed')
        async def automation_seed():
            conversation=service.create_draft(cwd=str(root/'projects'/'fixture'),project_id='fixture',model='fixture',effort='medium',permission='read-only',owner_account_id='browser')
            await service.start(conversation['id'],command_id='seed-'+conversation['id'],text='自动化测试对话')
            await finish_fixture(conversation['id'],'准备就绪')
            return {'conversation_id':conversation['id']}
        @app.post('/fixture/automation-tick')
        async def automation_tick(request:Request):
            value=await request.json();task=service.automations.get(value['task_id'],'browser')
            now=datetime.now(timezone.utc);store._db.execute('UPDATE conversation_automation_tasks SET next_run_at=? WHERE id=?',(now.isoformat().replace('+00:00','Z'),task['id']))
            await service.scheduler.tick(now)
            await finish_fixture(task['conversation_id'],value['text']);service.automations.refresh_runs()
            return {'runs':service.automations.runs(task['id'],'browser')}
        @app.get('/login/status')
        def login():return JSONResponse({'authenticated':True,'method':'passkey','passkey_enabled':True})
        @app.get('/sw.js')
        def sw():return Response('',media_type='application/javascript')
        @app.post('/fixture/conflict')
        def conflict():
            current=settings.read_account('browser');settings.save_account('browser',current['revision'],{'personal':'其他页面刚修改'})
            return {'ok':True}
        @app.post('/fixture/external-rule')
        def external_rule():
            (projects.ROOT/'fixture'/'AGENTS.md').write_text('外部修改，请勿覆盖')
            return {'ok':True}
        @app.post('/fixture/missing-skill')
        def missing_skill():
            current=settings.read_project('browser','fixture')
            settings.save_project('browser','fixture',current['revision'],{**current['defaults'],'skills':['removed-skill']})
            return {'ok':True}
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        gateway=None
        try:
            if '--proxy' in sys.argv:
                nginx=shutil.which('nginx')
                if not nginx:raise RuntimeError('nginx is required for the real gateway regression')
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
                conf=root/'nginx.conf'
                conf.write_text(f'''pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 128; }}
http {{
  access_log off;
  client_body_temp_path {root}/body;
  proxy_temp_path {root}/proxy;
  server {{
    listen 127.0.0.1:{port} ssl;
    ssl_certificate {cert};
    ssl_certificate_key {key};
    add_header Referrer-Policy no-referrer always;
    # Match the production gateway: unlisted root automation paths are blocked.
    location ~ ^/automations(/|$) {{ return 404; }}
    location / {{
      proxy_pass http://127.0.0.1:{sock.getsockname()[1]};
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_http_version 1.1;
      proxy_buffering off;
    }}
  }}
}}
''')
                gateway=subprocess.Popen([nginx,'-p',str(root)+'/', '-c',str(conf),'-g','daemon off; master_process off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                for _ in range(100):
                    if gateway.poll() is not None:raise RuntimeError('isolated nginx failed: '+gateway.stderr.read().decode())
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                    except OSError:time.sleep(.02)
                else:raise RuntimeError('isolated nginx did not become ready')
                shared_config.PASSKEY_ORIGIN='https://127.0.0.1:'+str(port)
                config=uvicorn.Config(app,host='127.0.0.1',log_level='error',proxy_headers=True,forwarded_allow_ips='127.0.0.1')
            else:
                port=sock.getsockname()[1]
                shared_config.PASSKEY_ORIGIN=''
                config=uvicorn.Config(app,host='127.0.0.1',log_level='error',ssl_certfile=str(cert),ssl_keyfile=str(key))
            print(json.dumps({'port':port,'gateway':'nginx' if gateway else 'direct'}),flush=True)
            uvicorn.Server(config).run(sockets=[sock])
        finally:
            if gateway:
                gateway.terminate()
                try:gateway.wait(timeout=5)
                except subprocess.TimeoutExpired:gateway.kill();gateway.wait()
            sock.close();store.close()

if __name__=='__main__':main()
