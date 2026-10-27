"""Resolve new conversation preferences without mutating existing threads."""
from __future__ import annotations

import asyncio
import sqlite3

from engine.codex_app_server import AppServerError
from shared.conversation_catalog import new_conversation_model
from shared.workbench_settings import settings_store


def validate_catalog_defaults(values, catalog, *, skills=True):
    models = catalog.get('models', [])
    model = next((row for row in models if (row.get('model') or row.get('id')) == values['model']), None)
    if model is None:
        raise ValueError('所选模型目前不可用，请刷新目录后重新选择。')
    efforts = [row.get('reasoningEffort') for row in model.get('supportedReasoningEfforts', [])]
    if values['effort'] not in efforts:
        raise ValueError('所选模型不支持此思考等级，请重新选择。')
    tiers = [row.get('id') for row in model.get('serviceTiers', [])]
    if values.get('service_tier') is not None and values['service_tier'] not in tiers:
        raise ValueError('所选模型不支持此服务速度，请重新选择。')
    if skills:
        available = [row.get('name') for row in catalog.get('skills', []) if row.get('enabled')]
        if any(available.count(name) != 1 for name in values.get('skills', [])):
            raise ValueError('默认技能已不可用或存在同名歧义，请在此项目的设置中重新选择。')


async def new_defaults(service, account, project_id=None, cwd=None, explicit=None):
    try:
        resolved = settings_store().resolve(account, project_id)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError('工作台默认设置暂不可读，未新建对话。') from exc
    values = resolved['values']
    supplied = {k: v for k, v in (explicit or {}).items() if v is not None}
    # An explicit empty service-tier means Standard, not inheritance.
    if 'service_tier' in supplied:
        supplied['service_tier'] = supplied['service_tier'] or None
    values.update(supplied)
    configured = any(resolved['sources'][k] != 'system'
                     for k in ('model', 'effort', 'service_tier', 'skills'))
    needs_model = 'model' not in supplied and resolved['sources']['model'] == 'system'
    if needs_model or configured:
        try:
            catalog = await asyncio.wait_for(service.catalog(cwd), 15)
        except (AppServerError, OSError, ValueError, asyncio.TimeoutError):
            if configured:
                raise ValueError('模型目录暂不可用，无法核验已保存的默认配置；未新建对话。')
            catalog = {}
        if needs_model:
            values['model'] = new_conversation_model(catalog)
        if configured:
            validate_catalog_defaults(values, catalog)
    return {**{key: values[key] for key in ('model', 'effort', 'permission', 'service_tier', 'collaboration_mode', 'completion_notification')},
            'web_preferences': {'personal': resolved['personal'], 'followup': values['followup'], 'skills': values['skills']}}
