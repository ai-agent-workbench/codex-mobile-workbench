"""Durable, in-process conversation automations.

This intentionally shares ConversationStore's SQLite connection.  It is a
small journal for scheduled *messages*, not a second conversation runtime.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.conversation_store import ConversationStore, utc_now
from shared.diagnostics import emit

_MAX_TASKS = 100
_MAX_TITLE = 120
_MAX_PROMPT = 60_000
_PENDING = {"dispatching", "queued", "accepted", "delivery_unknown"}
_RUN_TERMINAL = {"completed", "failed", "interrupted", "skipped"}
_TASK_STATES = {"active", "paused", "deleted"}


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("now must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: str, label: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be RFC3339 with timezone")
    return parsed.astimezone(timezone.utc)


def _time(value: Any) -> tuple[int, int, str]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise ValueError("schedule time must be HH:MM")
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError as exc:
        raise ValueError("schedule time must be HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("schedule time must be HH:MM")
    return hour, minute, f"{hour:02d}:{minute:02d}"


def validate_schedule(schedule: Any) -> dict[str, Any]:
    """Validate and canonicalize a public schedule object."""
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    kind = schedule.get("kind", schedule.get("type"))
    keys = {"interval":{"interval_minutes"},"daily":{"time","timezone"},"weekly":{"time","timezone","weekdays"},"once":{"run_at"}}
    if not isinstance(kind,str) or kind not in keys or set(schedule) - ({"kind","type"} | keys[kind]):
        raise ValueError("schedule contains unsupported fields")
    if "kind" in schedule and "type" in schedule and schedule["kind"] != schedule["type"]:
        raise ValueError("schedule kind conflicts with type")
    if kind == "interval":
        minutes = schedule.get("interval_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 525_600:
            raise ValueError("interval_minutes must be an integer from 1 to 525600")
        return {"kind": "interval", "interval_minutes": minutes}
    if kind not in {"daily", "weekly", "once"}:
        raise ValueError("schedule kind must be interval, daily, weekly, or once")
    if kind == "once":
        return {"kind": "once", "run_at": _stamp(_parse_stamp(schedule.get("run_at"), "run_at"))}
    hour, minute, time_text = _time(schedule.get("time"))
    tz_name = schedule.get("timezone", "Asia/Hong_Kong")
    if not isinstance(tz_name, str) or not tz_name:
        raise ValueError("schedule timezone is invalid")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("schedule timezone is invalid") from exc
    result: dict[str, Any] = {"kind": kind, "time": time_text, "timezone": tz_name}
    if kind == "weekly":
        days = schedule.get("weekdays")
        if (not isinstance(days, list) or not days or len(days)>7 or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days)):
            raise ValueError("weekly weekdays must be a nonempty list of 0..6")
        result["weekdays"] = sorted(set(days))
    return result


def next_due(schedule: Any, after: datetime | None = None) -> datetime | None:
    """Return the first due instant strictly after ``after`` in UTC."""
    rule = validate_schedule(schedule)
    now = _utc(after)
    if rule["kind"] == "interval":
        return now + timedelta(minutes=rule["interval_minutes"])
    if rule["kind"] == "once":
        due = _parse_stamp(rule["run_at"], "run_at")
        return due if due > now else None
    hour, minute, _ = _time(rule["time"])
    zone = ZoneInfo(rule["timezone"])
    local_now = now.astimezone(zone)
    for offset in range(0, 8):
        date = local_now.date() + timedelta(days=offset)
        if rule["kind"] == "weekly" and date.weekday() not in rule["weekdays"]:
            continue
        candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
        # Skip nonexistent wall times on DST spring-forward days.
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
            continue
        if candidate.astimezone(timezone.utc) > now:
            return candidate.astimezone(timezone.utc)
    raise ValueError("could not calculate next due time")


class AutomationStore:
    """Task and run journal, atomically coupled to a ConversationStore."""

    def __init__(self, store: ConversationStore):
        self.store = store
        self._prepare()

    def _prepare(self) -> None:
        with self.store._lock:
            version = self.store._db.execute("SELECT value FROM schema_meta WHERE key='automation_schema_version'").fetchone()
            if version is not None and version["value"] != "1":
                raise ValueError("unsupported automation schema version")
            self.store._db.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_automation_tasks (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    owner_account_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    collaboration_mode TEXT NOT NULL DEFAULT 'default',
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS conversation_automation_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES conversation_automation_tasks(id) ON DELETE CASCADE,
                    command_id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    turn_id TEXT,
                    skipped_count INTEGER NOT NULL DEFAULT 1,
                    last_skipped_at TEXT,
                    UNIQUE(task_id, scheduled_at)
                );
                CREATE TABLE IF NOT EXISTS automation_mutations (
                    request_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    owner_account_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS automation_tasks_due_idx
                    ON conversation_automation_tasks(state, next_run_at);
                CREATE INDEX IF NOT EXISTS automation_runs_task_idx
                    ON conversation_automation_runs(task_id, started_at DESC);
                INSERT OR IGNORE INTO schema_meta(key,value) VALUES('automation_schema_version','1');
            """)
            columns = {row["name"] for row in self.store._db.execute(
                "PRAGMA table_info(conversation_automation_tasks)"
            )}
            if "collaboration_mode" not in columns:
                self.store._db.execute(
                    "ALTER TABLE conversation_automation_tasks "
                    "ADD COLUMN collaboration_mode TEXT NOT NULL DEFAULT 'default'"
                )

    @staticmethod
    def _owner(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("owner_account_id is required")
        return value.strip()

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{label} must be nonempty and at most {maximum} characters")
        return value

    @staticmethod
    def _fingerprint(action: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps({"action": action, **payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _mutation(self, request_key: Any, action: str, owner: str, fingerprint: str) -> dict[str, Any] | None:
        if request_key is None:
            return None
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise ValueError("request_key is invalid")
        row = self.store._db.execute("SELECT * FROM automation_mutations WHERE request_key = ?", (request_key,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["owner_account_id"] != owner or row["fingerprint"] != fingerprint:
            raise ValueError("request_key was reused with different input")
        identity = json.loads(row["response_json"])
        # Replay the identity, not a stale active configuration after edits/delete.
        return self._decode_task(self._task(identity["id"], owner))

    def _save_mutation(self, request_key: Any, action: str, owner: str, fingerprint: str, response: dict[str, Any]) -> None:
        if request_key is not None:
            self.store._db.execute("INSERT INTO automation_mutations(request_key,conversation_id,action,owner_account_id,fingerprint,response_json,created_at) VALUES(?,?,?,?,?,?,?)", (request_key, response["conversation_id"], action, owner, fingerprint, json.dumps({"id":response["id"]}, separators=(",", ":")), utc_now()))

    def _source(self, conversation_id: str, owner: str) -> tuple[str, str, str]:
        row = self.store._db.execute("SELECT owner_account_id, thread_id, cwd, settings_json FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None or row["owner_account_id"] != owner:
            raise ValueError("conversation does not belong to owner")
        if not row["thread_id"]:
            raise ValueError("conversation has no native thread")
        try:
            settings = json.loads(row["settings_json"])
        except (TypeError, json.JSONDecodeError):
            settings = {}
        permission = settings.get("permission") if isinstance(settings, dict) else None
        if permission not in {"read-only","workspace-write","danger-full-access"}:
            raise ValueError("conversation permission is invalid")
        collaboration_mode = settings.get("collaboration_mode", "default") if isinstance(settings, dict) else "default"
        if collaboration_mode not in {"default", "plan"}:
            raise ValueError("conversation collaboration mode is invalid")
        return row["cwd"], permission, collaboration_mode

    @staticmethod
    def _decode_task(row: sqlite3.Row, *, internal: bool = False) -> dict[str, Any]:
        value = dict(row)
        value["schedule"] = json.loads(value.pop("schedule_json"))
        if not internal:
            value.pop("cwd", None)
        return value

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _task(self, task_id: str, owner: str | None = None) -> sqlite3.Row:
        if not isinstance(task_id,str) or not task_id or len(task_id)>160:
            raise ValueError("automation task id is invalid")
        row = self.store._db.execute("SELECT * FROM conversation_automation_tasks WHERE id = ?" + (" AND owner_account_id = ?" if owner else ""), (task_id, owner) if owner else (task_id,)).fetchone()
        if row is None:
            raise ValueError("automation task does not exist")
        return row

    def create(self, conversation_id: str, owner_account_id: str, *, title: str, prompt: str, schedule: Any, request_key: str | None = None, initial_state: str = "active") -> dict[str, Any]:
        owner = self._owner(owner_account_id); title = self._text(title, "title", _MAX_TITLE); prompt = self._text(prompt, "prompt", _MAX_PROMPT); rule = validate_schedule(schedule)
        if initial_state not in {"active","paused"}:
            raise ValueError("invalid initial task state")
        fingerprint = self._fingerprint("create", {"conversation_id": conversation_id, "title": title, "prompt": prompt, "schedule": rule, "initial_state":initial_state})
        now = datetime.now(timezone.utc)
        with self.store._lock:
            try:
                self.store._begin()
                replay = self._mutation(request_key, "create", owner, fingerprint)
                if replay is not None:
                    self.store._finish(None); return replay
                cwd, permission, collaboration_mode = self._source(conversation_id, owner)
                count = self.store._db.execute("SELECT COUNT(*) FROM conversation_automation_tasks WHERE owner_account_id = ? AND state != 'deleted'", (owner,)).fetchone()[0]
                if count >= _MAX_TASKS:
                    raise ValueError("owner task limit reached")
                due = next_due(rule, now)
                if due is None:
                    raise ValueError("一次性执行时间必须晚于当前时间。")
                task_id = str(uuid.uuid4())
                self.store._db.execute("INSERT INTO conversation_automation_tasks(id,conversation_id,owner_account_id,title,prompt,schedule_json,state,cwd,permission,collaboration_mode,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, conversation_id, owner, title, prompt, json.dumps(rule, ensure_ascii=False, sort_keys=True), initial_state, cwd, permission, collaboration_mode, _stamp(due) if initial_state=='active' else None, _stamp(now), _stamp(now)))
                response = self._decode_task(self._task(task_id), internal=False)
                self._save_mutation(request_key, "create", owner, fingerprint, response)
                self.store._finish(None); return response
            except BaseException as error:
                self.store._finish(error); raise

    def list(self, owner_account_id: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        owner = self._owner(owner_account_id)
        with self.store._lock:
            query = "SELECT * FROM conversation_automation_tasks WHERE owner_account_id = ? AND state != 'deleted'"
            args: list[Any] = [owner]
            if conversation_id is not None:
                query += " AND conversation_id = ?"; args.append(conversation_id)
            query += " ORDER BY created_at DESC, id DESC"
            return [self._decode_task(row) for row in self.store._db.execute(query, args).fetchall()]

    def get(self, task_id: str, owner_account_id: str) -> dict[str, Any]:
        owner = self._owner(owner_account_id)
        with self.store._lock:
            return self._decode_task(self._task(task_id, owner))

    def update(self, task_id: str, owner_account_id: str, *, revision: int, title: str | None = None, prompt: str | None = None, schedule: Any = None, state: str | None = None, request_key: str | None = None) -> dict[str, Any]:
        owner = self._owner(owner_account_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision is invalid")
        if title is not None: title = self._text(title, "title", _MAX_TITLE)
        if prompt is not None: prompt = self._text(prompt, "prompt", _MAX_PROMPT)
        if state is not None and (not isinstance(state,str) or state not in _TASK_STATES): raise ValueError("state is invalid")
        rule = validate_schedule(schedule) if schedule is not None else None
        if title is None and prompt is None and rule is None and state is None: raise ValueError("no automation changes supplied")
        payload = {"task_id": task_id, "revision": revision, "title": title, "prompt": prompt, "schedule": rule, "state": state}
        fingerprint = self._fingerprint("update", payload)
        with self.store._lock:
            try:
                self.store._begin()
                replay = self._mutation(request_key, "update", owner, fingerprint)
                if replay is not None:
                    self.store._finish(None); return replay
                current = self._task(task_id, owner)
                if current["revision"] != revision: raise ValueError("automation revision conflict")
                if current["state"] == "deleted":
                    raise ValueError("任务已删除，不能恢复或修改；请重新创建。")
                effective_rule = rule or json.loads(current["schedule_json"])
                effective_state = state or current["state"]
                if effective_state == "active":
                    cwd, permission, collaboration_mode = self._source(current["conversation_id"],owner)
                    if (cwd != current["cwd"] or permission != current["permission"]
                            or collaboration_mode != current["collaboration_mode"]):
                        raise ValueError("对话权限、执行模式或工作目录已改变，请在确认新范围后重新创建任务。")
                current_due = _parse_stamp(current["next_run_at"]) if current["next_run_at"] else None
                # A schedule edit or resume starts a fresh cadence; resuming a
                # one-shot that has already fired deliberately invents no run.
                if effective_state != "active":
                    due = None
                elif rule is not None or current["state"] != "active" or current_due is None:
                    due = next_due(effective_rule, datetime.now(timezone.utc))
                else:
                    due = current_due
                if effective_state == "active" and due is None:
                    raise ValueError("一次性执行时间已过，请先设置新的未来时间。")
                self.store._db.execute("UPDATE conversation_automation_tasks SET title=?,prompt=?,schedule_json=?,state=?,next_run_at=?,updated_at=?,revision=revision+1 WHERE id=?", (title if title is not None else current["title"], "" if effective_state == "deleted" else prompt if prompt is not None else current["prompt"], json.dumps(effective_rule, ensure_ascii=False, sort_keys=True), effective_state, _stamp(due) if due else None, utc_now(), task_id))
                response = self._decode_task(self._task(task_id, owner))
                self._save_mutation(request_key, "update", owner, fingerprint, response)
                self.store._finish(None); return response
            except BaseException as error:
                self.store._finish(error); raise

    def runs(self, task_id: str, owner_account_id: str, limit: int = 30) -> list[dict[str, Any]]:
        owner = self._owner(owner_account_id); limit = max(1, min(int(limit), 100))
        with self.store._lock:
            self._task(task_id, owner)
            return [self._decode_run(row) for row in self.store._db.execute("SELECT * FROM conversation_automation_runs WHERE task_id=? ORDER BY started_at DESC LIMIT ?", (task_id, limit)).fetchall()]

    def _insert_run(self, task: sqlite3.Row, scheduled: datetime, status: str, reason: str | None = None) -> sqlite3.Row:
        if status == "skipped" and reason == "previous_run_pending":
            latest = self.store._db.execute("SELECT * FROM conversation_automation_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1",(task["id"],)).fetchone()
            if latest and latest["status"] == "skipped" and latest["reason"] == reason:
                self.store._db.execute("UPDATE conversation_automation_runs SET skipped_count=skipped_count+1,last_skipped_at=? WHERE id=?",(_stamp(scheduled),latest["id"]))
                return self.store._db.execute("SELECT * FROM conversation_automation_runs WHERE id=?",(latest["id"],)).fetchone()
        run_id = str(uuid.uuid4()); now = utc_now()
        self.store._db.execute("INSERT OR IGNORE INTO conversation_automation_runs(id,task_id,command_id,conversation_id,scheduled_at,started_at,completed_at,status,reason) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, task["id"], "auto-" + run_id, task["conversation_id"], _stamp(scheduled), now, now if status in _RUN_TERMINAL else None, status, reason))
        return self.store._db.execute("SELECT * FROM conversation_automation_runs WHERE task_id=? AND scheduled_at=?", (task["id"], _stamp(scheduled))).fetchone()

    def _skip_due(self, task: sqlite3.Row, current: datetime, reason: str) -> None:
        """Called within the caller's transaction; keep cadence, not catch-up."""
        scheduled = _parse_stamp(task["next_run_at"])
        rule = json.loads(task["schedule_json"])
        self._insert_run(task, scheduled, "skipped", reason)
        if rule["kind"] == "interval":
            seconds = rule["interval_minutes"] * 60
            count = int((current - scheduled).total_seconds() // seconds) + 1
            future = scheduled + timedelta(seconds=count * seconds)
            self.store._db.execute("UPDATE conversation_automation_runs SET skipped_count=?,last_skipped_at=? "
                "WHERE task_id=? AND scheduled_at=?", (count, _stamp(current), task["id"], _stamp(scheduled)))
        else:
            future = None if rule["kind"] == "once" else next_due(rule, current)
        self.store._db.execute("UPDATE conversation_automation_tasks SET next_run_at=?,state=?,updated_at=? WHERE id=?",
            (_stamp(future) if future else None, "active" if future else "finished", utc_now(), task["id"]))

    def configure_module(self, enabled: bool, now: datetime | None = None) -> None:
        """One startup transition; no loop while disabled, no missed-slot replay.

        The marker is operational bookkeeping in the existing journal, not a
        second configuration source. A normal enabled restart retains the
        existing maintenance policy, including its 60-second grace period.
        """
        current = _utc(now)
        with self.store._lock:
            try:
                self.store._begin()
                previous = self.store._db.execute("SELECT value FROM schema_meta "
                    "WHERE key='automation_module_enabled'").fetchone()
                if enabled and previous is not None and previous["value"] != "1":
                    rows = self.store._db.execute("SELECT * FROM conversation_automation_tasks WHERE "
                        "state='active' AND next_run_at IS NOT NULL AND next_run_at<=?", (_stamp(current),)).fetchall()
                    for task in rows:
                        self._skip_due(task, current, "module_disabled")
                self.store._db.execute("INSERT INTO schema_meta(key,value) VALUES('automation_module_enabled',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if enabled else "0",))
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def claim_due(self, now: datetime | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        current = _utc(now); claimed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self.store._lock, self.store.maintenance_admission.admission() as admission:
            if admission['mode'] != 'open':
                return []
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT * FROM conversation_automation_tasks WHERE state='active' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at LIMIT 10", (_stamp(current),)).fetchall()
                for task in rows:
                    scheduled = _parse_stamp(task["next_run_at"])
                    rule = json.loads(task["schedule_json"])
                    # Never make delayed maintenance execute historical work.
                    if current - scheduled > timedelta(seconds=60):
                        self._skip_due(task, current, "missed_maintenance")
                        continue
                    source = self.store._db.execute("SELECT owner_account_id,thread_id,cwd,settings_json FROM conversations WHERE id=?", (task["conversation_id"],)).fetchone()
                    permission = None
                    collaboration_mode = "default"
                    try:
                        settings = json.loads(source["settings_json"]) if source else {}
                        permission = settings.get("permission")
                        collaboration_mode = settings.get("collaboration_mode", "default")
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        pass
                    if (not source or source["owner_account_id"] != task["owner_account_id"]
                            or not source["thread_id"] or source["cwd"] != task["cwd"]
                            or permission != task["permission"]
                            or collaboration_mode != task["collaboration_mode"]):
                        self._insert_run(task, scheduled, "failed", "conversation_source_changed")
                        self.store._db.execute("UPDATE conversation_automation_tasks SET state='paused',next_run_at=NULL,revision=revision+1,updated_at=? WHERE id=?", (utc_now(), task["id"]))
                        continue
                    future = None if rule["kind"] == "once" else next_due(rule, scheduled)
                    self.store._db.execute("UPDATE conversation_automation_tasks SET next_run_at=?,updated_at=? WHERE id=?", (_stamp(future) if future else None, utc_now(), task["id"]))
                    pending = self.store._db.execute("SELECT 1 FROM conversation_automation_runs WHERE task_id=? AND status IN ('dispatching','queued','accepted','delivery_unknown') LIMIT 1", (task["id"],)).fetchone()
                    if pending:
                        self._insert_run(task, scheduled, "skipped", "previous_run_pending")
                    else:
                        run = self._insert_run(task, scheduled, "dispatching")
                        claimed.append((self._decode_task(self._task(task["id"]), internal=True), self._decode_run(run)))
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error); raise
        return claimed

    def _set_run(self, run_id: str, status: str, reason: str | None = None, turn_id: str | None = None) -> None:
        self.store._db.execute("UPDATE conversation_automation_runs SET status=?,reason=COALESCE(?,reason),turn_id=COALESCE(?,turn_id),completed_at=CASE WHEN ? IN ('completed','failed','interrupted','skipped') THEN ? ELSE completed_at END WHERE id=?", (status, reason, turn_id, status, utc_now(), run_id))

    def refresh_runs(self) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT r.*, c.state AS command_state,c.result_turn_id,t.status AS turn_status FROM conversation_automation_runs r LEFT JOIN commands c ON c.id=r.command_id LEFT JOIN turns t ON t.id=c.result_turn_id WHERE r.status IN ('dispatching','queued','accepted','delivery_unknown')").fetchall()
                for row in rows:
                    command = row["command_state"]
                    if command is None: continue
                    if command in {"submitted", "queued"}: status = "queued"
                    elif command == "accepted": status = "accepted"
                    elif command == "delivery_unknown": status = "delivery_unknown"
                    elif command == "rejected": status = "failed"
                    elif command == "completed": status = row["turn_status"] if row["turn_status"] in {"completed", "failed", "interrupted"} else "completed"
                    else: status = "delivery_unknown"
                    self._set_run(row["id"], status, "command_rejected" if command == "rejected" else None, row["result_turn_id"])
                self.store._db.execute("""UPDATE conversation_automation_tasks SET state='finished',revision=revision+1,updated_at=?
                    WHERE state='active' AND next_run_at IS NULL AND json_extract(schedule_json,'$.kind')='once'
                    AND EXISTS (SELECT 1 FROM conversation_automation_runs r WHERE r.task_id=conversation_automation_tasks.id AND r.status IN ('completed','failed','interrupted','skipped'))
                    AND NOT EXISTS (SELECT 1 FROM conversation_automation_runs r WHERE r.task_id=conversation_automation_tasks.id AND r.status IN ('dispatching','queued','accepted','delivery_unknown'))""",(utc_now(),))
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error); raise

    def recover_runs(self) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT r.id,c.id AS command_exists FROM conversation_automation_runs r LEFT JOIN commands c ON c.id=r.command_id WHERE r.status='dispatching'").fetchall()
                for row in rows:
                    if row["command_exists"] is None: self._set_run(row["id"], "failed", "interrupted_before_dispatch")
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error); raise
        self.refresh_runs()


class AutomationScheduler:
    """One cooperative in-process loop; dispatch never retries a run."""
    def __init__(self, store: AutomationStore, dispatch: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]], owner_enabled: Callable[[str], bool], interval_seconds: float = 5):
        if interval_seconds <= 0: raise ValueError("interval_seconds must be positive")
        self.store, self.dispatch, self.owner_enabled, self.interval_seconds = store, dispatch, owner_enabled, interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed: raise RuntimeError("scheduler is closed")
        if self._task is None or self._task.done(): self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None

    async def _loop(self) -> None:
        recovered = False
        while not self._closed:
            try:
                if not recovered:
                    self.store.recover_runs()
                    recovered = True
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                emit("automation_scheduler_error",level="error",exception_type=type(error).__name__)
            await asyncio.sleep(self.interval_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        self.store.refresh_runs()
        for task, run in self.store.claim_due(now):
            try:
                enabled = self.owner_enabled(task["owner_account_id"])
            except Exception as exc:
                # Fail closed.  In particular, an account lookup outage must not
                # kill the scheduler loop or cause an unverified dispatch.
                with self.store.store._lock:
                    self.store.store._db.execute("UPDATE conversation_automation_tasks SET state='paused',next_run_at=NULL,revision=revision+1,updated_at=? WHERE id=?", (utc_now(), task["id"]))
                    self.store._set_run(run["id"], "failed", f"owner_check_error:{type(exc).__name__}")
                continue
            if not enabled:
                with self.store.store._lock:
                    self.store.store._db.execute("UPDATE conversation_automation_tasks SET state='paused',next_run_at=NULL,revision=revision+1,updated_at=? WHERE id=?", (utc_now(), task["id"]))
                    self.store._set_run(run["id"], "failed", "owner_disabled")
                continue
            try:
                result = await self.dispatch(task, run)
                command = result.get("command", result) if isinstance(result, dict) else None
                if isinstance(command, dict):
                    state = command.get("state")
                    if state in {"submitted", "queued"}: state = "queued"
                    elif state == "rejected": state = "failed"
                    elif state not in {"accepted", "delivery_unknown", "completed", "failed", "interrupted"}: state = "delivery_unknown"
                    with self.store.store._lock:
                        self.store._set_run(run["id"], state, reason=command.get("reason"), turn_id=command.get("result_turn_id"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.store.store._lock:
                    recorded = self.store.store._db.execute("SELECT state FROM commands WHERE id=?",(run["command_id"],)).fetchone()
                    self.store._set_run(run["id"], "delivery_unknown" if recorded else "failed", f"dispatch_error:{type(exc).__name__}")
        self.store.refresh_runs()
