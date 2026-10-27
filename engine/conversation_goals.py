"""Native Goal control and a rollback-compatible, additive SQLite projection.

Native Codex owns continuation. This module never starts a continuation turn or
replays an uncertain goal mutation. Only the exact owned Thread is projected.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import math
from typing import Any
from pathlib import Path

from engine.codex_app_server import AppServerError, AppServerProtocolError, AppServerRPCError
from engine.conversation_store import utc_now

STATUSES = frozenset({'active', 'paused', 'blocked', 'usageLimited', 'budgetLimited', 'complete'})


def validate_goal(goal: Any, thread_id: str) -> dict[str, Any] | None:
    if goal is None:
        return None
    if (not isinstance(goal, dict) or goal.get('threadId') != thread_id
            or not isinstance(goal.get('objective'), str) or not goal['objective'].strip()
            or goal.get('status') not in STATUSES):
        raise AppServerProtocolError('native goal response is invalid')
    for key in ('createdAt', 'updatedAt', 'tokensUsed', 'timeUsedSeconds'):
        value = goal.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise AppServerProtocolError('native goal usage is invalid')
    budget = goal.get('tokenBudget')
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0):
        raise AppServerProtocolError('native goal budget is invalid')
    return {key: goal.get(key) for key in ('threadId', 'objective', 'status', 'createdAt',
                                          'updatedAt', 'tokensUsed', 'timeUsedSeconds', 'tokenBudget')}


def validate_intent(objective: str, status: str) -> None:
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise AppServerProtocolError('目标须为 1–4000 个字符。')
    if status not in {'active', 'paused', 'complete'}:
        raise AppServerProtocolError('目标状态无效。')


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _control(goal: Any) -> Any:
    return None if goal is None else {key: goal.get(key) for key in ('objective', 'status', 'tokenBudget', 'createdAt')}


class GoalStore:
    def __init__(self, store):
        self.store = store
        with store._lock:
            store._db.execute('''CREATE TABLE IF NOT EXISTS native_goal_projection(
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                goal_json TEXT, draft_json TEXT, sync_state TEXT NOT NULL DEFAULT 'unknown',
                ever_native INTEGER NOT NULL DEFAULT 0, continuing INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
                settlement_pending INTEGER NOT NULL DEFAULT 0
            )''')
            store._db.execute('''CREATE TABLE IF NOT EXISTS native_goal_queued_input(
                command_id TEXT PRIMARY KEY REFERENCES commands(id) ON DELETE CASCADE,
                after_turn_id TEXT NOT NULL
            )''')
            columns = {r[1] for r in store._db.execute('PRAGMA table_info(native_goal_projection)')}
            if 'settlement_pending' not in columns:
                store._db.execute('ALTER TABLE native_goal_projection ADD COLUMN settlement_pending INTEGER NOT NULL DEFAULT 0')
            if 'notification_floor' not in columns:
                store._db.execute('ALTER TABLE native_goal_projection ADD COLUMN notification_floor INTEGER')
            # Goal RPC receipts are not user-turn delivery receipts. Keeping
            # them additive lets an older runtime reconcile without mistaking
            # an uncertain Goal RPC for an uncertain user-message delivery.
            store._db.execute('''CREATE TABLE IF NOT EXISTS native_goal_commands(
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, origin TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )''')
            store._db.execute("UPDATE native_goal_commands SET state='delivery_unknown',reason='restart_during_goal_delivery',updated_at=? WHERE state='submitted'", (utc_now(),))

    def begin_command(self, cid: str, command_id: str, *, payload: dict, origin='user', admitted_parent=None) -> tuple[dict, bool]:
        self.store._validate_id(command_id, 'command')
        if origin not in {'user', 'system'}:
            raise AppServerProtocolError('goal command origin is invalid')
        with self.store._lock, self.store.maintenance_admission.admission() as admission:
            self.store.get_conversation(cid)
            if self.store._db.execute('SELECT 1 FROM commands WHERE id=?', (command_id,)).fetchone():
                raise AppServerProtocolError('command ID was reused with new input')
            row = self.store._db.execute('SELECT * FROM native_goal_commands WHERE id=?', (command_id,)).fetchone()
            if row:
                if row['conversation_id'] != cid or row['payload_json'] != _json(payload) or row['origin'] != origin:
                    raise AppServerProtocolError('command ID was reused with new input')
                return self.command(command_id), False
            conversation = self.store.get_conversation(cid)
            activating = payload.get('action') == 'resume' or (payload.get('action') == 'set' and (payload.get('status') or 'active') == 'active')
            if activating and conversation.get('project_id'):
                if conversation['project_id'] in self.store._project_deletes or not Path(conversation['cwd']).is_dir():
                    raise AppServerProtocolError('项目正在删除或目录已不存在，目标未激活。')
            parent_admitted = False
            if admitted_parent is not None and origin == 'system':
                from shared.maintenance_admission import STEER_ONLY
                parent = self.store.command(admitted_parent)
                parent_admitted = (parent['conversation_id'] == cid
                    and parent['state'] in {'submitted','queued','accepted','completed'}
                    and not parent.get('payload', {}).get(STEER_ONLY)
                    and (parent['kind'] == 'start' or
                         (parent['kind'] == 'steer' and parent.get('payload', {}).get('goal_seed')
                          and parent.get('payload', {}).get('initial_goal'))))
            rejected = activating and admission['mode'] != 'open' and not parent_admitted
            now = utc_now()
            self.store._db.execute('INSERT INTO native_goal_commands VALUES(?,?,?,?,?,?,?,?)',
                                   (command_id, cid, _json(payload), 'rejected' if rejected else 'submitted', 'maintenance_quiescing' if rejected else None, origin, now, now))
            return self.command(command_id), not rejected

    def command(self, command_id: str) -> dict:
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM native_goal_commands WHERE id=?', (command_id,)).fetchone()
            if row is None:
                raise AppServerProtocolError('goal command does not exist')
            result = dict(row)
            result['payload'] = json.loads(result.pop('payload_json'))
            return {**result, 'kind': 'goal', 'text': None, 'target_turn_id': None, 'result_turn_id': None}

    def update_command(self, command_id: str, state: str, *, reason=None) -> dict:
        if state not in {'submitted', 'accepted', 'rejected', 'delivery_unknown'}:
            raise AppServerProtocolError('goal command state is invalid')
        with self.store._lock:
            self.command(command_id)
            self.store._db.execute('UPDATE native_goal_commands SET state=?,reason=?,updated_at=? WHERE id=?',
                                   (state, reason, utc_now(), command_id))
            return self.command(command_id)

    def entry(self, cid: str) -> dict[str, Any]:
        self.store.get_conversation(cid)
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM native_goal_projection WHERE conversation_id=?', (cid,)).fetchone()
            if row is None:
                return {'goal': None, 'draft': None, 'sync_state': 'unknown', 'ever_native': False,
                        'continuing': False, 'revision': 0, 'version': 0, 'settlement_pending': 0, 'notification_floor': None, 'exists': False}
            return {'goal': json.loads(row['goal_json']) if row['goal_json'] else None,
                    'draft': json.loads(row['draft_json']) if row['draft_json'] else None,
                    **{key: row[key] for key in ('sync_state', 'ever_native', 'continuing', 'revision', 'version', 'settlement_pending', 'notification_floor')}, 'exists': True}

    def put(self, cid: str, *, goal=..., draft=..., sync_state=None, ever_native=None, continuing=None,
            expected_revision=None, settlement_pending=None, notification_floor=...) -> dict[str, Any]:
        with self.store._lock:
            old = self.entry(cid)
            if expected_revision is not None and old['revision'] != expected_revision:
                return old
            new_goal = old['goal'] if goal is ... else goal
            new_draft = old['draft'] if draft is ... else draft
            state = sync_state or old['sync_state']
            ever = old['ever_native'] if ever_native is None else bool(ever_native)
            if continuing is None:
                run = new_goal and new_goal.get('status') == 'active'
                run = bool(run) if goal is not ... or draft is not ... else bool(old['continuing'])
            else:
                run = bool(continuing)
            pending = old['settlement_pending'] if settlement_pending is None else int(bool(settlement_pending))
            if goal is not ... and settlement_pending is None:
                # All native observations (RPC, read and notification) use the
                # same atomic transition. Response-before-notification cannot
                # lose terminal settlement, and terminal echoes cannot loop.
                pending = 0 if run else int(bool(pending or old['continuing'] or _control(new_goal) != _control(old['goal'])))
            floor = old['notification_floor'] if notification_floor is ... else notification_floor
            if run and not old['continuing']:
                conversation = self.store.get_conversation(cid)
                active = self.store._db.execute('SELECT ordinal FROM turns WHERE conversation_id=? AND id=?',
                                               (cid, conversation.get('active_turn_id'))).fetchone()
                last = self.store._db.execute('SELECT COALESCE(MAX(ordinal),0) FROM turns WHERE conversation_id=?', (cid,)).fetchone()[0]
                floor = active[0] if active else last + 1
            if settlement_pending is False:
                floor = None
            changed = (pending != old['settlement_pending'] or new_goal != old['goal'] or new_draft != old['draft'] or state != old['sync_state']
                       or floor != old['notification_floor'] or ever != bool(old['ever_native']) or run != bool(old['continuing']) or not old['exists'])
            if not changed:
                return old
            version_changed = _control(new_goal) != _control(old['goal']) or new_draft != old['draft']
            self.store._db.execute('''INSERT INTO native_goal_projection(conversation_id,goal_json,draft_json,sync_state,ever_native,continuing,revision,version,settlement_pending,notification_floor) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET goal_json=excluded.goal_json,
                draft_json=excluded.draft_json,sync_state=excluded.sync_state,
                ever_native=excluded.ever_native,continuing=excluded.continuing,
                revision=excluded.revision,version=excluded.version,settlement_pending=excluded.settlement_pending,notification_floor=excluded.notification_floor''',
                (cid, _json(new_goal) if new_goal else None, _json(new_draft) if new_draft else None,
                 state, int(ever), int(run), old['revision'] + 1, old['version'] + int(version_changed), pending, floor))
            self.store._db.execute('UPDATE conversations SET revision=revision+1,updated_at=? WHERE id=?', (utc_now(), cid))
            return self.entry(cid)

    def public(self, cid: str) -> dict[str, Any]:
        entry = self.entry(cid)
        goal = {**entry['goal'], 'version': str(entry['version'])} if entry['goal'] else None
        draft = {**{k: entry['draft'].get(k) for k in ('objective','status','tokenBudget')}, 'revision': str(entry['version'])} if entry['draft'] else None
        conversation = self.store.get_conversation(cid)
        return {'goal': goal, 'goal_draft': draft, 'goal_sync_state': (
            'draft' if draft and not goal and entry['sync_state'] in {'draft','synced'} else
            'synced' if not conversation.get('thread_id') else entry['sync_state'])}

    def recoverable_ids(self) -> list[str]:
        with self.store._lock:
            return [r[0] for r in self.store._db.execute('''SELECT g.conversation_id
                FROM native_goal_projection g JOIN conversations c ON c.id=g.conversation_id
                WHERE c.thread_id IS NOT NULL AND (g.continuing=1 OR g.sync_state='unknown' OR g.settlement_pending=1)''')]

    def suppress_notice(self, cid: str, tid: str) -> None:
        with self.store._lock:
            self.store._db.execute('''UPDATE turns SET notification_requested=0,notification_detail='goal_continuing'
                WHERE conversation_id=? AND id=? AND notification_state IS NULL AND notification_requested=1''', (cid, tid))

    def notice_suppressed(self, cid: str, tid: str) -> bool:
        with self.store._lock:
            row = self.store._db.execute('SELECT notification_detail FROM turns WHERE conversation_id=? AND id=?', (cid, tid)).fetchone()
            return bool(row and row[0] == 'goal_continuing')

    def restore_rejected_activation_notices(self, cid: str, floor: int) -> None:
        with self.store._lock:
            self.store._db.execute("""UPDATE turns SET notification_requested=(
                SELECT completion_notification FROM conversations WHERE id=?),notification_detail=NULL
                WHERE conversation_id=? AND ordinal>=? AND notification_state IS NULL
                AND notification_detail='goal_continuing'""", (cid, cid, floor))

    def restore_notice(self, cid: str, tid: str) -> None:
        with self.store._lock:
            floor = self.entry(cid)['notification_floor']
            self.store._db.execute('''UPDATE turns SET notification_requested=(SELECT completion_notification FROM conversations WHERE id=?),notification_detail=NULL
                WHERE conversation_id=? AND id=? AND notification_state IS NULL
                AND (notification_detail='goal_continuing' OR (? IS NOT NULL AND ordinal>=? AND notification_detail IS NULL
                    AND EXISTS(SELECT 1 FROM conversations WHERE id=? AND completion_notification=1)))''',
                (cid, cid, tid, floor, floor, cid))


class ConversationGoals:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = GoalStore(runtime.store)
        self._settling: dict[str, object] = {}

    def project_native(self, cid: str, **updates) -> dict:
        entry = self.store.put(cid, **updates)
        if entry['settlement_pending'] and not entry['continuing']:
            self.schedule_settlement(cid)
        return entry

    async def _result(self, cid: str, command_id: str) -> dict:
        await self.runtime._emit_status(cid)
        return {'command': self.store.command(command_id), 'snapshot': self.runtime.store.snapshot(cid)}

    def continuing(self, cid: str) -> bool:
        return bool(self.store.entry(cid)['continuing'])

    def suppressing_notices(self, cid: str) -> bool:
        entry = self.store.entry(cid)
        return bool(entry['continuing'] or (entry['draft'] and entry['draft']['status'] == 'active'))

    def disconnected(self) -> None:
        for cid in self.store.recoverable_ids():
            self.store.put(cid, sync_state='unknown')

    def schedule_settlement(self, cid: str) -> None:
        if cid in self._settling or getattr(self.runtime, '_closing', False):
            return
        token = object()
        self._settling[cid] = token
        version = self.store.entry(cid)['version']
        async def settle():
            try:
                await self._settle_goal(cid)
            finally:
                if self._settling.get(cid) is token:
                    self._settling.pop(cid, None)
                if self.runtime.store.conversation_exists(cid):
                    entry = self.store.entry(cid)
                    if not getattr(self.runtime, '_closing', False) and entry['settlement_pending'] and entry['version'] != version and not entry['continuing']:
                        self.schedule_settlement(cid)
        task = self.runtime._spawn(settle(), 'goal-terminal-reconcile')
        # Cancellation before the coroutine first runs must release admission.
        def released(_):
            if self._settling.get(cid) is token:
                self._settling.pop(cid, None)
        task.add_done_callback(released)

    def recover_settlements(self) -> None:
        for cid in self.store.recoverable_ids():
            entry = self.store.entry(cid)
            if entry['settlement_pending'] and not entry['continuing']:
                self.schedule_settlement(cid)

    def first_delivery_uncertain(self, cid: str) -> None:
        entry = self.store.entry(cid)
        if entry['draft']:
            self.store.put(cid, draft={**entry['draft'], 'status': 'paused', 'activation_unconfirmed': True},
                           sync_state='unknown', continuing=bool(entry['continuing'] or (entry['goal'] and entry['goal']['status'] == 'active')))

    def save_draft(self, cid: str, objective: str, status: str = 'active') -> None:
        validate_intent(objective, status)
        if status == 'complete':
            raise AppServerProtocolError('新目标只能开始或暂停。')
        self.store.put(cid, draft={'objective': objective.strip(), 'status': status, 'tokenBudget': None}, sync_state='draft')

    async def refresh_locked(self, cid: str, client=None, *, resume=True) -> dict[str, Any]:
        conversation = self.runtime.store.get_conversation(cid)
        if not conversation.get('thread_id'):
            return self.store.public(cid)
        client = client or await self.runtime._ensure_client()
        if resume and not self.runtime.store.pending_tree_stop(cid):
            await self.runtime._resume_owned_thread(client, conversation['thread_id'], reuse_loaded=True)
        checkpoint = self.store.entry(cid)['revision']
        try:
            goal = await client.get_thread_goal(conversation['thread_id'])
            await client.flush_notifications()
        except AppServerRPCError as error:
            self.store.put(cid, sync_state='unavailable' if error.code == -32601 else 'unknown')
            raise
        except AppServerError:
            self.store.put(cid, sync_state='unknown')
            raise
        entry = self.store.entry(cid)
        draft = entry['draft']
        if goal is None and draft and entry['ever_native'] and entry['sync_state'] == 'unknown':
            draft = {**draft, 'status': 'paused', 'activation_unconfirmed': True}
        clear_draft = bool(goal and draft and draft.get('activation_unconfirmed')
                           and draft['objective'] == goal['objective'])
        self.project_native(cid, goal=goal, sync_state='synced', draft=None if clear_draft else draft,
                       ever_native=bool(goal) or entry['ever_native'], expected_revision=checkpoint)
        return self.store.public(cid)

    async def refresh_for_reconcile(self, cid: str, client) -> None:
        entry = self.store.entry(cid)
        if not entry['exists']:
            return
        if not entry['ever_native'] and entry['sync_state'] == 'unavailable':
            return
        try:
            await self.refresh_locked(cid, client, resume=False)
        except AppServerError:
            # An unestablished optional capability is not Thread availability.
            # Still read the real native turns; never infer an existing goal away.
            if entry['ever_native'] or entry['continuing']:
                raise

    def defer_input(self, cid: str, command_id: str, turn_id: str) -> None:
        if self.store.entry(cid)['ever_native']:
            with self.runtime.store._lock:
                self.runtime.store._db.execute('INSERT OR REPLACE INTO native_goal_queued_input(command_id,after_turn_id) VALUES(?,?)', (command_id, turn_id))

    def input_waits_for_next_turn(self, command_id: str, turn_id: str) -> bool:
        with self.runtime.store._lock:
            row = self.runtime.store._db.execute('SELECT after_turn_id FROM native_goal_queued_input WHERE command_id=?', (command_id,)).fetchone()
            return bool(row and row[0] == turn_id)

    def quarantine_drafts(self) -> None:
        # A process death between first-turn delivery and activation is not
        # permission to replay either RPC. Preserve the objective for the user.
        with self.runtime.store._lock:
            ids = [r[0] for r in self.runtime.store._db.execute("SELECT g.conversation_id FROM native_goal_projection g JOIN conversations c ON c.id=g.conversation_id WHERE g.draft_json IS NOT NULL AND c.thread_id IS NOT NULL")]
        for cid in ids:
            entry = self.store.entry(cid)
            self.store.put(cid, draft={**entry['draft'], 'status': 'paused', 'activation_unconfirmed': True},
                           sync_state='unknown', continuing=bool(entry['continuing'] or (entry['goal'] and entry['goal']['status'] == 'active')))

    async def refresh(self, cid: str) -> dict[str, Any]:
        async with self.runtime._conversation_locks[cid]:
            return await self.refresh_locked(cid)

    async def sync_settings(self, cid: str, client, *, selection=None, settings=None) -> None:
        if self.runtime.store.pending_tree_stop(cid):
            raise AppServerProtocolError('停止尚未核验完成，不能修改原生设置。')
        conversation = self.runtime.store.get_conversation(cid)
        settings = settings or conversation['settings']
        selection = selection or await client.resolve_model_selection(settings.get('model'), settings.get('effort'), force_refresh=True)
        if self.runtime.store.pending_tree_stop(cid):
            raise AppServerProtocolError('停止尚未核验完成，不能修改原生设置。')
        await client.update_goal_thread_settings(conversation['thread_id'], cwd=conversation['cwd'],
            model=selection['model'], effort=selection['reasoning_effort'], permission=settings['permission'],
            collaboration_mode=settings.get('collaboration_mode', 'default'), service_tier=settings.get('service_tier'))

    async def change(self, cid: str, *, command_id: str, action: str, objective=None, status=None,
                     token_budget=None, goal_version: str = '') -> dict[str, Any]:
        if action not in {'set', 'pause', 'resume', 'complete', 'clear'}:
            raise AppServerProtocolError('目标操作无效。')
        if action == 'set':
            validate_intent(objective, status or 'active')
        if token_budget is not None and (isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0):
            raise AppServerProtocolError('目标预算须为正整数。')
        payload = {'action': action, 'objective': objective, 'status': status, 'token_budget': token_budget, 'goal_version': goal_version}
        command, created = self.store.begin_command(cid, command_id, payload=payload)
        if not created:
            return await self._result(cid, command_id)
        try:
            async with self.runtime._conversation_locks[cid]:
                return await self._change_locked(cid, command_id, payload)
        except asyncio.CancelledError:
            self._cancel_command(cid, command_id)
            raise

    def _cancel_command(self, cid: str, command_id: str) -> None:
        receipt = self.store.command(command_id)
        if receipt['state'] != 'submitted':
            return
        sent = receipt['reason'] == 'goal_rpc_sending'
        self.store.update_command(command_id, 'delivery_unknown' if sent else 'rejected',
                                  reason='goal_delivery_unknown' if sent else 'goal_not_sent')
        if sent:
            self.store.put(cid, sync_state='unknown')
        elif receipt['origin'] == 'system' and self.store.entry(cid)['draft']:
            # Only the owning first-turn activation can quarantine its draft.
            # An unrelated UI mutation cancelled while waiting must not alter it.
            self.first_delivery_uncertain(cid)
            self._release_unconfirmed_notice(cid)

    def _release_unconfirmed_notice(self, cid: str) -> None:
        entry = self.store.entry(cid)
        tid = self.runtime.store.get_conversation(cid).get('last_terminal_turn_id')
        if entry['draft'] and not entry['goal'] and not entry['continuing'] and tid and self.store.notice_suppressed(cid, tid):
            self.store.restore_notice(cid, tid)
            self.store.put(cid, settlement_pending=True)
            self.schedule_settlement(cid)

    async def _change_locked(self, cid: str, command_id: str, payload: dict) -> dict:
        # Both explicit changes and first-turn activation use this boundary.
        try:
            return await self._apply_change_locked(cid, command_id, payload)
        except asyncio.CancelledError:
            self._cancel_command(cid, command_id)
            raise

    async def _apply_change_locked(self, cid: str, command_id: str, payload: dict) -> dict:
        runtime = self.runtime
        conversation = runtime.store.get_conversation(cid)
        if conversation.get('native_delete_pending'):
            self.store.update_command(command_id, 'rejected', reason='native_delete_pending')
            return await self._result(cid, command_id)
        if runtime.store.pending_tree_stop(cid) and (payload['action'] == 'resume' or
                (payload['action'] == 'set' and (payload.get('status') or 'active') == 'active')):
            self.store.update_command(command_id, 'rejected', reason='tree_stop_pending')
            return await self._result(cid, command_id)
        client = None
        sent = False
        entry = self.store.entry(cid)
        try:
            if conversation.get('thread_id'):
                client = await runtime._ensure_client()
                await self.refresh_locked(cid, client, resume=not runtime.store.pending_tree_stop(cid))
            entry = self.store.entry(cid)
            current = entry['goal'] or entry['draft']
            version = str(entry['version']) if current else ''
            if str(payload.get('goal_version') or '') != version:
                raise AppServerProtocolError('目标已变化，请重新打开目标面板核对；编辑内容仍保留。')
            action = payload['action']
            objective = payload.get('objective')
            status = payload.get('status') or 'active'
            if action != 'set' and action != 'clear' and current is None:
                raise AppServerProtocolError('当前没有目标。')
            statuses = {'pause': 'paused', 'resume': 'active', 'complete': 'complete'}
            status = statuses.get(action, status)
            if client is None:
                if status == 'complete' and action != 'clear':
                    raise AppServerProtocolError('尚未开始的目标不能标记完成。')
                draft = None if action == 'clear' else {
                    'objective': objective.strip() if action == 'set' else current['objective'],
                    'status': status, 'tokenBudget': payload.get('token_budget') if payload.get('token_budget') is not None else (current or {}).get('tokenBudget')}
                self.store.put(cid, draft=draft, sync_state='draft' if draft else 'synced')
            else:
                if status == 'active' and action != 'clear':
                    await self.sync_settings(cid, client)
                await client.flush_notifications()
                # Config RPCs yield: native/model tools may complete, pause or
                # clear the goal meanwhile. Revalidate before any mutation.
                if self.store.entry(cid)['version'] != entry['version']:
                    raise AppServerProtocolError('目标已变化，请重新核对。')
                if runtime.store.pending_tree_stop(cid) and status == 'active' and action != 'clear':
                    self.store.update_command(command_id, 'rejected', reason='tree_stop_pending')
                    return await self._result(cid, command_id)
                self.store.put(cid, sync_state='unknown', ever_native=True,
                               continuing=True if status == 'active' and action != 'clear' else entry['continuing'])
                attempt = self.store.entry(cid)
                checkpoint = attempt['revision']
                self.store.update_command(command_id, 'submitted', reason='goal_rpc_sending')
                sent = True
                if action == 'clear':
                    await client.clear_thread_goal(conversation['thread_id'])
                    goal = None
                else:
                    params = {'status': status}
                    if action == 'set':
                        params['objective'] = objective.strip()
                    if payload.get('token_budget') is not None:
                        params['tokenBudget'] = payload['token_budget']
                    goal = await client.set_thread_goal(conversation['thread_id'], **params)
                await client.flush_notifications()
                self.project_native(cid, goal=goal, sync_state='synced', expected_revision=checkpoint)
                # Clear the command's old draft even when a newer native goal
                # notification won the response CAS. Do not replace that goal.
                if entry['draft']:
                    self.store.put(cid, draft=None)
        except AppServerRPCError as error:
            self.store.update_command(command_id, 'rejected', reason='goal_rejected')
            unsupported = error.code == -32601 and (sent or self.store.entry(cid)['sync_state'] == 'unavailable')
            rollback_applied = self.store.entry(cid)['revision'] == (checkpoint if sent else entry['revision'])
            self.store.put(cid, sync_state='unavailable' if unsupported else 'unknown', continuing=entry['continuing'],
                           notification_floor=entry['notification_floor'],
                           expected_revision=checkpoint if sent else entry['revision'])
            if entry['draft']:
                self.first_delivery_uncertain(cid)
                if unsupported:
                    self.store.put(cid, sync_state='unavailable')
                if rollback_applied:
                    self._release_unconfirmed_notice(cid)
            if sent and rollback_applied and attempt['continuing'] and not entry['continuing'] and attempt['notification_floor'] is not None:
                # Definite rejection is different from unknown delivery. Undo
                # only this tentative cycle, never a newer native observation.
                self.store.restore_rejected_activation_notices(cid, attempt['notification_floor'])
                self.store.put(cid, settlement_pending=True)
                self.schedule_settlement(cid)
            return await self._result(cid, command_id)
        except AppServerProtocolError:
            self.store.update_command(command_id, 'delivery_unknown' if sent else 'rejected',
                                         reason='goal_delivery_unknown' if sent else 'goal_state_changed_or_invalid')
            if sent:
                self.store.put(cid, sync_state='unknown')
                if entry['draft']:
                    self.first_delivery_uncertain(cid)
            return await self._result(cid, command_id)
        except AppServerError:
            self.store.update_command(command_id, 'delivery_unknown' if sent else 'rejected',
                                         reason='goal_delivery_unknown' if sent else 'goal_not_sent')
            self.store.put(cid, sync_state='unknown')
            if entry['draft']:
                self.first_delivery_uncertain(cid)
            return await self._result(cid, command_id)
        self.store.update_command(command_id, 'accepted')
        if conversation.get('thread_id'):
            runtime._spawn(runtime.reconcile(cid, origin='goal_changed'), 'goal-reconcile')
        return await self._result(cid, command_id)

    async def prepare_first(self, cid: str, parent_id: str, client, selection: dict) -> str | None:
        draft = self.store.entry(cid)['draft']
        if not draft:
            return None
        if not draft.get('activation_unconfirmed'):
            await self.sync_settings(cid, client, selection=selection)
        # Do not precreate an active or artificially paused native goal. The
        # exact user turn must bind first; native set(active) on an in-progress
        # turn continues only after it ends (verified on 0.159.2).
        return ('用户为此对话保存的目标（独立于执行模式）：\n' + draft['objective'] + '\n目标后续状态：' +
                ('暂停，不自行恢复；本轮仅执行用户消息。' if draft['status'] == 'paused' else '首轮绑定后由工作台启用原生后续推进；按真实结果更新目标状态。'))

    async def finish_first(self, cid: str, parent_id: str) -> None:
        if self.runtime.store.pending_tree_stop(cid):
            return
        entry = self.store.entry(cid)
        draft = entry['draft']
        if not draft or draft.get('activation_unconfirmed'):
            return
        client = await self.runtime._ensure_client()
        try:
            await self.refresh_locked(cid, client, resume=False)
            entry = self.store.entry(cid)
            # Native model tools may have already created, paused, completed,
            # or cleared the goal. Their state wins; never re-activate it.
            if entry['goal']:
                self.store.put(cid, draft=None)
                if entry['settlement_pending']:
                    self.schedule_settlement(cid)
                return
            if entry['ever_native']:
                # A prior activation may have been rejected or its response
                # lost. Keep the quarantined objective for explicit UI retry.
                return
            command_id = 'ga' + hashlib.sha256(parent_id.encode()).hexdigest()
            payload = {'action': 'set', 'objective': draft['objective'], 'status': draft['status'],
                       'token_budget': draft.get('tokenBudget'), 'goal_version': str(entry['version'])}
            _, created = self.store.begin_command(cid, command_id, payload=payload, origin='system', admitted_parent=parent_id)
            if created:
                await self._change_locked(cid, command_id, payload)
        except AppServerError:
            # The first prompt is already accepted. Expose uncertainty instead
            # of resubmitting it or automatically replaying an activation.
            self.first_delivery_uncertain(cid)

    async def notification(self, method: str, params: dict) -> bool:
        thread_id = params.get('threadId')
        conversation = self.runtime.store.conversation_for_thread(thread_id) if isinstance(thread_id, str) else None
        if not conversation:
            return True
        cid = conversation['id']
        if method == 'thread/goal/updated':
            goal = validate_goal(params.get('goal'), thread_id)
            if goal is None:
                raise AppServerProtocolError('native goal update cannot be null')
        else:
            goal = None
        before = self.store.entry(cid)
        # Native resume echoes cleared even for a thread that never had a
        # goal. That absence is not a model/user clear of a saved draft.
        # ever_native is a recovery requirement (attempted OR observed), not
        # evidence of creation. A resume absence echo must not erase an
        # activation_unconfirmed draft after rejected/lost first activation.
        confirmed = bool(before['goal'])
        draft = None if method == 'thread/goal/cleared' and confirmed else before['draft']
        self.project_native(cid, goal=goal, draft=draft, sync_state='synced',
                            ever_native=before['ever_native'] or bool(goal))
        await self.runtime._emit_status(cid)
        return True

    async def _settle_goal(self, cid: str) -> None:
        if not self.runtime.store.conversation_exists(cid):
            return
        await self.runtime.reconcile(cid, origin='goal_terminal')
        conversation = self.runtime.store.get_conversation(cid)
        if self.continuing(cid) or conversation['state'] != 'idle' or conversation.get('native_delete_pending'):
            return
        tid = conversation.get('last_terminal_turn_id')
        entry = self.store.entry(cid)
        if tid:
            self.store.restore_notice(cid, tid)
            await self.runtime._call_handler(self.runtime._terminal_handler, cid, tid,
                {**self.runtime.store.snapshot(cid), 'terminal_source': 'reconcile'})
        self.store.put(cid, settlement_pending=False, expected_revision=entry['revision'])

    def bind_automatic_turn(self, conversation: dict, turn: dict, *, dispatch_queue: bool = True) -> bool:
        cid = conversation['id']
        if not self.store.entry(cid)['ever_native'] or conversation.get('native_delete_pending'):
            return False
        known = self.runtime.store.conversation_for_turn(conversation['thread_id'], turn.get('id'))
        if known is not None:
            self.runtime.store.admit_live_completion_receipt(cid, turn['id'])
            if dispatch_queue and any(c['kind'] == 'goal_continue' and c.get('result_turn_id') == turn.get('id')
                   for c in self.runtime.store.snapshot(cid)['commands']):
                self.runtime._spawn(self.deliver_queue(cid, turn['id']), 'goal-known-turn-queued-input')
            return True
        command_id = 'gc' + hashlib.sha256((conversation['thread_id'] + ':' + str(turn.get('id'))).encode()).hexdigest()
        _, created = self.runtime.store.begin_command(cid, command_id, kind='goal_continue', origin='system',
                                                     payload={'native_turn_id': turn.get('id')})
        if not created:
            return True
        self.runtime.store.bind_turn(cid, turn, command_id=command_id)
        settings = self.runtime.store.get_conversation(cid)['settings']
        if settings.get('pending_for_next_turn') and settings.get('_goal_settings_after_turn') != turn['id']:
            self.runtime.store.update_settings(cid, {'pending_for_next_turn': False, '_pending_turn_settings_base': None})
        self.runtime._spawn(self.runtime.reconcile(cid, origin='goal_turn_started'), 'goal-turn-hydrate')
        if dispatch_queue:
            self.runtime._spawn(self.deliver_queue(cid, turn['id']), 'goal-turn-queued-input')
        return True

    def recovered_turn(self, cid: str, turn: dict) -> None:
        entry = self.store.entry(cid)
        if not entry['ever_native'] or turn.get('status') not in {'inProgress', 'active', 'queued', 'stopping'}:
            return
        commands = self.runtime.store.snapshot(cid)['commands']
        owned = [c for c in commands if c.get('result_turn_id') == turn['id']]
        if not owned:
            command_id = 'gc' + hashlib.sha256((self.runtime.store.get_conversation(cid)['thread_id'] + ':' + turn['id']).encode()).hexdigest()
            _, created = self.runtime.store.begin_command(cid, command_id, kind='goal_continue', origin='system', payload={'native_turn_id': turn['id']}, notification_admitted=False)
            if created:
                self.runtime.store.bind_turn(cid, turn, command_id=command_id)
            owned = [self.runtime.store.command(command_id)]
        if any(c['kind'] == 'goal_continue' for c in owned):
            settings = self.runtime.store.get_conversation(cid)['settings']
            if settings.get('pending_for_next_turn') and settings.get('_goal_settings_after_turn') != turn['id']:
                self.runtime.store.update_settings(cid, {'pending_for_next_turn': False, '_pending_turn_settings_base': None})
            self.runtime._spawn(self.deliver_queue(cid, turn['id']), 'goal-recovered-turn-queued-input')

    async def deliver_queue(self, cid: str, turn_id: str) -> None:
        async with self.runtime._conversation_locks[cid]:
            for command in self.runtime.store.queued_commands(cid):
                conversation = self.runtime.store.get_conversation(cid)
                if (conversation['state'] != 'active' or conversation.get('active_turn_id') != turn_id
                        or conversation.get('native_delete_pending') or self.runtime.store.pending_tree_stop(cid)
                        or self.runtime.store.commands_with_state(cid, 'delivery_unknown')):
                    return
                if self.input_waits_for_next_turn(command['id'], turn_id):
                    continue
                if command.get('origin') == 'scheduled':
                    continue
                settings = conversation['settings']
                if settings.get('pending_for_next_turn') and settings.get('_goal_settings_after_turn') == turn_id:
                    return
                result = await self.runtime._steer_locked(cid, command['id'], command.get('text') or '',
                                      skill_selectors=command.get('payload', {}).get('skills') or [])
                if result.get('command', {}).get('state') not in {'accepted', 'completed'}:
                    return
