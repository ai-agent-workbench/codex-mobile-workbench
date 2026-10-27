"""Opt-in isolated real-model gate; creates no production tasks or services."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.codex_app_server import CodexAppServerClient
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService


async def settle(store,count,timeout=180):
    for _ in range(int(timeout*4)):
        value=store.snapshot('automation-probe')
        if value['conversation']['state']=='needs_user':raise RuntimeError('native probe requires recovery: '+str(value['conversation'].get('recovery_state'))+' / '+json.dumps(value['conversation'].get('last_error')))
        if value['conversation']['state']=='idle' and len([t for t in value['turns'] if t['status']=='completed'])==count:return value
        await asyncio.sleep(.25)
    raise RuntimeError('native probe timed out')


async def run():
    if not (urllib.request.getproxies().get('https') or urllib.request.getproxies().get('all')):
        raise RuntimeError('verified proxy environment is unavailable')
    with tempfile.TemporaryDirectory(prefix='automation-real-',dir=f'/run/user/{os.getuid()}') as tmp:
        root=Path(tmp);workspace=root/'workspace';workspace.mkdir();socket=root/'native.sock'
        binary=Path('/opt/codex-web-app-server/current/bin/codex')
        if not binary.is_file():raise RuntimeError('current native binary unavailable')
        process=await asyncio.create_subprocess_exec(str(binary),'app-server','--listen','unix://'+str(socket),cwd=workspace,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        service=None;thread=None;deleted=False
        try:
            for _ in range(200):
                if socket.exists():break
                if process.returncode is not None:raise RuntimeError('isolated native server exited')
                await asyncio.sleep(.05)
            probe=CodexAppServerClient(socket);await probe.connect();models=await probe.list_all_models(force_refresh=True);version=probe.server_version
            requested=os.environ.get('CODEX_PROBE_MODEL');model=next((m for m in models if (m['model']==requested if requested else 'luna' in m['model'])),next(m for m in models if m.get('isDefault')))
            effort='low' if any(x['reasoningEffort']=='low' for x in model.get('supportedReasoningEfforts',[])) else model['defaultReasoningEffort'];await probe.close()
            def make_service():
                store=ConversationStore(root/'state.sqlite3');artifacts=ConversationArtifactService(store,root/'artifacts');runtime=ConversationRuntime(store,artifacts,lambda **handlers:CodexAppServerClient(socket,**handlers))
                result=ConversationService(store,artifacts,runtime);result._automation_owner_enabled=lambda owner:owner=='probe-owner';result.scheduler.owner_enabled=result._automation_owner_enabled;return result
            service=make_service();service.store.create_conversation('automation-probe',title='isolated automation test',cwd=str(workspace),settings={'permission':'read-only','model':model['model'],'effort':effort},owner_account_id='probe-owner')
            await service.start('automation-probe',command_id='create-probe',text='这是隔离自动化验收。请用 manage_scheduled_task 创建一个每隔1分钟执行的任务，名称“隔离定时验收”，每轮执行内容必须是“只回复 SCHEDULED_RUN_OK，不调用任何工具，不创建或修改任务”。用户已授权只读无人值守执行。只创建这一项，不使用 shell 或其他调度工具。成功后简短确认。')
            value=await settle(service.store,1);thread=value['conversation']['thread_id'];tasks=service.automations.list('probe-owner');assert len(tasks)==1,'AI did not create exactly one task';task=tasks[0];assert task['state']=='active','readonly task not enabled'
            service.scheduler.start()
            await settle(service.store,2,150);service.automations.refresh_runs()
            # Reopen Web-owned state and transport; thread metadata must retain tools.
            await service.close();service=make_service();await service.startup()
            await service.send('automation-probe',command_id='resume-list',text='请调用 manage_scheduled_task 的 list 操作核对之前任务仍存在，不修改或创建任务。然后只回复 RESUME_LIST_OK。')
            await settle(service.store,3)
            calls=service.store._db.execute("SELECT COUNT(*) FROM tool_calls WHERE tool='manage_scheduled_task' AND state='completed'").fetchone()[0];assert calls>=2,'dynamic tool did not survive resume'
            service.scheduler.start();await settle(service.store,4,150);await settle(service.store,5,150);service.automations.refresh_runs()
            runs=service.automations.runs(task['id'],'probe-owner');assert len(runs)==3 and all(r['status']=='completed' for r in runs),'three real runs did not complete'
            snapshot=service.public_snapshot('automation-probe');scheduled=[t for t in snapshot['turns'] if any(c['origin']=='scheduled' for c in t['commands'])];assert len(scheduled)==3 and all('SCHEDULED_RUN_OK' in (t['final_text'] or '') for t in scheduled)
            task=service.automations.get(task['id'],'probe-owner');service.automations.update(task['id'],'probe-owner',revision=task['revision'],state='paused');await service.scheduler.close();await service.delete('automation-probe');deleted=True
            return {'ok':True,'server_version':version,'model':model['model'],'real_scheduled_runs':3,'same_thread':True,'restart_recovery':True,'dynamic_tool_after_resume':True,'test_thread_deleted':True}
        finally:
            if service:
                if not thread:
                    try:thread=service.store.get_conversation('automation-probe').get('thread_id')
                    except Exception:pass
                if thread and not deleted:
                    try:
                        await service.scheduler.close();await service.runtime._ensure_client();await service.runtime._client.request('thread/delete',{'threadId':thread})
                    except Exception:pass
                await service.close()
            if process.returncode is None:
                process.terminate()
                try:await asyncio.wait_for(process.wait(),5)
                except asyncio.TimeoutError:process.kill();await process.wait()

if __name__=='__main__':print(json.dumps(asyncio.run(run()),ensure_ascii=False))
