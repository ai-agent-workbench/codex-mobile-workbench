"""Synthetic-only direct reply authorization and transaction regression tests."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.dot_events import DotEventRelay, RelayBinding
from engine.dot_mailbox import DotConversationService, DotScope, MCP_REPLY_SOURCE
from engine.dot_mcp import DotMcpServer, PROTOCOL, META_VERSION, META_CAPABILITIES


class DotMcpReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mailbox = DotConversationService(self.root / "dot-mailbox.sqlite3",enabled=True)
        self.cid = self.mailbox.open("synthetic-owner","synthetic-parent")["id"]
        self.binding = RelayBinding("synthetic-principal","synthetic-owner",self.cid,
                                    "synthetic-parent",frozenset())
        self.granted = True
        self.relay = self.make_relay(self.cid)
        self.server = DotMcpServer(self.relay)
        self.send("message-1")

    def tearDown(self):
        self.relay.close()
        self.mailbox.close()
        self.temp.cleanup()

    def make_relay(self, cid=None):
        return DotEventRelay(self.root / "dot-mailbox.sqlite3",self.root / "dot-events.sqlite3",
                             self.binding,enabled=True,authorizer=lambda _:self.granted,
                             reply_conversation_id=cid)

    def send(self, mid):
        self.mailbox.send(self.cid,"synthetic-owner",message_id=mid,text="[synthetic user]",
                          expected_revision=self.snapshot()["conversation"]["revision"],origin="synthetic_test")

    def snapshot(self):
        return self.mailbox.public_snapshot(self.cid,"synthetic-owner")

    def request(self, method, body, *, principal="synthetic-principal"):
        return self.server.process({"jsonrpc":"2.0","id":1,"method":method,
            "params":{"_meta":{META_VERSION:PROTOCOL,META_CAPABILITIES:{}},**body}},principal)

    def arguments(self, **changes):
        return {"conversation_id":self.cid,"message_id":"message-1","reply_id":"reply-1",
                "text":"  [synthetic parent exact reply]\n","expected_revision":self.snapshot()["conversation"]["revision"],**changes}

    def reply(self, args=None, **kwargs):
        return self.request("tools/call",{"name":"dot_reply_message","arguments":args or self.arguments()},**kwargs)

    def test_catalog_has_exact_scope_and_no_extra_capability(self):
        catalog = self.request("tools/list",{})["result"]["tools"]
        self.assertEqual({t["name"] for t in catalog},{"dot_read_message","dot_snapshot","dot_reply_message"})
        tool = catalog[-1]
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["idempotentHint"])
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(tool["inputSchema"]["properties"]["conversation_id"]["const"],self.cid)
        self.assertEqual(set(tool["inputSchema"]["required"]),set(self.arguments()))
        self.assertIsNone(self.relay._reply_mailbox)
        self.assertTrue(self.relay.mailbox.mailbox.read_only)
        self.assertFalse((self.root / "dot-events.sqlite3").exists())

    def test_disabled_and_mismatched_grant_hide_and_deny_writer(self):
        for grant in (None,"another-conversation"):
            relay = self.make_relay(grant)
            try:
                self.server = DotMcpServer(relay)
                self.assertEqual(len(self.request("tools/list",{})["result"]["tools"]),2)
                self.assertEqual(self.reply()["error"]["code"],-32012)
                self.assertIsNone(relay._reply_mailbox)
            finally:
                relay.close()
        self.assertIsNone(self.snapshot()["messages"][0]["reply"])

    def test_exact_reply_retry_source_and_revision(self):
        args = self.arguments()
        result = self.reply(args)["result"]["structuredContent"]
        self.assertEqual(result["reply"]["text"],args["text"])
        self.assertEqual(result["reply"]["source"],MCP_REPLY_SOURCE)
        revision = self.snapshot()["conversation"]["revision"]
        self.assertEqual(revision,args["expected_revision"]+1)
        self.assertEqual(self.reply(args)["result"]["structuredContent"],result)
        self.assertEqual(self.snapshot()["conversation"]["revision"],revision)
        self.assertEqual(self.reply(args | {"text":"different"})["error"]["code"],-32009)
        self.assertTrue(self.relay.mailbox.mailbox.read_only)

    def test_order_cas_and_reply_id_collision(self):
        self.send("message-2")
        self.assertEqual(self.reply(self.arguments(message_id="message-2"))["error"]["code"],-32009)
        self.assertEqual(self.reply(self.arguments(expected_revision=1))["error"]["code"],-32009)
        self.assertIn("result",self.reply())
        self.assertEqual(self.reply(self.arguments(message_id="message-2"))["error"]["code"],-32009)
        self.assertIn("result",self.reply(self.arguments(message_id="message-2",reply_id="reply-2")))

    def test_principal_conversation_message_and_revocation(self):
        self.assertEqual(self.reply(principal="other")["error"]["code"],-32012)
        self.assertEqual(self.reply(self.arguments(conversation_id="other"))["error"]["code"],-32012)
        self.assertEqual(self.reply(self.arguments(message_id="not-in-conversation"))["error"]["code"],-32012)
        foreign = self.mailbox.open("foreign-owner","foreign-parent")["id"]
        self.mailbox.send(foreign,"foreign-owner",message_id="foreign-message",text="synthetic",expected_revision=1)
        self.assertEqual(self.reply(self.arguments(message_id="foreign-message"))["error"]["code"],-32012)
        self.granted = False
        self.assertEqual(self.reply()["error"]["code"],-32012)
        self.assertIsNone(self.snapshot()["messages"][0]["reply"])

    def test_closed_schema_and_invalid_values_never_write(self):
        for key in self.arguments():
            args = self.arguments(); del args[key]
            self.assertEqual(self.reply(args)["error"]["code"],-32602)
        for changes in ({"path":"/tmp/synthetic"},{"command":"true"},{"source":"fake-model"},
                        {"expected_revision":True},{"expected_revision":0},{"text":" "},
                        {"text":"x"*60001},{"reply_id":"../bad"},{"message_id":[]}):
            with self.subTest(fields=list(changes)):
                self.assertEqual(self.reply(self.arguments(**changes))["error"]["code"],-32602)
        self.assertIsNone(self.snapshot()["messages"][0]["reply"])

    def test_notification_never_invokes_writer(self):
        result = self.server.process({"jsonrpc":"2.0","method":"tools/call",
            "params":{"name":"dot_reply_message","arguments":self.arguments()}},self.binding.principal_id)
        self.assertIsNone(result)
        self.assertIsNone(self.relay._reply_mailbox)

    def test_existing_cli_reply_cannot_be_overwritten_or_impersonated(self):
        args = self.arguments()
        scope = DotScope("synthetic-owner",self.cid,"synthetic-parent",frozenset({"reply"}))
        self.mailbox.reply(scope,**{k:v for k,v in args.items() if k != "conversation_id"})
        self.assertEqual(self.reply(args)["error"]["code"],-32009)
        self.assertEqual(self.snapshot()["messages"][0]["reply"]["source"],"parent_dot_via_manual_task_relay")

    def test_revocation_after_dispatch_before_writer(self):
        self.relay.authorizer = mock.Mock(side_effect=[True,True,False])
        self.assertEqual(self.reply()["error"]["code"],-32012)
        self.assertIsNone(self.relay._reply_mailbox)
        self.assertIsNone(self.snapshot()["messages"][0]["reply"])


if __name__ == "__main__":
    unittest.main()
