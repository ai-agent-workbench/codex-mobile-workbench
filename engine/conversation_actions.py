"""Desktop-style Plan/Goal actions over the existing native command journal.

Codex owns execution and Goal continuation. A plan approval is one durable
user command, not a browser-side settings POST followed by a second POST.
"""
from __future__ import annotations

import hashlib
import re

from engine.codex_app_server import AppServerError, AppServerProtocolError, AppServerRPCError
from engine.conversation_goals import validate_intent
from engine.conversation_plan import matching_plan


class ConversationActions:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    async def start_goal(self, cid, *, command_id, objective, goal_version="", skills=None):
        validate_intent(objective, "active")
        objective = objective.strip()
        # A first-turn receipt remains authoritative after its Thread binds.
        # Retrying an unknown HTTP response must not become a new Goal RPC.
        try:
            previous = self.store.command(command_id)
        except AppServerProtocolError:
            previous = None
        initial = {"objective": objective, "goal_version": str(goal_version or "")}
        if previous is not None:
            if (previous["conversation_id"] != cid or previous["kind"] not in {"start", "steer"}
                    or previous.get("payload", {}).get("initial_goal") != initial
                    or previous.get("payload", {}).get("skills") != (skills or [])):
                raise AppServerProtocolError("command ID was reused with new input")
            return {"command": self.action_command(cid, previous), "snapshot": self.store.snapshot(cid)}
        conversation = self.store.get_conversation(cid)
        if not conversation.get("thread_id"):
            result = await self.runtime.start_conversation(cid, command_id=command_id,
                text=objective, skill_selectors=skills,
                initial_goal=initial)
            return {**result, 'command': self.action_command(cid, result['command'])}
        # Native set(active) persists the Goal, but 0.160 does not wake every
        # idle Thread. Seed this explicit user action once if still idle; if a
        # native automatic turn already exists, steer it instead. Continuation
        # remains entirely native-owned; recovery never repeats the seed.
        command, created = self.store.begin_command(cid, command_id, kind='steer', text=objective,
            payload={'skills': skills or [], 'initial_goal': initial, 'goal_seed': True,
                     'force_new_turn': False})
        if not created:
            return {'command': self.action_command(cid, command), 'snapshot': self.store.snapshot(cid)}
        async with self.runtime._command_lock(cid, command_id):
            conversation = self.store.get_conversation(cid)
            if (conversation['state'] == 'needs_user' or conversation.get('native_delete_pending')
                    or self.store.pending_tree_stop(cid)):
                self.store.update_command(command_id, 'rejected', reason='goal_start_stale')
                return await self.runtime._result(cid, command_id)
            activation_id = 'ga' + hashlib.sha256(command_id.encode()).hexdigest()
            payload = {'action': 'set', 'objective': objective, 'status': 'active',
                       'token_budget': None, 'goal_version': goal_version}
            _, activated = self.runtime.goals.store.begin_command(cid, activation_id, payload=payload, origin='system', admitted_parent=command_id)
            activation = (await self.runtime.goals._change_locked(cid, activation_id, payload))['command'] if activated else self.runtime.goals.store.command(activation_id)
            if activation['state'] != 'accepted':
                self.store.update_command(command_id, activation['state'], reason=activation.get('reason'))
                return await self.runtime._result(cid, command_id)
            result = await self.runtime._preflight_start_locked(cid, command_id, objective, skill_selectors=skills)
            return {**result, 'command': self.action_command(cid, result['command'])}

    _matching_plan = staticmethod(matching_plan)

    def action_command(self, cid, command):
        """Keep first-message success separate from native Goal activation."""
        initial = command.get('payload', {}).get('initial_goal')
        if not initial or command['state'] not in {'accepted', 'completed'}:
            return command
        result = {**command, 'kind': 'goal_start', 'message_state': command['state']}
        activation_id = 'ga' + hashlib.sha256(command['id'].encode()).hexdigest()
        result['activation_command_id'] = activation_id
        try:
            activation = self.runtime.goals.store.command(activation_id)
        except AppServerProtocolError:
            activation = None
        entry = self.runtime.goals.store.entry(cid)
        observed = (entry['sync_state'] == 'synced' and entry['goal']
                    and entry['goal']['objective'] == initial['objective'])
        if activation:
            result['state'] = activation['state']
            if activation['state'] == 'delivery_unknown' and observed:
                result['state'] = 'accepted'
            result['reason'] = ('goal_activation_rejected' if result['state'] == 'rejected'
                                else 'goal_activation_unknown' if result['state'] not in {'accepted', 'completed'}
                                else None)
        elif observed or (entry['ever_native'] and not entry['draft'] and entry['sync_state'] == 'synced'):
            # A model tool may create/complete/clear the goal during the first
            # prompt; finish_first deliberately does not reactivate it.
            result['state'], result['reason'] = 'accepted', None
        else:
            result['state'], result['reason'] = 'delivery_unknown', 'goal_activation_unknown'
        return result

    async def implement_plan(self, cid, *, command_id, turn_id, item_id, plan_digest, context_digest="", skills=None):
        from engine.conversation_runtime import has_pending_native_delete
        runtime = self.runtime
        binding = {"turn_id": turn_id, "item_id": item_id, "plan_digest": plan_digest}
        if context_digest:
            binding["context_digest"] = context_digest
        if (not isinstance(context_digest, str)
                or any(not isinstance(value, str) or not value for value in binding.values())
                or not re.fullmatch(r"[a-f0-9]{64}", plan_digest)
                or (context_digest and not re.fullmatch(r"[a-f0-9]{64}", context_digest))):
            raise AppServerProtocolError("计划确认信息无效。")
        text = "按刚刚确认的计划开始实施，并完成必要验证。"
        command, created = self.store.begin_command(cid, command_id, kind="steer", text=text,
            payload={"skills": skills or [], "force_new_turn": True, "plan_confirmation": binding})
        if not created:
            return {"command": command, "snapshot": self.store.snapshot(cid)}
        async with runtime._command_lock(cid, command_id):
            conversation = self.store.get_conversation(cid)
            if (not conversation.get("thread_id") or has_pending_native_delete(conversation)
                    or self.store.pending_tree_stop(cid) or conversation["state"] == "needs_user"):
                self.store.update_command(command_id, "rejected", reason="plan_reconcile_required")
                return await runtime._result(cid, command_id)
            try:
                client = await runtime._ensure_client()
                await runtime._resume_owned_thread(client, conversation["thread_id"],
                    reuse_loaded=runtime.goals.store.entry(cid)["exists"])
                if runtime.goals.store.entry(cid)["ever_native"]:
                    await runtime.goals.refresh_locked(cid, client, resume=False)
                checkpoint = self.store.capture_reconcile_checkpoint(cid)
                native = await client.read_thread(conversation["thread_id"], include_turns=True)
                runtime._resolve_snapshot_commands(cid, native)
                await runtime._repair_delta_baselines(cid, client, native, checkpoint)
                runtime._reconcile_native(cid, native, checkpoint=checkpoint)
                snapshot = self.store.snapshot(cid)
                duplicate = any(
                    value["id"] != command_id
                    and value["state"] in {"submitted", "queued", "accepted", "completed", "delivery_unknown"}
                    and isinstance(value.get("payload", {}).get("plan_confirmation"), dict)
                    and all(value["payload"]["plan_confirmation"].get(key) == binding[key]
                            for key in ("turn_id", "item_id", "plan_digest"))
                    for value in snapshot["commands"])
                if (self._matching_plan(snapshot, binding, exclude_command_id=command_id) is None or duplicate
                        or snapshot["conversation"]["state"] not in {"idle", "active"}
                        or any(value["id"] != command_id for value in snapshot.get("queue", []))
                        or has_pending_native_delete(self.store.get_conversation(cid))
                        or self.store.pending_tree_stop(cid)):
                    self.store.update_command(command_id, "rejected", reason="plan_confirmation_stale")
                    return await runtime._result(cid, command_id)
                settings = {**snapshot["conversation"]["settings"], "collaboration_mode": "default"}
                after_turn = None
                transition = {'command_id': command_id,
                              'previous_mode': snapshot['conversation']['settings'].get('collaboration_mode', 'default')}
                if runtime.goals.store.entry(cid)["ever_native"]:
                    # Automatic Goal turns read thread settings, not our next
                    # turn/start options. Synchronize their native boundary.
                    selection = await client.resolve_model_selection(settings.get('model'), settings.get('effort'), force_refresh=True)
                    if not await runtime._validate_plan_delivery(cid, command_id, client):
                        return await runtime._result(cid, command_id)
                    self.store.update_settings(cid, {'_plan_approval_transition': transition})
                    if not runtime._mark_command_sending(command_id):
                        return await runtime._result(cid, command_id)
                    await runtime.goals.sync_settings(cid, client, settings=settings, selection=selection)
                    # Settings ACK is not an implementation-input delivery.
                    # Persist the boundary before any subsequent read/flush.
                    self.store.update_command(command_id, "submitted", reason="plan_settings_synced")
                    await client.flush_notifications()
                    boundary = await client.read_thread(conversation["thread_id"], include_turns=True)
                    runtime._reconcile_native(cid, boundary)
                    if not await runtime._validate_plan_delivery(cid, command_id, client):
                        return await runtime._result(cid, command_id)
                    active = [turn["id"] for turn in boundary["thread"].get("turns", [])
                              if turn.get("status") in {"inProgress", "active", "queued", "stopping"}]
                    if len(active) > 1:
                        raise AppServerProtocolError("native settings boundary is ambiguous")
                    after_turn = active[0] if active else None
                # Persist the chosen mode with the durable approval. Existing
                # permissions/model/effort/budget and paused goals are untouched.
                self.store.update_settings(cid, {"collaboration_mode": "default",
                    "pending_for_next_turn": True, "_goal_settings_after_turn": after_turn,
                    '_plan_approval_transition': transition})
                return await runtime._preflight_start_locked(cid, command_id, text, skill_selectors=skills)
            except AppServerRPCError as error:
                self.store.update_command(command_id, "rejected", reason="plan_settings_rejected")
                self.store.set_conversation_state(cid, "needs_user", last_error=runtime._rpc_error(error))
            except AppServerError as error:
                sent = self.store.command(command_id).get("reason") == "native_rpc_sending"
                self.store.update_command(command_id, "delivery_unknown" if sent else "rejected",
                    reason="plan_settings_delivery_unknown" if sent else "plan_preflight_failed")
                self.store.set_conversation_state(cid, "needs_user", connection_state="lost",
                    recovery_state="plan_delivery_unknown" if sent else "plan_preflight_failed",
                    last_error={"type": type(error).__name__})
            return await runtime._result(cid, command_id)
