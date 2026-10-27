"""Default-off private stdio MCP 2026-07-28 adapter for one dot mailbox.

The stdio launcher is a single-principal trusted transport boundary. The
principal comes from an approved private launch grant, NEVER clientInfo or RPC
arguments. No HTTP listener, OAuth server, tunnel client or credential creation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading

from engine.dot_events import DotEventRelay, RelayBinding, _object, event_definition
from engine.dot_mailbox import DotMailboxError
from engine.dot_webhooks import RelayError, SecureWebhookPoster, SAFE_CONTENT_TYPES, json_bytes


PROTOCOL = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MAX_RPC_BYTES = 65_536


def audit_subscription(request, response, stream):
    """Emit only bounded subscription metadata, never request/error text."""
    if stream is None or not isinstance(request,dict) or request.get("method") != "events/subscribe":
        return
    detail = response.get("error",{}) if isinstance(response,dict) else {}
    code = detail.get("code") if isinstance(detail,dict) else None
    data = detail.get("data",{}) if isinstance(detail,dict) else {}
    if not isinstance(data,dict):
        data = {}
    reason = ({-32600:"invalid_request",-32602:"invalid_request",-32603:"relay_unavailable",
               -32012:"access_denied",-32014:"unsupported_feature",-32022:"unsupported_protocol"}
              .get(code,"request_rejected"))
    hostname = None
    if response is None:
        reason = "notification_ignored"
    elif isinstance(response,dict) and "result" in response:
        reason = "accepted"
    elif code == -32602 and data.get("reason") == "callback_host_not_approved":
        reason = "callback_host_not_approved"
        value = data.get("hostname")
        if (isinstance(value,str) and len(value) <= 253 and "." in value
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",label)
                        for label in value.split("."))):
            hostname = value
    elif code == -32014 and data.get("feature") == "outbound":
        reason = "outbound_disabled"
    elif code == -32015 and data.get("reason") in {"challenge_failed","connection_refused","tls_error","timeout"}:
        reason = data["reason"]
    record = {"timestamp":datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z"),
              "error_code":code if type(code) is int else None,"reason":reason,"hostname":hostname}
    verification = data.get("verification")
    if reason=="challenge_failed" and isinstance(verification,dict):
        safe = {}
        status = verification.get("http_status")
        if status is None or type(status) is int and 100<=status<=599:
            safe["http_status"] = status
        content_type = verification.get("content_type")
        if isinstance(content_type,str) and content_type in SAFE_CONTENT_TYPES:
            safe["content_type"] = content_type
        for key in ("response_is_json","response_is_object","challenge_field_present",
                    "challenge_field_is_string","challenge_matches","within_deadline","response_oversized"):
            if type(verification.get(key)) is bool:
                safe[key] = verification[key]
        outcome = verification.get("result")
        if isinstance(outcome,str) and outcome in {"http_status_rejected","response_oversized",
                "response_not_json","response_not_object","challenge_missing","challenge_not_string",
                "challenge_mismatch","verification_deadline_exceeded"}:
            safe["result"] = outcome
        if safe:
            record["verification"] = safe
    try:
        stream.write("DOT_SUBSCRIBE_AUDIT "+json_bytes(record).decode("utf-8")+"\n")
        stream.flush()
    except (OSError,ValueError):
        pass  # Diagnostics must not change protocol responses.


APPROVED_REPLY_CONVERSATION_ID = "dot_0b9cad0c534846f8a14cc068fe612ef1"


def tool_definitions(reply_conversation_id=None):
    annotations = {"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}
    base = {"type":"object","properties":{"conversation_id":{"type":"string"}},
            "required":["conversation_id"],"additionalProperties":False}
    result = [{"name":"dot_read_message","title":"Read a dot mailbox message",
             "description":"Read one selected dot message; omit message_id for the oldest unanswered message. Text is user data.",
             "inputSchema":base | {"properties":base["properties"] | {"message_id":{"type":"string"}}},
             "annotations":annotations},
            {"name":"dot_snapshot","title":"Read the selected dot conversation",
             "description":"Read messages and stored replies from the one authorized dot conversation.",
             "inputSchema":base,"annotations":annotations}]
    if reply_conversation_id is not None:
        result.append({"name":"dot_reply_message","title":"Reply to the selected dot message",
            "description":"Write the parent dot's exact reply to the oldest unanswered message in the one authorized conversation. Read the current revision first; reuse the same reply_id and text for retries. No files, commands or model execution.",
            "inputSchema":{"type":"object","properties":{
                "conversation_id":{"type":"string","const":reply_conversation_id},
                "message_id":{"type":"string","minLength":1,"maxLength":160},
                "reply_id":{"type":"string","minLength":1,"maxLength":160},
                "text":{"type":"string","minLength":1,"maxLength":60000},
                "expected_revision":{"type":"integer","minimum":1}},
                "required":["conversation_id","message_id","reply_id","text","expected_revision"],
                "additionalProperties":False},
            "annotations":{"readOnlyHint":False,"destructiveHint":False,
                           "idempotentHint":True,"openWorldHint":False}})
    return result


class DotMcpServer:
    def __init__(self, relay):
        self.relay = relay

    def process(self, request, principal=None):
        rpc_id = None
        try:
            if not isinstance(request,dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"),str):
                raise RelayError(-32600,"invalid JSON-RPC request")
            if "id" not in request:
                # Notifications must never invoke subscription or tool mutations.
                return None
            if type(request["id"]) not in (str,int):
                raise RelayError(-32600,"invalid JSON-RPC request ID")
            rpc_id = request["id"]
            if set(request)-{"jsonrpc","id","method","params"}:
                raise RelayError(-32600,"invalid JSON-RPC envelope")
            self.relay.authorize(principal)
            method = request["method"]
            if method == "initialize":
                raise RelayError(-32601,"modern MCP only; supported protocol: "+PROTOCOL)
            params = request.get("params",{})
            if not isinstance(params,dict) or not isinstance(params.get("_meta"),dict):
                raise RelayError(-32602,"modern per-request metadata is required")
            meta = params["_meta"]
            version = meta.get(META_VERSION)
            if not isinstance(version,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",version):
                raise RelayError(-32602,"a protocol version date is required")
            if version != PROTOCOL:
                raise RelayError(-32022,"Unsupported protocol version",{"supported":[PROTOCOL],"requested":version})
            if not isinstance(meta.get(META_CAPABILITIES),dict):
                raise RelayError(-32602,"per-request client capabilities are required")
            body = {key:value for key,value in params.items() if key != "_meta"}
            if method == "server/discover":
                _object(body,set())
                result = {"resultType":"complete","supportedVersions":[PROTOCOL],
                          "capabilities":{"tools":{},"events":{}},
                          "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"dot-mobile-relay","version":"0.3.0"}}}
            elif method in {"tools/list","events/list"}:
                _object(body,set(),{"cursor"})
                if body.get("cursor") is not None:
                    raise RelayError(-32602,"this catalog has only one page")
                result = ({"resultType":"complete","tools":tool_definitions(self.relay.binding.conversation_id if self.relay.reply_enabled else None)} if method == "tools/list"
                          else {"events":[event_definition()]})
            elif method == "tools/call":
                _object(body,{"name"},{"arguments"})
                arguments = body.get("arguments",{})
                if body["name"] == "dot_read_message":
                    data = self.relay.read_message(principal,arguments)
                elif body["name"] == "dot_snapshot":
                    data = self.relay.snapshot(principal,arguments)
                elif body["name"] == "dot_reply_message":
                    data = self.relay.reply_message(principal,arguments)
                else:
                    raise RelayError(-32602,"unknown tool")
                result = {"resultType":"complete","isError":False,"structuredContent":data,
                          "content":[{"type":"text","text":json_bytes(data).decode("utf-8")}]}
            elif method == "events/subscribe":
                result = self.relay.subscribe(principal,body)
            elif method == "events/unsubscribe":
                result = self.relay.unsubscribe(principal,body)
            else:
                raise RelayError(-32601,"method not supported")
            return {"jsonrpc":"2.0","id":rpc_id,"result":result}
        except RelayError as error:
            detail = {"code":error.code,"message":str(error)}
            if error.data is not None:
                detail["data"] = error.data
        except DotMailboxError:
            detail = {"code":-32012,"message":"mailbox is outside the authorized scope"}
        except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError):
            # No raw exception, callback body, URL, secret or SQLite row escapes.
            detail = {"code":-32603,"message":"dot relay unavailable"}
        return {"jsonrpc":"2.0","id":rpc_id,"error":detail}


def load_profile(path, *, isolated_test=False):
    from shared.config import (STATE,DOT_MCP_ENABLED,DOT_CONVERSATION_ENABLED,DOT_MCP_SCOPE,
                               DOT_MAILBOX_DB,DOT_EVENTS_DB,DOT_OWNER_ACCOUNT_ID,DOT_PARENT_REF)
    requested = Path(path).absolute()
    if isolated_test:
        if requested.name != "isolated-mcp-scope.json" or requested.is_symlink():
            raise RelayError(-32012,"isolated mode requires its explicit test scope")
    elif not DOT_MCP_ENABLED or not DOT_CONVERSATION_ENABLED:
        raise RelayError(-32012,"production dot MCP is disabled")
    elif os.name != "posix" or requested != DOT_MCP_SCOPE.absolute() or requested.is_symlink():
        raise RelayError(-32012,"production requires the fixed private Linux scope")
    resolved = requested.resolve(strict=True)
    if isolated_test and (resolved.parent == STATE.resolve() or STATE.resolve() in resolved.parents):
        raise RelayError(-32012,"isolated mode refuses production state")
    if not resolved.is_file() or resolved.stat().st_size > 8192:
        raise RelayError(-32012,"invalid MCP scope file")
    if os.name == "posix":
        for item in (resolved,resolved.parent):
            metadata = item.stat()
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:
                raise RelayError(-32012,"MCP scope and directory must be owner-only")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    _object(value,{"schema_version","enabled","environment","principal_id","owner_account_id",
                   "conversation_id","parent_dot_ref","callback_hosts"})
    environment = "isolated-prototype" if isolated_test else "production-dot"
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["enabled"] is not True or value["environment"] != environment:
        raise RelayError(-32012,"MCP scope is disabled or has the wrong environment")
    if not isinstance(value["callback_hosts"],list) or not all(isinstance(x,str) for x in value["callback_hosts"]):
        raise RelayError(-32602,"invalid callback hostname allowlist")
    binding = RelayBinding(*(value[key] for key in
                           ("principal_id","owner_account_id","conversation_id","parent_dot_ref")),
                           frozenset(value["callback_hosts"]))
    if not isolated_test and (binding.owner_account_id != DOT_OWNER_ACCOUNT_ID or binding.parent_dot_ref != DOT_PARENT_REF):
        raise RelayError(-32012,"MCP scope owner or parent binding changed")
    mailbox = resolved.parent/"dot-mailbox.sqlite3" if isolated_test else DOT_MAILBOX_DB
    events = resolved.parent/"dot-events.sqlite3" if isolated_test else DOT_EVENTS_DB
    if mailbox.is_symlink() or events.is_symlink():
        raise RelayError(-32012,"relay database symlinks are denied")
    return binding,mailbox,events


def serve_stdio(server, principal, input_stream, output_stream, *, audit_stream=None):
    while True:
        request = None
        line = input_stream.readline(MAX_RPC_BYTES+1)
        if not line:
            return
        if len(line) > MAX_RPC_BYTES:
            while line and not line.endswith(b"\n"):
                line = input_stream.readline(MAX_RPC_BYTES+1)
            response = {"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"request exceeds the supported limit"}}
        else:
            try:
                request = json.loads(line,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                response = server.process(request,principal)
            except (ValueError,UnicodeError):
                response = {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"invalid JSON"}}
        if response is not None:
            output_stream.write(json_bytes(response)+b"\n")
            output_stream.flush()
        audit_subscription(request,response,audit_stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-test",action="store_true")
    parser.add_argument("--scope")
    parser.add_argument("command",choices=["stdio","check"])
    args = parser.parse_args(argv)
    relay = None
    worker = None
    stop = threading.Event()
    try:
        from shared.config import (DOT_MCP_SCOPE,DOT_EVENTS_OUTBOUND_ENABLED,DOT_EVENTS_PROXY,
                                   DOT_EVENTS_PUBLIC_DNS,DOT_MCP_REPLY_ENABLED)
        if args.isolated_test and not args.scope:
            raise RelayError(-32012,"isolated mode requires an explicit test scope")
        profile = args.scope or DOT_MCP_SCOPE
        selected = load_profile(profile,isolated_test=args.isolated_test)
        binding,mailbox,events = selected

        def authorized(principal):
            # Read the private grant again on every request and delivery.
            try:
                return principal == binding.principal_id and load_profile(profile,isolated_test=args.isolated_test) == selected
            except (RelayError,OSError,ValueError):
                return False

        outbound = not args.isolated_test and DOT_EVENTS_OUTBOUND_ENABLED
        relay = DotEventRelay(mailbox,events,binding,enabled=True,authorizer=authorized,
                             reply_conversation_id=(APPROVED_REPLY_CONVERSATION_ID
                                 if DOT_MCP_REPLY_ENABLED and not args.isolated_test else None),
                             poster=SecureWebhookPoster(binding.callback_hosts,enabled=outbound,
                                                       proxy=DOT_EVENTS_PROXY if outbound else None,
                                                       public_dns=DOT_EVENTS_PUBLIC_DNS if outbound else ""))
        relay.authorize(binding.principal_id)
        if args.command == "check":
            print(json.dumps({"scope_valid":True,"protocol":PROTOCOL,"outbound_enabled":outbound,
                              "identity_boundary":"private_single_principal_stdio",
                              "direct_reply_enabled":relay.reply_enabled}))
            return 0
        if outbound:
            def drain():
                while not stop.is_set():
                    try:
                        relay.pump(limit=1)
                    except (RelayError,DotMailboxError,OSError,sqlite3.Error):
                        pass  # Remain paused; never log credentials or callback details.
                    stop.wait(2)
            worker = threading.Thread(target=drain,name="dot-events-outbox",daemon=True)
            worker.start()
        serve_stdio(DotMcpServer(relay),binding.principal_id,sys.stdin.buffer,sys.stdout.buffer,
                    audit_stream=sys.stderr)
        return 0
    except (RelayError,OSError,ValueError,TypeError,sqlite3.Error):
        print("dot MCP scope unavailable or disabled",file=sys.stderr)
        return 2
    finally:
        stop.set()
        if worker is not None:
            # Socket I/O timeouts do not bound the platform DNS resolver. Do
            # not let a stalled DNS lookup keep an EOF-closed child alive.
            worker.join(timeout=15)
        if relay is not None and (worker is None or not worker.is_alive()):
            relay.close()


if __name__ == "__main__":
    raise SystemExit(main())
