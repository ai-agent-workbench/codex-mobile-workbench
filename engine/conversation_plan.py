"""Stable Plan approval bindings and review contexts.

A browser only returns opaque digests.  The server derives both the Plan
binding and any later, delivered user supplements from the native projection.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


_DIGEST = "sha256"
_CONTEXT_DOMAIN = "codex-web-plan-confirmation-context"
_CONTEXT_VERSION = 1


def _plan_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_plan(item: dict[str, Any]) -> bool:
    return (item.get("kind") == "plan" and item.get("native_item_id") != "turn-plan"
            and item.get("is_current_source") is not False)


def _ordinary_user(command: dict[str, Any]) -> bool:
    # Old rows decode a missing/invalid origin as user.  Be equally cautious
    # with snapshots constructed by callers and tests.
    return command.get("kind") in {"start", "steer"} and command.get("origin", "user") == "user"


def _ordinary_input(command: dict[str, Any]) -> bool:
    return command.get("kind") in {"start", "steer"}


def _automatic_goal_turn(commands: list[dict[str, Any]]) -> bool:
    return bool(commands) and all(
        command.get("kind") == "goal_continue" and command.get("origin") == "system"
        for command in commands
    )


def _effective_commands(turn: dict[str, Any], exclude_command_id: str | None) -> list[dict[str, Any]]:
    # A definite rejection did not deliver input or perform the operation.
    # Pending/unknown receipts remain relevant and must still block approval.
    return [command for command in turn.get("commands", []) if isinstance(command, dict)
            and command.get("id") != exclude_command_id and command.get("state") != "rejected"]


def _stable_attachments(command: dict[str, Any]) -> list[dict[str, Any]]:
    """Bind immutable attachment references; only their hash goes public."""
    values = command.get("payload", {}).get("attachments") or []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if not isinstance(value, dict):
            # It will be rejected as an unsafe/unknown ordinary input rather
            # than allowing a token that cannot describe its context.
            result.append({"invalid": True})
            continue
        result.append({key: value.get(key) for key in (
            "id", "name", "size", "mime_type", "preview_kind", "created_at", "sha256", "path", "identity",
        )})
    return result


def _context_digest(snapshot: dict[str, Any], binding: dict[str, str], supplements: list[dict[str, Any]]) -> str:
    conversation = snapshot.get("conversation", {})
    payload = {
        "domain": _CONTEXT_DOMAIN,
        "version": _CONTEXT_VERSION,
        "algorithm": _DIGEST,
        "conversation_id": conversation.get("id"),
        "thread_id": conversation.get("thread_id"),
        "plan": binding,
        "supplements": [{
            "id": item["id"], "turn_id": item["turn_id"], "text": item.get("text") or "",
            "attachments": _stable_attachments(item),
        } for item in supplements],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _blocked(plan: dict[str, Any] | None, binding: dict[str, str] | None, reason: str,
             supplements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"state": "blocked", "plan": plan, "binding": binding,
            "supplements": supplements or [], "reason": reason}


def plan_confirmation_candidate(snapshot: dict[str, Any], *, exclude_command_id: str | None = None) -> dict[str, Any] | None:
    """Return the only current Plan that may be confirmed, or its blocker.

    A completed ordinary user turn after the Plan becomes an explicit review
    context.  Anything that cannot be proven to be a completed user input is
    a blocker: it must not be silently discarded by implementing an older Plan.
    """
    turns = sorted((turn for turn in snapshot.get("turns", []) if isinstance(turn, dict)),
                   key=lambda turn: (turn.get("ordinal", -1), str(turn.get("id", ""))))
    latest: tuple[dict[str, Any], dict[str, Any], int] | None = None
    for index, turn in enumerate(turns):
        for item in turn.get("items", []):
            if isinstance(item, dict) and _is_plan(item):
                latest = (turn, item, index)
    if latest is None:
        return None
    bound, plan, bound_index = latest
    plan_text = plan.get("text") if isinstance(plan.get("text"), str) else ""
    if (bound.get("status") != "completed" or plan.get("status") != "completed" or not plan_text):
        return _blocked(plan, None, "latest_plan_not_completed")
    binding = {"turn_id": bound.get("id"), "item_id": plan.get("native_item_id"),
               "plan_digest": _plan_digest(plan_text)}
    if any(not isinstance(value, str) or not value for value in binding.values()):
        return _blocked(plan, None, "latest_plan_invalid")
    for command in snapshot.get("commands", []):
        if not isinstance(command, dict):
            continue
        prior = command.get("payload", {}).get("plan_confirmation")
        if (command.get("id") != exclude_command_id and isinstance(prior, dict)
                and command.get("state") in {"submitted", "queued", "accepted", "completed", "delivery_unknown"}
                and all(prior.get(key) == binding[key] for key in binding)):
            return _blocked(plan, binding, "plan_already_confirmed")

    # A message may remain unbound while native delivery is unresolved or
    # queued.  It is not enough to inspect snapshot.queue because submitted
    # and unknown receipts do not necessarily appear there.
    pending_input = any(
        isinstance(command, dict) and command.get("id") != exclude_command_id
        and _ordinary_input(command) and command.get("state") in {"submitted", "queued", "delivery_unknown"}
        for command in snapshot.get("commands", [])
    )

    supplements: list[dict[str, Any]] = []
    # A steer may predate the marker of the automatic turn that consumed it.
    # Creation order does not establish which command actually started a turn.
    # If there is an automatic marker, every user input is a supplement. With
    # several ordinary inputs, conservatively review ALL of them: the existing
    # journal does not preserve enough start-vs-steer provenance to omit one.
    bound_commands = _effective_commands(bound, exclude_command_id)
    has_automatic_source = any(_automatic_goal_turn([command]) for command in bound_commands)
    ordinary_bound = [command for command in bound_commands if not _automatic_goal_turn([command])]
    extra = ordinary_bound if has_automatic_source or len(ordinary_bound) > 1 else []
    if extra:
        if not all(_ordinary_user(command) for command in extra):
            return _blocked(plan, binding, "subsequent_input_not_user_message")
        if any(command.get("state") not in {"accepted", "completed"} for command in extra):
            return _blocked(plan, binding, "ordinary_input_pending")
        supplements.extend({**command, "turn_id": bound.get("id"), "scope": "plan_turn"} for command in extra)
    for turn in turns[bound_index + 1:]:
        commands = _effective_commands(turn, exclude_command_id)
        # A newly observed native turn without an owned command could be an
        # external user input.  Do not infer either its source or its intent.
        if not commands:
            return _blocked(plan, binding, "new_native_turn_unattributed", supplements)
        if _automatic_goal_turn(commands):
            if turn.get("status") not in {"inProgress", "completed"}:
                return _blocked(plan, binding, "automatic_turn_needs_reconcile", supplements)
            continue
        commands = [command for command in commands if not _automatic_goal_turn([command])]
        if not all(_ordinary_user(command) for command in commands):
            return _blocked(plan, binding, "subsequent_input_not_user_message", supplements)
        if any(command.get("state") not in {"accepted", "completed"} for command in commands):
            return _blocked(plan, binding, "ordinary_input_pending", supplements)
        if turn.get("status") != "completed":
            return _blocked(plan, binding, "ordinary_input_not_completed", supplements)
        supplements.extend({**command, "turn_id": turn.get("id"), "scope": "after_plan"} for command in commands)

    if pending_input:
        return _blocked(plan, binding, "ordinary_input_pending", supplements)
    if not supplements:
        return {"state": "direct", "plan": plan, "binding": binding, "supplements": [], "reason": None}
    # Preserve turn ordinal then the durable command sequence returned by the
    # store.  A command ID is intentionally not sorted: creation order is the
    # user's order within a turn.
    digest = _context_digest(snapshot, binding, supplements)
    return {"state": "review", "plan": plan, "binding": binding,
            "context_digest": digest, "supplements": supplements, "reason": None}


def matching_plan(snapshot: dict[str, Any], binding: dict[str, Any], *, exclude_command_id: str | None = None):
    """Strictly match either a legacy direct binding or a reviewed binding."""
    if not isinstance(binding, dict):
        return None
    candidate = plan_confirmation_candidate(snapshot, exclude_command_id=exclude_command_id)
    if candidate is None or candidate["state"] == "blocked":
        return None
    expected = candidate.get("binding")
    if any(binding.get(key) != expected.get(key) for key in ("turn_id", "item_id", "plan_digest")):
        return None
    context_digest = binding.get("context_digest", "")
    if context_digest:
        if candidate["state"] != "review" or context_digest != candidate.get("context_digest"):
            return None
    elif candidate["state"] != "direct":
        return None
    return candidate["plan"]


def approved_context(plan: dict[str, Any], binding: dict[str, Any], supplements: list[dict[str, Any]] | None = None) -> str:
    """Build native-only leading context from server-owned durable input."""
    direct = ("用户已明确确认下述固定版本的计划，请按此版本实施，不用其他计划替换：\n"
              + "计划版本：" + binding["plan_digest"] + "\n" + plan["text"])
    if not binding.get("context_digest"):
        return direct
    parts = [
        "用户已明确确认以此版本计划为基础并结合下述后续补充实施。后续用户补充要求仍有效，不被旧计划覆盖；冲突或未明确处先澄清，不自行实施相冲突部分。",
        "计划版本：" + binding["plan_digest"] + "\n" + plan["text"],
    ]
    for command in supplements or []:
        parts.append("后续用户补充（" + str(command.get("id") or "") + "）：\n" + (command.get("text") or ""))
        # Validate and present the exact frozen attachment version, never a
        # mutable upload-directory lookup.  Imported lazily to avoid a module
        # cycle during basic Plan-only tests.
        from shared.message_attachments import attachment_context
        context = attachment_context(command)
        if context:
            parts.append(context)
    return "\n\n".join(parts)
