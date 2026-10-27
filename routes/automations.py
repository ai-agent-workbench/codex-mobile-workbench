"""Owner-scoped, same-origin automation management; never exposes an RPC socket."""
from __future__ import annotations

import sqlite3
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from engine.conversation_service import conversation_service
from engine.codex_app_server import AppServerError
from routes.settings_center import body
from shared.html_lib import html, nav, templates
from shared.security import audit, logged_in, require_auth
from shared.diagnostics import record_exception

router = APIRouter()
HEADERS = {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}


def reply(value, status=200):
    return JSONResponse(value, status_code=status, headers=HEADERS)


def auth(request):
    denied = require_auth(request)
    if denied is not None:
        if denied.headers.get('x-codex-auth-required') == '1':
            return None, denied
        return None, reply({'error':'请重新登录后管理定时任务。'},403 if denied.status_code==403 else 401)
    owner = logged_in(request)
    return (owner,None) if owner else (None,reply({'error':'请重新登录。'},401))


def failed(error):
    if isinstance(error, ValueError):
        message=str(error)
        status=409 if 'revision conflict' in message else 404 if 'does not exist' in message or 'does not belong' in message else 400
        if status==409: message='任务已被其他操作修改，请重新读取后比较；输入已保留。'
        if status==404: message='任务或对话不存在，或不属于当前账号。'
        return reply({'error':message},status)
    record_exception('automation_api_error', error)
    return reply({'error':'定时任务暂不可用，未报告成功，请保留输入后重新查询。'},503)


def scope(service, owner, conversation_id):
    if not isinstance(conversation_id,str) or not conversation_id:
        raise ValueError('缺少对话标识。')
    try:value = service.store.get_conversation(conversation_id)
    except AppServerError as error:raise ValueError('conversation does not exist') from error
    if value.get('owner_account_id') != owner:
        raise ValueError('conversation does not belong to owner')
    return value


@router.get('/settings/automations')
@router.get('/automations')
def automation_page(request: Request):
    denied = require_auth(request)
    if denied is not None:
        return denied
    response=html('定时任务',templates.get_template('automations.html').render(nav_html=nav('settings')))
    response.headers.update(HEADERS)
    return response


@router.get('/settings/automations/api/tasks')
@router.get('/automations/api/tasks')
def tasks(request: Request, conversation_id: str = '', summary: bool = False):
    owner,denied=auth(request)
    if denied is not None:return denied
    try:
        service=conversation_service()
        if conversation_id: scope(service,owner,conversation_id)
        values=service.automations.list(owner,conversation_id or None)
        if summary:
            values=[{key:item[key] for key in ('id','title','state','next_run_at')} for item in values]
        return reply({'tasks':values,
                      'conversations':[] if summary else service.automation_conversations(owner),
                      'module_enabled':service.automations_enabled,
                      'scheduler_running':service.scheduler_running})
    except (ValueError,AppServerError,OSError,sqlite3.Error) as error:return failed(error)


@router.post('/settings/automations/api/tasks')
@router.post('/automations/api/tasks')
async def create_task(request: Request):
    owner,denied=auth(request)
    if denied is not None:return denied
    try:
        value=await body(request)
        if set(value)-{'conversation_id','title','prompt','schedule','request_key','confirm_unattended'}:
            raise ValueError('包含不支持的字段。')
        if value.get('confirm_unattended') is not True:
            raise ValueError('请确认按该对话当前权限无人值守执行。')
        if not isinstance(value.get('request_key'),str) or not value['request_key']:
            raise ValueError('缺少请求标识，请刷新后重试。')
        service=conversation_service();scope(service,owner,value.get('conversation_id'))
        service.require_automations_enabled()
        task=service.automations.create(value.get('conversation_id'),owner,title=value.get('title'),prompt=value.get('prompt'),schedule=value.get('schedule'),request_key='web:'+owner+':'+value['request_key'])
        audit(request,'automation_created',task['id'])
        return reply({'task':task})
    except (ValueError,AppServerError,OSError,sqlite3.Error,TypeError) as error:return failed(error)


@router.post('/settings/automations/api/tasks/{task_id}')
@router.post('/automations/api/tasks/{task_id}')
async def update_task(request: Request, task_id: str):
    owner,denied=auth(request)
    if denied is not None:return denied
    try:
        value=await body(request)
        if set(value)-{'revision','title','prompt','schedule','state','request_key','confirm_unattended'}:
            raise ValueError('包含不支持的字段。')
        if any(key in value for key in ('title','prompt','schedule')) or value.get('state')=='active':
            if value.get('confirm_unattended') is not True:
                raise ValueError('请确认按任务原权限无人值守执行。')
        if not isinstance(value.get('request_key'),str) or not value['request_key']:
            raise ValueError('缺少请求标识，请刷新后重试。')
        service=conversation_service()
        changes={key:value[key] for key in ('title','prompt','schedule','state') if key in value}
        if set(changes) != {'state'} or changes.get('state') not in {'paused', 'deleted'}:
            service.require_automations_enabled()
        existing=service.automations.get(task_id,owner)
        # Linearize UI pause/delete/edit against queued turn startup. Once this
        # returns, no old queued turn can cross the native dispatch boundary.
        async with service.runtime._conversation_locks[existing['conversation_id']]:
            task=service.automations.update(task_id,owner,revision=value.get('revision'),request_key='web:'+owner+':'+value['request_key'],**changes)
        audit(request,'automation_updated',task['id'])
        return reply({'task':task})
    except (ValueError,AppServerError,OSError,sqlite3.Error,TypeError) as error:return failed(error)


@router.get('/settings/automations/api/tasks/{task_id}/runs')
@router.get('/automations/api/tasks/{task_id}/runs')
def task_runs(request: Request, task_id: str, limit: int = 30):
    owner,denied=auth(request)
    if denied is not None:return denied
    try:
        service=conversation_service();service.automations.refresh_runs()
        return reply({'task':service.automations.get(task_id,owner),'runs':service.automations.runs(task_id,owner,limit=limit)})
    except (ValueError,AppServerError,OSError,sqlite3.Error) as error:return failed(error)
