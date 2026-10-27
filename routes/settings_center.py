"""Authenticated self-service settings; never edits shared Codex configuration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from shared.diagnostics import record_exception
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature

import projects
from engine.codex_app_server import AppServerError, CodexAppServerClient
from engine.conversation_service import conversation_service
from shared.config import APP_ROOT, CODEX_APP_SERVER_SOCKET, STATE
from shared.conversation_catalog import catalog_models, new_conversation_model
from shared.conversation_defaults import validate_catalog_defaults
from shared.project_rules import read_rule, preview_rule, write_rule
from shared.security import audit, confirmation_signer, logged_in, require_auth
from shared.request_origin import require_same_https_origin
from shared.skill_catalog import skill_choices
from shared.workbench_settings import SettingsConflict, _defaults, _personal, _presets, settings_store, system_defaults

router = APIRouter()
HEADERS = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}
RANK = {'read-only': 0, 'workspace-write': 1, 'danger-full-access': 2}
# A fingerprint of the loaded settings implementation, not a mutable Git HEAD.
BUILD = hashlib.sha256(Path(__file__).read_bytes() + (APP_ROOT / 'app.py').read_bytes()).hexdigest()[:12]


def reply(value, status=200):
    return JSONResponse(value, status_code=status, headers=HEADERS)


def auth(request):
    denied = require_auth(request)
    if denied is not None:
        if denied.headers.get('x-codex-auth-required') == '1':
            return None, denied
        return None, reply({'error': '请重新登录后继续，未保存内容仍保留。'}, 403 if denied.status_code == 403 else 401)
    account = logged_in(request)
    if not account:
        return None, reply({'error': '请重新登录。'}, 401)
    return account, None


async def body(request):
    try:
        require_same_https_origin(request)
    except ValueError as exc:
        raise ValueError('请求来源不匹配，请从工作台页面重试。') from exc
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise ValueError('请求格式不正确。')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 600 * 1024:
            raise ValueError('请求内容过大。')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('请求内容不是有效 JSON。') from exc
    if not isinstance(value, dict):
        raise ValueError('请求格式不正确。')
    return value


def failed(error):
    record_exception("settings_api_error", error)
    if isinstance(error, SettingsConflict):
        return reply({'error': str(error), 'code': 'conflict'}, 409)
    if isinstance(error, (OSError, sqlite3.Error, AppServerError, json.JSONDecodeError, LookupError, TypeError, UnicodeError)):
        return reply({'error': '暂时无法读取或保存；未报告成功，请保留输入后重试。'}, 503)
    return reply({'error': str(error)}, 400)


async def get_catalog(project_id=''):
    cwd = projects.get(project_id)['path'] if project_id else None
    return await asyncio.wait_for(conversation_service().catalog(cwd), 15)


def effective_values(account, project, catalog):
    values = {**system_defaults(), **account['defaults'], **(project or {}).get('defaults', {})}
    if 'model' not in account['defaults'] and 'model' not in (project or {}).get('defaults', {}):
        values['model'] = new_conversation_model(catalog)
    return values


@router.get('/settings/api/config')
async def configuration(request: Request, project_id: str = ''):
    account_id, denied = auth(request)
    if denied is not None:
        return denied
    try:
        if project_id:
            projects.get(project_id)
        store = settings_store()
        account = store.read_account(account_id)
        project = store.read_project(account_id, project_id) if project_id else None
        sources = store.resolve(account_id, project_id or None)['sources']
        unavailable = False
        try:
            catalog = await get_catalog(project_id)
        except (AppServerError, OSError, ValueError, asyncio.TimeoutError) as error:
            record_exception("settings_catalog_unavailable", error)
            catalog, unavailable = {}, True
        effective = effective_values(account, project, catalog)
        raw_names = [row.get('model') or row.get('id') for row in catalog.get('models', [])]
        models = [{**row, 'available': row['model'] in raw_names} for row in catalog_models(catalog, effective)]
        skills = skill_choices(catalog, include_disabled=True)
        return reply({'account': account, 'project': project, 'effective': effective, 'sources': sources,
                      'system': {**system_defaults(), 'model': new_conversation_model(catalog)},
                      'models': models, 'skills': skills, 'catalog_error': unavailable,
                      'projects': [{'id': p['id'], 'name': p['name']} for p in projects.recent()]})
    except (OSError, ValueError, sqlite3.Error) as exc:
        return failed(exc)


@router.post('/settings/api/config')
async def save_configuration(request: Request):
    account_id, denied = auth(request)
    if denied is not None:
        return denied
    try:
        data = await body(request)
        if set(data) - {'project_id', 'revision', 'account_revision', 'changes', 'confirm_permission'}:
            raise ValueError('设置请求包含不支持的字段。')
        project_id = data.get('project_id') or ''
        if not isinstance(project_id, str):
            raise ValueError('项目标识不正确。')
        if project_id:
            projects.get(project_id)
        store = settings_store()
        account = store.read_account(account_id)
        project = store.read_project(account_id, project_id) if project_id else None
        target = project if project_id else account
        if data.get('revision') != target['revision'] or (project_id and data.get('account_revision') != account['revision']):
            raise SettingsConflict('设置已在其他页面更改，输入已保留；请重新读取后比较。')
        changes = data.get('changes')
        if not isinstance(changes, dict) or not changes or set(changes) - ({'defaults'} if project_id else {'defaults', 'personal', 'presets'}):
            raise ValueError('设置更新格式不正确。')
        if 'personal' in changes:
            _personal(changes['personal'])
        if 'defaults' in changes:
            _defaults(changes['defaults'])
        if 'presets' in changes:
            _presets(changes['presets'])
        if 'defaults' in changes or 'presets' in changes:
            try:
                catalog = await get_catalog(project_id)
            except (AppServerError, OSError, ValueError, asyncio.TimeoutError) as exc:
                raise AppServerError('catalog unavailable') from exc
            before = effective_values(account, project, catalog)
            new_account = {**account, **changes} if not project_id else account
            new_project = {**project, **changes} if project_id else None
            after = effective_values(new_account, new_project, catalog)
            validate_catalog_defaults(after, catalog)
            for preset in changes.get('presets', []):
                validate_catalog_defaults({**system_defaults(), **preset['defaults']}, catalog)
            if RANK[after['permission']] > RANK[before['permission']] and data.get('confirm_permission') is not True:
                return reply({'error': '默认权限将扩大，需确认影响范围后保存。', 'code': 'permission_confirmation'}, 409)
        if project_id:
            saved = store.save_project(account_id, project_id, data['revision'], changes['defaults'])
        else:
            saved = store.save_account(account_id, data['revision'], changes)
        audit(request, 'workbench_settings_saved', 'project' if project_id else 'account', account=account_id)
        return reply({'saved': saved})
    except (OSError, ValueError, sqlite3.Error, AppServerError) as exc:
        return failed(exc)


@router.get('/settings/api/rules')
def rules(request: Request, project_id: str, name: str = 'AGENTS.md'):
    _, denied = auth(request)
    if denied is not None:
        return denied
    try:
        return reply(read_rule(project_id, name))
    except (OSError, ValueError, LookupError, TypeError) as exc:
        return failed(exc)


@router.post('/settings/api/rules/{action}')
async def change_rule(request: Request, action: str):
    account, denied = auth(request)
    if denied is not None:
        return denied
    try:
        data = await body(request)
        if set(data) - {'project_id', 'name', 'version', 'content', 'token', 'remove'}:
            raise ValueError('规则请求包含不支持的字段。')
        project_id, name, version, content = (data.get(key) for key in ('project_id', 'name', 'version', 'content'))
        if not all(isinstance(value, str) for value in (project_id, name, version, content)):
            raise ValueError('规则请求格式不正确。')
        remove = data.get('remove', False)
        if type(remove) is not bool:
            raise ValueError('恢复方式不正确。')
        binding = {'remove': remove, 'account': account, 'project': project_id, 'name': name, 'version': version,
                   'digest': hashlib.sha256(content.encode()).hexdigest(), 'purpose': 'project-rule'}
        if action == 'preview':
            diff = preview_rule(project_id, name, version, content, remove=remove)
            return reply({'diff': diff or '内容未改变。', 'token': confirmation_signer().dumps(binding)})
        if action != 'save':
            raise ValueError('不支持此操作。')
        try:
            signed = confirmation_signer().loads(data.get('token', ''), max_age=600)
        except (BadSignature, TypeError) as exc:
            raise ValueError('差异确认已过期，请重新预览。') from exc
        if signed != binding:
            raise ValueError('预览后内容有变化，请重新预览。')
        result = write_rule(project_id, name, version, content, remove=remove)
        audit(request, 'project_rule_saved', name, account=account)
        return reply(result)
    except (OSError, ValueError, LookupError, TypeError) as exc:
        return failed(exc)


@router.get('/settings/api/diagnostics')
async def diagnostics(request: Request):
    _, denied = auth(request)
    if denied is not None:
        return denied
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'web': '正常', 'build': BUILD,
              'app_server': '不可用', 'model_count': 0, 'disk_free_gb': None}
    client = CodexAppServerClient(CODEX_APP_SERVER_SOCKET)
    try:
        await asyncio.wait_for(client.connect(), 10)
        models = await asyncio.wait_for(client.list_all_models(include_hidden=False, force_refresh=True), 10)
        result.update(app_server=client.server_version, model_count=len(models))
    except (OSError, ValueError, AppServerError, asyncio.TimeoutError):
        pass
    finally:
        try:
            await asyncio.wait_for(client.close(), 3)
        except (OSError, ValueError, AppServerError, asyncio.TimeoutError):
            result['app_server'] = '连接关闭异常'
    try:
        result['disk_free_gb'] = round(shutil.disk_usage(STATE).free / (1024 ** 3), 1)
    except OSError:
        pass
    return reply(result)
