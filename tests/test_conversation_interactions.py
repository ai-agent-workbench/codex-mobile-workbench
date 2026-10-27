from __future__ import annotations

import os
import tempfile
import unittest

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_interactions import (
    NativeInteractionStore, command_choices, declined_response, normalize_request, validate_response,
)
from engine.conversation_store import ConversationStore, utc_now
from tests.store_fixtures import seeded_store


COMMAND = "item/commandExecution/requestApproval"
FILE = "item/fileChange/requestApproval"
PERMISSIONS = "item/permissions/requestApproval"
MCP = "mcpServer/elicitation/request"


def command(*, item="item1", approval=None, available=None):
    value = {"itemId": item, "threadId": "thread1", "turnId": "turn1", "startedAtMs": 1,
             "command": "echo safe", "kind": "command"}
    if approval is not None:
        value["approvalId"] = approval
    if available is not None:
        value["availableDecisions"] = available
    return value


def file_change():
    return {"itemId": "file1", "threadId": "thread1", "turnId": "turn1", "startedAtMs": 1,
            "reason": "write"}


def permissions(requested=None):
    return {"itemId": "perm1", "threadId": "thread1", "turnId": "turn1", "startedAtMs": 1,
            "cwd": "/tmp", "permissions": requested if requested is not None else {}}


def mcp(mode="form"):
    value = {"serverName": "server", "threadId": "thread1", "turnId": None, "mode": mode,
             "message": "provide form", "requestedSchema": {"type": "object", "properties": {
                 "name": {"type": "string"}}, "required": ["name"]}}
    if mode == "url":
        value.pop("requestedSchema")
        value["url"] = "https://example.invalid/continue"
        value["elicitationId"] = "el1"
    if mode == "openai/userVerification":
        value.pop("requestedSchema")
        value.update({"title": "Verify", "description": "device", "challenge": "challenge"})
    return value


class NativeInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = seeded_store(os.path.join(self.temp.name, "conversations.sqlite3"))
        self.store.create_conversation("conv1", title="x", cwd="/tmp", settings={})
        now = utc_now()
        with self.store._lock:
            self.store._db.execute("UPDATE conversations SET thread_id=? WHERE id='conv1'", ("thread1",))
            self.store._db.execute("INSERT INTO turns(id,conversation_id,ordinal,status,kind,started_at) VALUES('turn1','conv1',1,'active','model',?)", (now,))
        self.interactions = NativeInteractionStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_all_native_methods_and_choices(self):
        cmd = normalize_request(COMMAND, command(available=["accept", "acceptForSession", "decline", "cancel"]))
        self.assertEqual(command_choices(cmd), ["accept", "acceptForSession", "decline", "cancel"])
        self.assertEqual(command_choices(normalize_request(FILE, file_change())), ["accept", "acceptForSession", "decline", "cancel"])
        self.assertEqual(normalize_request(PERMISSIONS, permissions())["display"]["scopes"], ["turn", "session"])
        self.assertEqual(normalize_request(MCP, mcp())["turn_id"], None)

    def test_command_missing_available_keeps_native_proposals(self):
        source = command()
        source["proposedExecpolicyAmendment"] = ["echo safe"]
        source["proposedNetworkPolicyAmendments"] = [{"host": "example.test", "action": "allow"}]
        choices = command_choices(normalize_request(COMMAND, source))
        self.assertIn("acceptForSession", choices)
        self.assertIn({"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["echo safe"]}}, choices)
        self.assertIn({"applyNetworkPolicyAmendment": {"network_policy_amendment": {"host": "example.test", "action": "allow"}}}, choices)

    def test_scheduled_cancel_when_decline_not_offered(self):
        normalized = normalize_request(COMMAND, command(available=["accept", "cancel"]))
        self.assertEqual(declined_response(normalized), {"decision": "cancel"})

    def test_nullable_native_permissions_and_read_downgrade_are_supported(self):
        requested={"fileSystem":{"read":["/tmp/a"], "write":None, "entries":None, "globScanMaxDepth":None}, "network":None}
        normalized=normalize_request(PERMISSIONS, permissions(requested))
        response={"permissions":{"fileSystem":{"read":["/tmp/a"], "write":None, "entries":None, "globScanMaxDepth":None}, "network":None}}
        self.assertEqual(validate_response(normalized,response),response)
        requested={"fileSystem":{"entries":[{"access":"write","path":{"type":"path","path":"/tmp/a"}}]}}
        response={"permissions":{"fileSystem":{"entries":[{"access":"read","path":{"type":"path","path":"/tmp/a"}}]}}}
        self.assertEqual(validate_response(normalize_request(PERMISSIONS,permissions(requested)),response),response)

    def test_glob_depth_and_deny_cannot_disappear_from_legacy_grants(self):
        requested={"fileSystem":{"entries":[{"access":"read","path":{"type":"glob_pattern","pattern":"/tmp/*"}}],"globScanMaxDepth":2}}
        normalized=normalize_request(PERMISSIONS,permissions(requested))
        with self.assertRaises(AppServerProtocolError):
            validate_response(normalized,{"permissions":{"fileSystem":{"entries":requested["fileSystem"]["entries"]}}})
        requested={"fileSystem":{"read":["/tmp"],"entries":[{"access":"deny","path":{"type":"path","path":"/tmp/secret"}}]}}
        with self.assertRaises(AppServerProtocolError):
            validate_response(normalize_request(PERMISSIONS,permissions(requested)),{"permissions":{"fileSystem":{"read":["/tmp"]}}})

    def test_command_decision_must_be_offered(self):
        normalized = normalize_request(COMMAND, command(available=["decline"]))
        with self.assertRaises(AppServerProtocolError):
            validate_response(normalized, {"decision": "accept"})
        self.assertEqual(validate_response(normalized, {"decision": "decline"}), {"decision": "decline"})

    def test_file_all_four_decisions_and_scheduled_decline(self):
        normalized = normalize_request(FILE, file_change())
        for decision in ("accept", "acceptForSession", "decline", "cancel"):
            self.assertEqual(validate_response(normalized, {"decision": decision})["decision"], decision)
        self.assertEqual(declined_response(normalized), {"decision": "decline"})

    def test_permissions_are_a_subset_and_deny_cannot_expand(self):
        requested = {"fileSystem": {"entries": [
            {"access": "read", "path": {"type": "path", "path": "/tmp/a"}},
            {"access": "deny", "path": {"type": "path", "path": "/tmp/no"}},
        ]}}
        normalized = normalize_request(PERMISSIONS, permissions(requested))
        good = {"permissions": requested, "scope": "session", "strictAutoReview": True}
        self.assertEqual(validate_response(normalized, good)["scope"], "session")
        injected = {"permissions": {"fileSystem": {"entries": [
            {"access": "write", "path": {"type": "path", "path": "/tmp/a"}}]}}}
        with self.assertRaises(AppServerProtocolError):
            validate_response(normalized, injected)
        self.assertEqual(declined_response(normalized), {"permissions": {}})

    def test_extended_mcp_preserves_full_json_schema_and_offline_local_refs(self):
        value=mcp('openai/form')
        value['requestedSchema']={'$schema':'https://json-schema.org/draft/2020-12/schema', '$defs':{'name':{'type':'string','minLength':2}},
            'type':'object','properties':{'name':{'$ref':'#/$defs/name'},'nil':{'const':None}},'required':['name','nil'],
            'additionalProperties':False,'allOf':[{'properties':{'name':{'pattern':'^ok'}}}]}
        normalized=normalize_request(MCP,value)
        self.assertEqual(validate_response(normalized,{'action':'accept','content':{'name':'okay','nil':None}})['action'],'accept')
        for content in ({'name':'bad','nil':None},{'name':'okay','nil':'not-null'},{'name':'okay','nil':None,'extra':True}):
            with self.assertRaises(AppServerProtocolError):validate_response(normalized,{'action':'accept','content':content})
        value['requestedSchema']={'$ref':'https://example.invalid/no-network'}
        normalized=normalize_request(MCP,value)
        with self.assertRaises(AppServerProtocolError):validate_response(normalized,{'action':'accept','content':{}})
        value['requestedSchema']={'type':'object','properties':{'x':{'$ref':'#/$defs/missing'}}}
        normalized=normalize_request(MCP,value)
        with self.assertRaises(AppServerProtocolError):validate_response(normalized,{'action':'accept','content':{'x':'private-content'}})
        value['requestedSchema']=True
        self.assertEqual(validate_response(normalize_request(MCP,value),{'action':'accept','content':42})['content'],42)

    def test_extended_mcp_uses_declared_dialect_without_extra_keyword_semantics(self):
        value=mcp("openai/form")
        value["requestedSchema"]={"$schema":"http://json-schema.org/draft-07/schema#",
            "type":"string","contentSchema":"opaque annotation",
            "prefixItems":{"$ref":"https://example.invalid/ordinary-data"}}
        normalized=normalize_request(MCP,value)
        self.assertEqual(validate_response(normalized,{"action":"accept","content":"valid"})["content"],"valid")

    def test_extended_mcp_data_ref_values_are_not_schema_references(self):
        value = mcp("openai/form")
        external_data = {"$ref": "https://example.invalid/value"}
        value["requestedSchema"] = {
            "type": "object",
            "properties": {
                "$ref": {"type": "string"},
                "constant": {"const": external_data},
                "choice": {"enum": [external_data], "default": external_data},
            },
            "required": ["$ref", "constant", "choice"],
        }
        normalized = normalize_request(MCP, value)
        self.assertEqual(validate_response(normalized, {"action": "accept", "content": {
            "$ref": "property name", "constant": external_data, "choice": external_data,
        }})["action"], "accept")

    def test_mcp_form_and_user_verification(self):
        form = normalize_request(MCP, mcp())
        self.assertEqual(validate_response(form, {"action": "accept", "content": {"name": "ok"}})["action"], "accept")
        self.assertEqual(validate_response(form, {"action": "accept", "content": {"name": "ok", "extra": "allowed"}})["action"], "accept")
        self.assertEqual(validate_response(form, {"action": "decline", "content": None})["content"], None)
        verify = normalize_request(MCP, mcp("openai/userVerification"))
        with self.assertRaises(AppServerProtocolError):
            validate_response(verify, {"action": "accept"})
        with self.assertRaises(AppServerProtocolError):
            validate_response(verify, {"action": "accept", "content": "actual-proof"})
        self.assertEqual(validate_response(verify, {"action": "decline", "content": None})["action"], "decline")

    def test_same_item_multiple_approval_ids_are_distinct(self):
        self.assertNotEqual(normalize_request(COMMAND, command(approval="a"))["logical_key"], normalize_request(COMMAND, command(approval="b"))["logical_key"])

    def test_register_duplicate_and_payload_mismatch(self):
        normalized = normalize_request(COMMAND, command())
        one, bound = self.interactions.register("conv1", normalized, '"rpc"', "string", "connection1")
        same, duplicate_bound = self.interactions.register("conv1", normalized, '"rpc"', "string", "connection1")
        self.assertTrue(bound)
        self.assertFalse(duplicate_bound)
        self.assertEqual(one["id"], same["id"])
        changed = normalize_request(COMMAND, command())
        changed["fingerprint"] = "different"
        with self.assertRaises(AppServerProtocolError):
            self.interactions.register("conv1", changed, '"rpc"', "string", "connection1")

    def test_claim_duplicate_click_and_response_is_not_persisted(self):
        normalized = normalize_request(COMMAND, command(available=["accept", "decline"]))
        record, _ = self.interactions.register("conv1", normalized, '1', "integer", "connection1")
        claimed, won = self.interactions.claim("conv1", record["id"], "click1", 1, record["fingerprint"], {"decision": "accept"}, "user")
        again, won_again = self.interactions.claim("conv1", record["id"], "click1", 1, record["fingerprint"], {"decision": "accept"}, "user")
        self.assertTrue(won)
        self.assertFalse(won_again)
        self.assertEqual(claimed["id"], again["id"])
        with self.store._lock:
            columns = self.store._db.execute("PRAGMA table_info(native_interaction_requests)").fetchall()
        self.assertNotIn("response_json", [column[1] for column in columns])

    def test_mcp_response_never_persisted_or_public(self):
        normalized = normalize_request(MCP, mcp())
        record, _ = self.interactions.register("conv1", normalized, '2', "integer", "connection1")
        self.interactions.claim("conv1", record["id"], "click2", 1, record["fingerprint"], {"action": "accept", "content": {"name": "very-secret"}}, "user")
        self.assertNotIn("very-secret", repr(self.interactions.get(record["id"])))
        with self.store._lock:
            dumped = " ".join(str(value) for row in self.store._db.execute("SELECT * FROM native_interaction_requests").fetchall() for value in row)
        self.assertNotIn("very-secret", dumped)

    def test_old_attempt_and_cross_connection_are_rejected(self):
        normalized = normalize_request(COMMAND, command())
        record, _ = self.interactions.register("conv1", normalized, '3', "integer", "connection1")
        self.interactions.connection_lost("connection1")
        rebound, _ = self.interactions.register("conv1", normalized, '4', "integer", "connection2")
        self.assertEqual(rebound["id"], record["id"])
        self.assertEqual(rebound["attempt"], 2)
        with self.assertRaises(AppServerProtocolError):
            self.interactions.claim("conv1", record["id"], "old", 1, record["fingerprint"], {"decision": "decline"}, "user")

    def test_url_mcp_reissues_only_with_elicitation_anchor(self):
        normalized = normalize_request(MCP, mcp("url"))
        first, _ = self.interactions.register("conv1", normalized, '51', "integer", "connection1")
        self.interactions.connection_lost("connection1")
        reissued, _ = self.interactions.register("conv1", normalize_request(MCP, mcp("url")), '52', "integer", "connection2")
        self.assertEqual((reissued["id"], reissued["attempt"], reissued["state"]), (first["id"], 2, "awaiting"))
        with self.assertRaises(AppServerProtocolError):
            self.interactions.claim("conv1", first["id"], "old-url", 1, first["fingerprint"], {"action": "accept"}, "user")
        claimed, won = self.interactions.claim("conv1", reissued["id"], "new-url", 2, reissued["fingerprint"], {"action": "accept"}, "user")
        self.assertTrue(won)
        self.interactions.sent(claimed["id"], 2, "connection2")
        self.assertEqual(self.interactions.resolved("thread1", '52', "connection2")["state"], "resolved")

    def test_url_mcp_delivery_unknown_reissues_without_reusing_old_answer(self):
        first, _ = self.interactions.register("conv1", normalize_request(MCP, mcp("url")), '55', "integer", "connection1")
        self.interactions.claim("conv1", first["id"], "url-answer-1", 1, first["fingerprint"], {"action": "accept"}, "user")
        self.interactions.connection_lost("connection1")
        self.assertEqual(self.interactions.get(first["id"])["state"], "delivery_unknown")
        reissued, _ = self.interactions.register("conv1", normalize_request(MCP, mcp("url")), '56', "integer", "connection2")
        self.assertEqual(reissued["attempt"], 2)
        with self.assertRaises(AppServerProtocolError):
            self.interactions.claim("conv1", reissued["id"], "url-answer-1", 2, reissued["fingerprint"], {"action": "accept"}, "user")
        _, won = self.interactions.claim("conv1", reissued["id"], "url-answer-2", 2, reissued["fingerprint"], {"action": "accept"}, "user")
        self.assertTrue(won)

    def test_unanchored_mcp_never_reissues_across_connections(self):
        first, _ = self.interactions.register("conv1", normalize_request(MCP, mcp()), '61', "integer", "connection1")
        self.interactions.connection_lost("connection1")
        second, _ = self.interactions.register("conv1", normalize_request(MCP, mcp()), '62', "integer", "connection2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "awaiting")
        self.assertEqual(self.interactions.get(first["id"])["state"], "awaiting_reissue")

    def test_null_turn_mcp_needs_known_thread_but_not_turn(self):
        normalized = normalize_request(MCP, mcp())
        record, _ = self.interactions.register("conv1", normalized, '5', "integer", "connection1")
        self.assertIsNone(record["turn_id"])
        bad = normalize_request(MCP, mcp())
        bad["thread_id"] = "unknown"
        with self.assertRaises(AppServerProtocolError):
            self.interactions.register("conv1", bad, '6', "integer", "connection1")

    def test_sent_resolved_disconnect_and_terminal_states(self):
        normalized = normalize_request(COMMAND, command())
        record, _ = self.interactions.register("conv1", normalized, '7', "integer", "connection1")
        self.interactions.claim("conv1", record["id"], "click7", 1, record["fingerprint"], {"decision": "decline"}, "user")
        self.interactions.sent(record["id"], 1, "connection1")
        resolved = self.interactions.resolved("thread1", '7', "connection1")
        self.assertEqual(resolved["state"], "resolved")
        repeated, won = self.interactions.claim("conv1", record["id"], "click7", 1, record["fingerprint"], {"decision": "decline"}, "user")
        self.assertFalse(won)
        self.assertEqual(repeated["state"], "resolved")
        self.interactions.connection_lost("connection1")
        self.assertEqual(self.interactions.get(record["id"])["state"], "resolved")
        other, _ = self.interactions.register("conv1", normalize_request(FILE, file_change()), '8', "integer", "connection1")
        self.interactions.close_turn("thread1", "turn1")
        self.assertEqual(self.interactions.get(other["id"])["state"], "closed")

    def test_claim_requires_live_known_turn(self):
        normalized = normalize_request(COMMAND, command())
        record, _ = self.interactions.register("conv1", normalized, '9', "integer", "connection1")
        with self.store._lock:
            self.store._db.execute("UPDATE turns SET status='completed' WHERE id='turn1'")
        with self.assertRaises(AppServerProtocolError):
            self.interactions.claim("conv1", record["id"], "click9", 1, record["fingerprint"], {"decision": "decline"}, "user")

    def test_recover_reissues_awaiting_and_touches_revision(self):
        normalized = normalize_request(COMMAND, command())
        record, _ = self.interactions.register("conv1", normalized, '10', "integer", "connection1")
        before = self.store.get_conversation("conv1")["revision"]
        self.interactions.recover()
        self.assertEqual(self.interactions.get(record["id"])["state"], "awaiting_reissue")
        self.assertGreater(self.store.get_conversation("conv1")["revision"], before)

    def test_mcp_external_reference_is_rejected_without_network(self):
        request = mcp("openai/form")
        request["requestedSchema"] = {"$ref": "https://example.invalid/schema"}
        with self.assertRaises(AppServerProtocolError):
            normalized = normalize_request(MCP, request)
            validate_response(normalized, {"action": "accept", "content": {}})

    def test_additive_old_store_schema(self):
        self.assertEqual(self.store._db.execute("SELECT value FROM schema_meta WHERE key='native_interactions_extension_version'").fetchone()[0], "1")
        self.assertIsNotNone(self.store._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='native_interaction_requests'").fetchone())


if __name__ == "__main__":
    unittest.main()
