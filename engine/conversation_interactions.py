"""Typed, durable handling of native App Server approval and elicitation requests.

This module deliberately contains no websocket, Runtime, HTTP, or browser wiring.
It validates the version-pinned native wire shapes before keeping a minimal
projection in SQLite; request and response payloads never become database
state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, ValidationError, SchemaError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_store import ACTIVE_TURN_STATUSES, ConversationStore, utc_now


_COMMAND = "item/commandExecution/requestApproval"
_FILE = "item/fileChange/requestApproval"
_PERMISSIONS = "item/permissions/requestApproval"
_MCP = "mcpServer/elicitation/request"
_METHODS = {_COMMAND, _FILE, _PERMISSIONS, _MCP}
NATIVE_INTERACTION_METHODS = frozenset(_METHODS)
_TERMINAL = frozenset({"resolved", "cancelled", "closed"})
_SCHEMA_NAMES = {
    _COMMAND: ("CommandExecutionRequestApprovalParams", "CommandExecutionRequestApprovalResponse"),
    _FILE: ("FileChangeRequestApprovalParams", "FileChangeRequestApprovalResponse"),
    _PERMISSIONS: ("PermissionsRequestApprovalParams", "PermissionsRequestApprovalResponse"),
    _MCP: ("McpServerElicitationRequestParams", "McpServerElicitationRequestResponse"),
}
_RECEIPT_KEY = secrets.token_bytes(32)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise AppServerProtocolError("native interaction JSON is invalid") from None


def _error(message: str) -> AppServerProtocolError:
    # Native request/response values can contain commands, paths, form data and
    # verification material.  Do not turn those values into public errors.
    return AppServerProtocolError(message)


def _load_schemas() -> dict[str, dict[str, Any]]:
    root = Path(__file__).with_name("fixtures") / "native_interactions_0_159_2"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in manifest.get("files", [])}
    result: dict[str, dict[str, Any]] = {}
    for names in _SCHEMA_NAMES.values():
        for name in names:
            if name not in result:
                path = root / f"{name}.json"
                raw = path.read_bytes()
                if expected.get(path.name) != hashlib.sha256(raw).hexdigest():
                    raise RuntimeError("native interaction schema fixture integrity check failed")
                result[name] = json.loads(raw)
    return result


_SCHEMAS = _load_schemas()


def _no_retrieval(uri: str):
    raise NoSuchResource(ref=uri)

_OFFLINE_REGISTRY = Registry(retrieve=_no_retrieval)
_VALIDATORS = {name: Draft7Validator(schema, registry=_OFFLINE_REGISTRY) for name, schema in _SCHEMAS.items()}


def _validate_schema(name: str, value: Any, label: str) -> None:
    try:
        _VALIDATORS[name].validate(value)
    except (ValidationError, TypeError, ValueError) as error:
        raise _error(f"native {label} is invalid") from error


def _decision_key(value: Any) -> str:
    return _canonical(value)


def _command_decisions(params: dict[str, Any]) -> list[Any]:
    choices = params.get("availableDecisions")
    if isinstance(choices, list):
        return list(choices)
    # The schema permits older servers to omit availableDecisions.  Present
    # every scalar legal choice plus exactly the proposals supplied by native.
    result: list[Any] = ["accept", "acceptForSession", "decline", "cancel"]
    amendment = params.get("proposedExecpolicyAmendment")
    if isinstance(amendment, list):
        result.append({"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}})
    for amendment in params.get("proposedNetworkPolicyAmendments") or []:
        if isinstance(amendment, dict):
            result.append({"applyNetworkPolicyAmendment": {"network_policy_amendment": amendment}})
    return result


def _display(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == _COMMAND:
        return {
            "kind": "command", "command": params.get("command"), "cwd": params.get("cwd"),
            "reason": params.get("reason"), "commandActions": params.get("commandActions"),
            "availableDecisions": _command_decisions(params),
            "networkApprovalContext": params.get("networkApprovalContext"),
            "additionalPermissions": params.get("additionalPermissions"),
            "proposedExecpolicyAmendment": params.get("proposedExecpolicyAmendment"),
            "proposedNetworkPolicyAmendments": params.get("proposedNetworkPolicyAmendments"),
            "approvalKind": params.get("kind", "command"), "environmentId": params.get("environmentId"),
            "approvalId": params.get("approvalId"),
        }
    if method == _FILE:
        return {"kind": "fileChange", "itemId": params.get("itemId"), "reason": params.get("reason"),
                "grantRoot": params.get("grantRoot"),
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}
    if method == _PERMISSIONS:
        return {"kind": "permissions", "cwd": params.get("cwd"), "reason": params.get("reason"),
                "requested": params.get("permissions"), "scopes": ["turn", "session"],
                "strictAutoReview": True}
    mode = params.get("mode")
    display = {"kind": "mcpElicitation", "serverName": params.get("serverName"), "mode": mode}
    for key in ("message", "requestedSchema", "url", "title", "description", "challenge", "elicitationId"):
        if key in params:
            display[key] = params[key]
    return display


def normalize_request(method: str, params: Any) -> dict[str, Any]:
    """Validate a native request and return a stable in-memory projection."""
    if method not in _METHODS:
        raise _error("unsupported native interaction method")
    _validate_schema(_SCHEMA_NAMES[method][0], params, "interaction request")
    assert isinstance(params, dict)  # ensured by the schema
    if method == _MCP:
        _mcp_content_validator(params)
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    native_item_id = params.get("itemId")
    if not isinstance(thread_id, str) or not thread_id:
        raise _error("native interaction thread is invalid")
    if method != _MCP and (not isinstance(turn_id, str) or not turn_id or not isinstance(native_item_id, str) or not native_item_id):
        raise _error("native interaction identity is invalid")
    if method == _COMMAND:
        approval_id = params.get("approvalId")
        logical_key = f"{native_item_id}:{approval_id!r}:{params.get('kind', 'command')}"
    elif method in {_FILE, _PERMISSIONS}:
        logical_key = f"{method}:{native_item_id}"
    else:
        # An MCP request is not inferred to be a retransmission merely because
        # its human message matches another one.  ElicitationId is the only
        # native anchor we may use, and it is not present for every mode.
        anchor = params.get("elicitationId")
        logical_key = f"{method}:{anchor}" if isinstance(anchor, str) and anchor else f"{method}:unanchored:{uuid.uuid4()}"
    return {
        "method": method, "thread_id": thread_id, "turn_id": turn_id if isinstance(turn_id, str) else None,
        "native_item_id": native_item_id if isinstance(native_item_id, str) else None,
        "logical_key": logical_key, "fingerprint": hashlib.sha256(_canonical(params).encode()).hexdigest(),
        "display": _display(method, params), "params": params,
    }


def command_choices(normalized: dict[str, Any]) -> list[Any]:
    if normalized.get("method") == _COMMAND:
        return list(normalized["display"]["availableDecisions"])
    if normalized.get("method") == _FILE:
        return ["accept", "acceptForSession", "decline", "cancel"]
    return []


def _permissions_subset(requested: Any, granted: Any) -> bool:
    if not isinstance(requested, dict) or not isinstance(granted, dict):
        return False
    for section, response_value in granted.items():
        if section not in requested:
            return False
        requested_value = requested[section]
        if response_value is None:
            continue
        if not isinstance(response_value, dict) or not isinstance(requested_value, dict):
            return False
        if section == "fileSystem" and response_value:
            denies = {_canonical(entry) for entry in (requested_value.get("entries") or []) if entry.get("access") == "deny"}
            if not denies.issubset({_canonical(entry) for entry in (response_value.get("entries") or [])}):
                return False
        if section == "fileSystem" and isinstance(requested_value.get("globScanMaxDepth"), int):
            glob_grants = any(entry.get("access") != "deny" and entry.get("path", {}).get("type") == "glob_pattern"
                              for entry in (response_value.get("entries") or []))
            if glob_grants and response_value.get("globScanMaxDepth") is None:
                return False
        for key, value in response_value.items():
            if key not in requested_value:
                return False
            original = requested_value[key]
            if value is None:
                continue
            if key == "entries":
                if not isinstance(value, list) or not isinstance(original, list):
                    return False
                original_keys = {_canonical(entry): entry for entry in original}
                for entry in value:
                    if _canonical(entry) in original_keys:
                        continue
                    if entry.get("access") != "read" or not any(
                        old.get("access") == "write" and old.get("path") == entry.get("path") for old in original
                    ):
                        return False
                # A submitted filesystem grant must preserve every native deny;
                # otherwise removing it could broaden a simultaneous grant.
                denies = {_canonical(entry) for entry in original if entry.get("access") == "deny"}
                if not denies.issubset({_canonical(entry) for entry in value}):
                    return False
            elif key in {"read", "write"}:
                if not isinstance(value, list) or not isinstance(original, list) or not set(value).issubset(set(original)):
                    return False
            elif key == "globScanMaxDepth":
                if isinstance(value, bool) or not isinstance(value, int) or isinstance(original, bool) or not isinstance(original, int) or value > original:
                    return False
            elif section == "network" and key == "enabled" and value is False and original is True:
                continue
            elif value != original:
                return False
    return True


def _without_null_metadata(value: Any) -> Any:
    """Draft-7 rejects native nullable metadata such as ``minLength: null``."""
    if isinstance(value, dict):
        return {key: _without_null_metadata(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_without_null_metadata(child) for child in value]
    return value


def _mcp_content_validator(params: dict[str, Any]):
    mode = params.get("mode")
    if mode not in {"form", "openai/form", "openaiForm"}:
        return None
    requested = params.get("requestedSchema")
    if mode == "form":
        if not isinstance(requested, dict):
            raise _error("native MCP requested schema is invalid")
        # Only the standard restricted form dialect has nullable metadata.
        schema = {"type": "object", "properties": _without_null_metadata(requested.get("properties", {})),
                  "required": requested.get("required") or []}
    else:
        # Extended forms carry genuine JSON Schema, including composition,
        # local definitions, boolean schemas and const:null constraints.
        schema = requested
    try:
        # The selected dialect defines which keys are schemas or annotations.
        # Never pre-walk arbitrary data; offline retrieval fails if an actual
        # evaluated reference needs a resource outside this submitted schema.
        cls = validator_for(schema)
        cls.check_schema(schema)
        return cls(schema, registry=_OFFLINE_REGISTRY)
    except (RuntimeError, SchemaError, TypeError, ValueError):
        raise _error("native MCP requested schema is invalid or not locally resolvable") from None


def validate_response(normalized: dict[str, Any], response: Any) -> dict[str, Any]:
    """Return a canonical native response, rejecting capability expansion."""
    method = normalized.get("method")
    if method not in _METHODS or not isinstance(normalized.get("params"), dict):
        raise _error("native interaction is invalid")
    _validate_schema(_SCHEMA_NAMES[method][1], response, "interaction response")
    assert isinstance(response, dict)
    params = normalized["params"]
    safe = json.loads(_canonical(response))
    if method == _COMMAND:
        if _decision_key(response.get("decision")) not in {_decision_key(v) for v in _command_decisions(params)}:
            raise _error("command approval decision was not offered")
    elif method == _FILE:
        if response.get("decision") not in {"accept", "acceptForSession", "decline", "cancel"}:
            raise _error("file approval decision is invalid")
    elif method == _PERMISSIONS:
        if not _permissions_subset(params.get("permissions"), response.get("permissions")):
            raise _error("permission approval expands the requested profile")
        if response.get("scope", "turn") not in {"turn", "session"}:
            raise _error("permission approval scope is invalid")
    else:
        action = response.get("action")
        if action in {"decline", "cancel"}:
            if response.get("content") is not None:
                raise _error("declined MCP elicitation cannot contain content")
            safe["content"] = None
        elif action == "accept":
            if params.get("mode") == "openai/userVerification":
                # This core has no device-verifier integration. A non-null
                # string is not proof, so accepting would falsely claim one.
                raise _error("user verification acceptance is unavailable")
            validator = _mcp_content_validator(params)
            if validator is not None:
                try:
                    validator.validate(response.get("content"))
                except (ValidationError, Unresolvable, TypeError, ValueError):
                    raise _error("MCP elicitation content is invalid or not locally resolvable") from None
        else:
            raise _error("MCP elicitation action is invalid")
    return safe


def declined_response(normalized: dict[str, Any]) -> dict[str, Any]:
    """A fail-closed scheduled response; it never simulates an approval."""
    method = normalized.get("method")
    if method == _COMMAND:
        choices = _command_decisions(normalized["params"])
        if "decline" in choices:
            return {"decision": "decline"}
        if "cancel" in choices:
            return {"decision": "cancel"}
        raise _error("native command offers no non-approval decision")
    if method == _FILE:
        return {"decision": "decline"}
    if method == _PERMISSIONS:
        return {"permissions": {}}
    if method == _MCP:
        return {"action": "decline", "content": None}
    raise _error("native interaction is invalid")


class NativeInteractionStore:
    """Additive durable state for native interactions, backed by ConversationStore."""

    def __init__(self, store: ConversationStore):
        self.store = store
        self._params: dict[str, dict[str, Any]] = {}
        self._prepare()

    def _prepare(self) -> None:
        with self.store._lock:
            version = self.store._db.execute("SELECT value FROM schema_meta WHERE key='native_interactions_extension_version'").fetchone()
            if version is not None and version["value"] != "1":
                raise _error("unsupported native interaction extension version")
            self.store._db.executescript("""
                CREATE TABLE IF NOT EXISTS native_interaction_requests (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    method TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT, native_item_id TEXT,
                    logical_key TEXT NOT NULL, fingerprint TEXT NOT NULL, display_json TEXT NOT NULL,
                    state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                    rpc_id_json TEXT, rpc_id_type TEXT, connection_id TEXT,
                    receipt TEXT, answered_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS native_interaction_rpc_idx ON native_interaction_requests(connection_id, rpc_id_type, rpc_id_json);
                CREATE INDEX IF NOT EXISTS native_interaction_logical_idx ON native_interaction_requests(conversation_id, logical_key);
                CREATE TABLE IF NOT EXISTS native_interaction_commands (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES native_interaction_requests(id) ON DELETE CASCADE,
                    attempt INTEGER NOT NULL, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL,
                    answered_by TEXT, created_at TEXT NOT NULL,
                    UNIQUE(request_id, id)
                );
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES('native_interactions_extension_version', '1');
            """)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "conversation_id": row["conversation_id"], "method": row["method"], "thread_id": row["thread_id"],
                "turn_id": row["turn_id"], "native_item_id": row["native_item_id"],
                "logical_key": row["logical_key"], "fingerprint": row["fingerprint"],
                "display": json.loads(row["display_json"]), "state": row["state"],
                "attempt": row["attempt"], "answered_by": row["answered_by"]}

    def _live(self, conversation_id: str, normalized: dict[str, Any]) -> None:
        row = self.store._db.execute("SELECT id, thread_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None or row["thread_id"] != normalized["thread_id"]:
            raise _error("native interaction conversation or thread is unknown")
        turn_id = normalized["turn_id"]
        if turn_id is not None:
            turn = self.store._db.execute("SELECT status FROM turns WHERE id=? AND conversation_id=?", (turn_id, conversation_id)).fetchone()
            if turn is None or turn["status"] not in ACTIVE_TURN_STATUSES:
                raise _error("native interaction turn is not live")

    @staticmethod
    def _rpc(request_id_json: str, request_id_type: str) -> tuple[str, str]:
        if request_id_type not in {"string", "integer"}:
            raise _error("native interaction RPC id type is invalid")
        try:
            parsed = json.loads(request_id_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise _error("native interaction RPC id is invalid") from error
        if request_id_type == "string" and (not isinstance(parsed, str) or not parsed):
            raise _error("native interaction RPC id is invalid")
        if request_id_type == "integer" and (not isinstance(parsed, int) or isinstance(parsed, bool)):
            raise _error("native interaction RPC id is invalid")
        return _canonical(parsed), request_id_type

    def register(self, conversation_id: str, normalized: dict[str, Any], request_id_json: str,
                 request_id_type: str, connection_id: str) -> tuple[dict[str, Any], bool]:
        if not isinstance(connection_id, str) or not connection_id:
            raise _error("native interaction connection is invalid")
        rpc_json, rpc_type = self._rpc(request_id_json, request_id_type)
        if normalized.get("method") not in _METHODS:
            raise _error("native interaction is invalid")
        with self.store._lock:
            try:
                self.store._begin()
                self._live(conversation_id, normalized)
                duplicate = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE connection_id=? AND rpc_id_type=? AND rpc_id_json=?", (connection_id, rpc_type, rpc_json)).fetchone()
                if duplicate is not None:
                    if (duplicate["fingerprint"] != normalized["fingerprint"] or duplicate["conversation_id"] != conversation_id
                            or duplicate["method"] != normalized["method"] or duplicate["thread_id"] != normalized["thread_id"]
                            or duplicate["turn_id"] != normalized["turn_id"]):
                        raise _error("native interaction RPC id payload mismatch")
                    self._params[duplicate["id"]] = normalized["params"]
                    self.store._finish(None)
                    return self._public(duplicate), False
                rebind = None
                # Reissue only with a real native anchor.  URL MCP requests
                # provide elicitationId even without a turn/item; unanchored
                # MCP requests must never be guessed from their message.
                mcp_anchor = (
                    normalized["method"] == _MCP
                    and isinstance(normalized["params"].get("elicitationId"), str)
                    and bool(normalized["params"]["elicitationId"])
                )
                if normalized.get("native_item_id") or mcp_anchor:
                    rebind = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE conversation_id=? AND logical_key=? AND fingerprint=? AND state IN ('awaiting_reissue','delivery_unknown') ORDER BY created_at DESC LIMIT 1", (conversation_id, normalized["logical_key"], normalized["fingerprint"])).fetchone()
                now = utc_now()
                if rebind is not None:
                    self.store._db.execute("UPDATE native_interaction_requests SET attempt=attempt+1, state='awaiting', rpc_id_json=?, rpc_id_type=?, connection_id=?, updated_at=? WHERE id=?", (rpc_json, rpc_type, connection_id, now, rebind["id"]))
                    row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (rebind["id"],)).fetchone()
                    bound = True
                else:
                    interaction_id = f"ni_{uuid.uuid4().hex}"
                    self.store._db.execute("INSERT INTO native_interaction_requests(id,conversation_id,method,thread_id,turn_id,native_item_id,logical_key,fingerprint,display_json,state,attempt,rpc_id_json,rpc_id_type,connection_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'awaiting',1,?,?,?,?,?)", (interaction_id, conversation_id, normalized["method"], normalized["thread_id"], normalized["turn_id"], normalized["native_item_id"], normalized["logical_key"], normalized["fingerprint"], _canonical(normalized["display"]), rpc_json, rpc_type, connection_id, now, now))
                    row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (interaction_id,)).fetchone()
                    bound = True
                self._params[row["id"]] = normalized["params"]
                self.store._db.execute("UPDATE conversations SET updated_at=?, revision=revision+1 WHERE id=?", (now, conversation_id))
                self.store._finish(None)
                return self._public(row), bound
            except BaseException as error:
                self.store._finish(error)
                raise

    def claim(self, conversation_id: str, interaction_id: str, command_id: str, attempt: int,
              fingerprint: str, response: Any, answered_by: str) -> tuple[dict[str, Any], bool]:
        if not all(isinstance(x, str) and x for x in (interaction_id, command_id, fingerprint, answered_by)) or type(attempt) is not int:
            raise _error("native interaction command is invalid")
        with self.store._lock:
            try:
                self.store._begin()
                row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=? AND conversation_id=?", (interaction_id, conversation_id)).fetchone()
                if row is None or row["attempt"] != attempt or row["fingerprint"] != fingerprint:
                    raise _error("native interaction is stale")
                # A same-command retry may arrive after native completion has
                # cleared in-memory source params. Its receipt is HMACed with a
                # process-only key, so it can be compared without persisting or
                # offline-testing the response. New commands still require live
                # request params and full native schema/capability validation.
                raw_receipt = hmac.new(_RECEIPT_KEY, _canonical(response).encode(), hashlib.sha256).hexdigest()
                existing = self.store._db.execute("SELECT * FROM native_interaction_commands WHERE id=?", (command_id,)).fetchone()
                if existing is not None:
                    if (existing["request_id"] == interaction_id and existing["attempt"] == attempt
                            and existing["fingerprint"] == fingerprint and existing["answered_by"] == answered_by
                            and hmac.compare_digest(existing["receipt"], raw_receipt)):
                        self.store._finish(None)
                        return self._public(row), False
                    raise _error("native interaction command conflicts")
                self._live(conversation_id, {"thread_id": row["thread_id"], "turn_id": row["turn_id"]})
                if row["state"] != "awaiting":
                    raise _error("native interaction is stale")
                normalized = {"method": row["method"], "params": {}}
                params = self._params.get(interaction_id)
                if params is None:
                    raise _error("native interaction request is no longer live")
                normalized["params"] = params
                validate_response(normalized, response)
                receipt = raw_receipt
                now = utc_now()
                self.store._db.execute("INSERT INTO native_interaction_commands(id,request_id,attempt,fingerprint,receipt,answered_by,created_at) VALUES(?,?,?,?,?,?,?)", (command_id, interaction_id, attempt, fingerprint, receipt, answered_by, now))
                self.store._db.execute("UPDATE native_interaction_requests SET state='claimed',receipt=?,answered_by=?,updated_at=? WHERE id=?", (receipt, answered_by, now, interaction_id))
                self.store._db.execute("UPDATE conversations SET updated_at=?, revision=revision+1 WHERE id=?", (now, conversation_id))
                row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (interaction_id,)).fetchone()
                self.store._finish(None)
                return self._public(row), True
            except BaseException as error:
                self.store._finish(error)
                raise

    def _touch(self, conversation_id: str) -> None:
        self.store._db.execute("UPDATE conversations SET updated_at=?, revision=revision+1 WHERE id=?", (utc_now(), conversation_id))

    def sent(self, interaction_id: str, attempt: int, connection_id: str) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (interaction_id,)).fetchone()
                if row is None or row["attempt"] != attempt or row["connection_id"] != connection_id:
                    raise _error("native interaction send is stale")
                # Native may clear the callback before the local sender observes
                # completion. That terminal acknowledgement is already enough;
                # do not log an avoidable stale-send error or change it back.
                if row["state"] in _TERMINAL:
                    self.store._finish(None)
                    return
                if row["state"] != "claimed":
                    raise _error("native interaction send is stale")
                self.store._db.execute("UPDATE native_interaction_requests SET state='response_sent',updated_at=? WHERE id=?", (utc_now(), interaction_id))
                self._touch(row["conversation_id"])
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def resolved(self, thread_id: str, typedrpcid: str, connection_id: str) -> dict[str, Any] | None:
        try:
            rpc = _canonical(json.loads(typedrpcid))
        except (TypeError, json.JSONDecodeError) as error:
            raise _error("native interaction RPC id is invalid") from error
        with self.store._lock:
            try:
                self.store._begin()
                row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE thread_id=? AND rpc_id_json=? AND connection_id=? AND state IN ('awaiting','claimed','response_sent')", (thread_id, rpc, connection_id)).fetchone()
                if row is None:
                    self.store._finish(None)
                    return None
                self.store._db.execute("UPDATE native_interaction_requests SET state='resolved',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                self._params.pop(row["id"], None)
                self._touch(row["conversation_id"])
                row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (row["id"],)).fetchone()
                self.store._finish(None)
                return self._public(row)
            except BaseException as error:
                self.store._finish(error)
                raise

    def close_turn(self, thread_id: str, turn_id: str) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT id,conversation_id FROM native_interaction_requests WHERE thread_id=? AND turn_id=? AND state NOT IN ('resolved','cancelled','closed')", (thread_id, turn_id)).fetchall()
                self.store._db.execute("UPDATE native_interaction_requests SET state='closed',updated_at=? WHERE thread_id=? AND turn_id=? AND state NOT IN ('resolved','cancelled','closed')", (utc_now(), thread_id, turn_id))
                for row in rows: self._params.pop(row["id"], None); self._touch(row["conversation_id"])
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def close_conversation(self, conversation_id: str) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT id FROM native_interaction_requests WHERE conversation_id=? AND state NOT IN ('resolved','cancelled','closed')", (conversation_id,)).fetchall()
                self.store._db.execute("UPDATE native_interaction_requests SET state='closed',updated_at=? WHERE conversation_id=? AND state NOT IN ('resolved','cancelled','closed')", (utc_now(), conversation_id))
                for row in rows: self._params.pop(row["id"], None)
                if rows: self._touch(conversation_id)
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def connection_lost(self, connection_id: str) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT id,conversation_id FROM native_interaction_requests WHERE connection_id=? AND state IN ('awaiting','claimed','response_sent')", (connection_id,)).fetchall()
                self.store._db.execute("UPDATE native_interaction_requests SET state=CASE WHEN state='awaiting' THEN 'awaiting_reissue' ELSE 'delivery_unknown' END,updated_at=? WHERE connection_id=? AND state IN ('awaiting','claimed','response_sent')", (utc_now(), connection_id))
                for row in rows: self._params.pop(row["id"], None); self._touch(row["conversation_id"])
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def recover(self) -> None:
        with self.store._lock:
            try:
                self.store._begin()
                rows = self.store._db.execute("SELECT id,conversation_id FROM native_interaction_requests WHERE state IN ('awaiting','claimed','response_sent')").fetchall()
                self.store._db.execute("UPDATE native_interaction_requests SET state=CASE WHEN state='awaiting' THEN 'awaiting_reissue' ELSE 'delivery_unknown' END,updated_at=? WHERE state IN ('awaiting','claimed','response_sent')", (utc_now(),))
                for row in rows: self._params.pop(row["id"], None); self._touch(row["conversation_id"])
                self.store._finish(None)
            except BaseException as error:
                self.store._finish(error)
                raise

    def list(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE conversation_id=? ORDER BY created_at,id", (conversation_id,)).fetchall()
            return [self._public(row) for row in rows]

    def get(self, interaction_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._db.execute("SELECT * FROM native_interaction_requests WHERE id=?", (interaction_id,)).fetchone()
            return self._public(row) if row is not None else None
