"""Render the real Jinja conversation composers for browser regression tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.conversation_catalog import catalog_models
from shared.html_lib import templates, upload_button, upload_panel


MODELS = [
    {
        "model": "gpt-6-sol",
        "name": "GPT-6 Sol",
        "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default_effort": "medium",
        "tiers": [{"id": "priority", "name": "Fast"}],
    },
    {
        "model": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "efforts": ["low", "medium", "high"],
        "default_effort": "medium",
        "tiers": [],
    },
    {
        "model": "gpt-5.6-sol",
        "name": "GPT-5.6-Sol",
        "efforts": ["medium", "high", "ultra"],
        "default_effort": "medium",
        "tiers": [{"id": "priority", "name": "Fast"}],
    },
    {
        "model": "gpt-5.6-terra",
        "name": "GPT-5.6-Terra",
        "efforts": ["low", "medium", "high", "ultra"],
        "default_effort": "medium", "tiers": [],
    },
    {
        "model": "gpt-5.6-luna",
        "name": "GPT-5.6-Luna",
        "efforts": ["low", "medium"],
        "default_effort": "medium",
        "tiers": [],
    },
]

# Exercise the same presentation filter as both real routes, including models
# present in the native catalog but intentionally hidden in the mobile UI.
MODELS.insert(0, {
    "model": "gpt-6-astra", "name": "GPT-6 Astra",
    "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "default_effort": "medium", "tiers": [{"id": "priority", "name": "Fast"}],
})
MODELS.insert(0, {
    "model": "gpt-6.1-sol", "name": "GPT-6.1 Sol",
    "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "default_effort": "medium", "tiers": [{"id": "priority", "name": "Fast"}],
})
MODEL_CATALOG = {"models": [{
    "model": row["model"], "displayName": row["name"],
    "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in row["efforts"]],
    "defaultReasoningEffort": row["default_effort"], "serviceTiers": row["tiers"],
} for row in MODELS] + [{
    "model": model, "displayName": model,
    "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
    "defaultReasoningEffort": "medium",
} for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark")]}
MODELS = catalog_models(MODEL_CATALOG, {"model": "gpt-5.6-sol"})


def main() -> None:
    if os.environ.get('COMPACT_CONTROLS_LEGACY_FIXTURE') == '1':
        templates.env.globals.pop('compact_controls_ready', None)
    nav_html = (
        '<nav class="nav"><a href="#"><span>⌂</span>首页</a>'
        '<a href="#"><span>▤</span>项目</a><a href="#"><span>◷</span>历史</a>'
        '<a href="#"><span>▣</span>文件</a><a href="#"><span>⚙</span>设置</a></nav>'
    )
    conversation_settings = {
        "model": "gpt-5.6-sol",
        "effort": "ultra",
        "permission": "danger-full-access",
        "service_tier": "priority",
    }
    initial_snapshot = {
        "schema_version": 4,
        "conversation": {
            "id": "conversation-ui",
            "thread_id": "thread-1",
            "title": "手机产物与 App Server 对话",
            "project_id": None,
            "state": "idle",
            "active_turn_id": None,
            "thread_status": {"type": "idle"},
            "settings": conversation_settings,
            "recovery_state": None,
            "connection_state": "connected",
            "completion_notification": True,
            "pinned": False,
            "client_origin": "mobile_web",
            "revision": 1,
            "terminal_generation": 1,
            "last_terminal_turn_id": "turn-initial",
            "last_terminal_at": "2026-08-24T11:59:00Z",
            "usage": None,
            "created_at": "2026-08-24T11:58:00Z",
            "updated_at": "2026-08-24T11:59:00Z",
        },
        "turns": [{
            "id": "turn-initial",
            "ordinal": 1,
            "status": "completed",
            "started_at": "2026-08-24T11:58:00Z",
            "completed_at": "2026-08-24T11:59:00Z",
            "final_text": "首屏最近记录仍然可见。",
            "error": None,
            "commands": [{
                "id": "message-initial",
                "kind": "start",
                "state": "completed",
                "text": "首屏历史记录",
                "created_at": "2026-08-24T11:58:00Z",
            }],
            "items": [{
                "native_item_id": "msg_initial",
                "kind": "agent_message",
                "status": "completed",
                "phase": "final_answer",
                "text": "首屏最近记录仍然可见。",
                "created_at": "2026-08-24T11:59:00Z",
                "updated_at": "2026-08-24T11:59:00Z",
            }],
            "artifacts": [],
        }],
        "messages": [],
        "queue": [],
        "artifacts": [],
    }
    conversation = templates.get_template("conversation.html").render(
        conversation={
            "id": "conversation-ui",
            "thread_id": "thread-1",
            "title": "手机产物与 App Server 对话",
            "completion_notification": True,
            "client_origin": "mobile_web",
        },
        initial_snapshot=initial_snapshot,
        back_url="/home",
        project_name="Codex Web 工作台维护",
        nav_html=nav_html,
        upload_notice_html="",
        upload_button_html=upload_button("conversation-upload"),
        upload_panel_html=upload_panel(
            "/home/workbench/fixture", "/conversation/conversation-ui",
            "conversation-upload",
        ),
        models=MODELS,
        skills=[],
        settings=conversation_settings,
        catalog_error=False,
    )
    project_settings = {
        "model": "gpt-6-sol",
        "effort": "high",
        "permission": "danger-full-access",
        "service_tier": None,
    }
    project = templates.get_template("project_conversation_composer.html").render(
        project_id="project-one",
        models=MODELS,
        settings=project_settings,
        catalog_error=False,
        upload_button_html=upload_button("project-upload"),
        upload_panel_html=upload_panel(
            "/home/workbench/fixture", "/project/project-one", "project-upload"
        ),
    )
    project_catalog_error = templates.get_template(
        "project_conversation_composer.html"
    ).render(
        project_id="project-catalog-error",
        models=catalog_models({}, {**project_settings, "model": "gpt-5.6-sol", "effort": "medium"}),
        settings={**project_settings, "model": "gpt-5.6-sol", "effort": "medium"},
        catalog_error=True,
        upload_button_html=upload_button("project-catalog-error-upload"),
        upload_panel_html=upload_panel(
            "/home/workbench/fixture", "/project/project-catalog-error",
            "project-catalog-error-upload",
        ),
    )
    sound_settings = templates.get_template(
        "completion_sound_settings.html"
    ).render(nav_html=nav_html)
    tts_settings = templates.get_template("tts_settings.html").render(
        nav_html=nav_html
    )
    html_artifact_viewer = templates.get_template(
        "html_artifact_viewer.html"
    ).render(artifact_name=("这是一个用于验证手机窄屏不会被超长中文附件标题撑宽的交互式网页草图" * 2) + ".html")
    print(json.dumps({
        "conversation": conversation,
        "project": project,
        "project_catalog_error": project_catalog_error,
        "sound_settings": sound_settings,
        "tts_settings": tts_settings,
        "html_artifact_viewer": html_artifact_viewer,
        "page_shell": templates.get_template("base.html").render(
            title="Browser fixture", content="<!--fixture-content-->",
        ),
    }))


if __name__ == "__main__":
    main()
