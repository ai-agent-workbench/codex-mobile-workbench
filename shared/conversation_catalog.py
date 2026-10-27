"""Presentation-safe helpers for App Server model and skill catalogs."""

from __future__ import annotations

from typing import Any

from shared.config import NEW_CONVERSATION_MODEL, NEW_CONVERSATION_REASONING
from shared.skill_catalog import skill_choices



def new_conversation_model(catalog: dict[str, Any]) -> str:
    """Prefer GPT-6 Sol only once the connected runtime advertises it.

    The fallback keeps new conversations usable during runtime promotion,
    rollback, or a temporary catalog outage. Saved/explicit choices never use
    this helper and are not migrated.
    """
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    if any(
        isinstance(row, dict)
        and (row.get("model") or row.get("id")) == NEW_CONVERSATION_MODEL
        for row in models
    ):
        return NEW_CONVERSATION_MODEL
    return "gpt-5.6-sol"


async def resolve_new_conversation_model(service: Any, cwd: str | None) -> str:
    from engine.codex_app_server import AppServerError

    try:
        catalog = await service.catalog(cwd)
    except (AppServerError, OSError, ValueError):
        catalog = {}
    return new_conversation_model(catalog)


def catalog_models(catalog: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    known: set[str] = set()
    current = str(settings.get("model") or NEW_CONVERSATION_MODEL)
    for raw in catalog.get("models", []) if isinstance(catalog, dict) else []:
        if not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or raw.get("id") or "").strip()
        if not model or model in known:
            continue
        known.add(model)
        efforts = [
            str(item.get("reasoningEffort"))
            for item in raw.get("supportedReasoningEfforts", [])
            if isinstance(item, dict) and item.get("reasoningEffort")
        ]
        tiers = [
            {
                "id": str(item.get("id")),
                "name": str(item.get("name") or item.get("id")),
            }
            for item in raw.get("serviceTiers", [])
            if isinstance(item, dict) and item.get("id")
        ]
        values.append({
            "model": model,
            "name": str(raw.get("displayName") or model),
            "efforts": efforts,
            "default_effort": str(raw.get("defaultReasoningEffort") or ""),
            "tiers": tiers,
        })
    if current and current not in known:
        current_tier = str(settings.get("service_tier") or "")
        values.insert(0, {
            "model": current,
            "name": current,
            "efforts": [str(settings.get("effort") or NEW_CONVERSATION_REASONING)],
            "default_effort": str(
                settings.get("effort") or NEW_CONVERSATION_REASONING
            ),
            "tiers": ([{
                "id": current_tier,
                "name": "Fast" if current_tier == "priority" else current_tier,
            }] if current_tier else []),
        })
    # Keep the App Server catalog order inside each series, but present the
    # current GPT-6 choices before legacy series in the compact mobile picker.
    return [value for value in values if value["model"].lower().startswith("gpt-6")] + [
        value for value in values if not value["model"].lower().startswith("gpt-6")
    ]


def catalog_skills(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return skill_choices(catalog)
