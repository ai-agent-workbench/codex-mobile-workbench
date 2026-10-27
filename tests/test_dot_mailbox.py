import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from engine.dot_mailbox import (
    DotConversationService, DotMailbox, DotMailboxConflict,
    DotMailboxDenied, DotMailboxDisabled, DotMailboxError, DotScope,
)
from scripts.dot_mailbox import load_scope


class DotMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dot-mailbox-test-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "dot-mailbox.sqlite3"
        self.service = DotConversationService(self.path,enabled=True)
        self.conversation = self.service.open("synthetic-owner","synthetic-parent-dot")
        self.cid = self.conversation["id"]
        self.scope = DotScope("synthetic-owner",self.cid,"synthetic-parent-dot",frozenset({"read","reply"}))

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def send(self, mid="test-message-1", text="[合成用户消息] 仅验证机制", revision=1):
        return self.service.send(self.cid,"synthetic-owner",message_id=mid,text=text,
                                 expected_revision=revision,origin="synthetic_test")

    def reply(self, mid="test-message-1", rid="test-reply-1", text="[合成回复] 不是父 dot 实际答复", revision=2, scope=None):
        return self.service.reply(scope or self.scope,message_id=mid,reply_id=rid,
                                  text=text,expected_revision=revision)

    def test_disabled_service_does_not_create_database(self):
        path = self.root / "disabled" / "dot-mailbox.sqlite3"
        service = DotConversationService(path)
        with self.assertRaises(DotMailboxDisabled):
            service.open("synthetic-owner","synthetic-parent-dot")
        self.assertFalse(path.parent.exists())

    def test_open_is_idempotent_and_never_rebinds_parent(self):
        self.assertEqual(self.cid,self.service.open("synthetic-owner","synthetic-parent-dot")["id"])
        with self.assertRaises(DotMailboxConflict):
            self.service.open("synthetic-owner","other-dot")

    def test_end_to_end_stores_exact_reply_and_public_message_data(self):
        text = "[合成回复]\n  两个空格\r\n最后一行\n"
        self.send()
        before = self.service.read(self.scope,message_id="test-message-1")
        self.assertEqual(before["revision"],2)
        self.assertEqual(before["message"]["origin"],"synthetic_test")
        self.reply(text=text)
        snapshot = self.service.public_snapshot(self.cid,"synthetic-owner")
        self.assertEqual(snapshot["conversation"]["title"],"dot")
        self.assertEqual(snapshot["conversation"]["kind"],"dot")
        self.assertIsNone(snapshot["conversation"]["thread_id"])
        self.assertEqual(snapshot["conversation"]["revision"],3)
        self.assertEqual(snapshot["messages"][0]["reply"]["text"],text)
        self.assertEqual(snapshot["messages"][0]["reply"]["source"],"parent_dot_via_manual_task_relay")
        self.assertFalse(snapshot["automatic_wakeup"])
        self.assertNotIn("owner_account_id",snapshot["conversation"])
        self.assertNotIn("parent_dot_ref",snapshot["conversation"])

    def test_duplicate_messages_replies_do_not_advance_revision(self):
        one = self.send()
        self.assertEqual(one,self.send())
        reply = self.reply()
        self.assertEqual(reply,self.reply())
        self.assertEqual(self.service.read(self.scope)["revision"],3)
        self.assertEqual(len(self.service.public_snapshot(self.cid,"synthetic-owner")["messages"]),1)

    def test_changed_input_reusing_id_is_rejected(self):
        self.send()
        with self.assertRaises(DotMailboxConflict):
            self.send(text="[合成变更]",revision=2)
        self.reply()
        with self.assertRaises(DotMailboxConflict):
            self.reply(text="[合成不同回复]",revision=3)
        with self.assertRaises(DotMailboxConflict):
            self.reply(rid="another-reply-id",revision=3)

    def test_stale_revision_rejected_without_mutation(self):
        self.send()
        with self.assertRaises(DotMailboxConflict):
            self.send(mid="second",revision=1)
        with self.assertRaises(DotMailboxConflict):
            self.reply(revision=1)
        self.assertEqual(self.service.read(self.scope)["revision"],2)

    def test_out_of_order_reply_and_future_message_are_rejected(self):
        self.send()
        self.send(mid="second",revision=2)
        with self.assertRaises(DotMailboxConflict):
            self.reply(mid="second",revision=3)
        with self.assertRaises(DotMailboxDenied):
            self.reply(mid="nonexistent",revision=3)
        self.reply(revision=3)
        self.reply(mid="second",rid="second-reply",revision=4)
        self.assertIsNone(self.service.read(self.scope)["message"])

    def test_owner_parent_conversation_and_message_scope_denied(self):
        self.send()
        other = self.service.open("other-owner","other-dot")
        self.service.send(other["id"],"other-owner",message_id="foreign-message",
                          text="[合成其他用户消息]",expected_revision=1)
        scopes = [DotScope("other-owner",self.cid,"synthetic-parent-dot"),
                  DotScope("synthetic-owner",self.cid,"other-dot"),
                  DotScope("synthetic-owner",other["id"],"synthetic-parent-dot")]
        for scope in scopes:
            with self.subTest(scope=scope), self.assertRaises(DotMailboxDenied):
                self.service.read(scope)
        with self.assertRaises(DotMailboxDenied):
            self.service.read(self.scope,message_id="foreign-message")
        with self.assertRaises(DotMailboxDenied):
            self.reply(mid="foreign-message")
        with self.assertRaises(DotMailboxDenied):
            self.service.send(self.cid,"other-owner",message_id="foreign-write",
                              text="[合成越权]",expected_revision=2)

    def test_read_scope_cannot_reply_or_mutate_revision(self):
        self.send()
        reader = DotScope("synthetic-owner",self.cid,"synthetic-parent-dot")
        self.service.read(reader)
        with self.assertRaises(DotMailboxDenied):
            self.reply(scope=reader)
        self.assertEqual(self.service.read(reader)["revision"],2)

    def test_scope_and_input_fail_closed(self):
        for bad in ("../auth.sqlite3","x' OR 1=1 --",None,{},"",True):
            with self.subTest(value=bad), self.assertRaises(DotMailboxError):
                self.send(mid=bad)
        for bad in (0,True,"1",None):
            with self.subTest(revision=bad), self.assertRaises(DotMailboxError):
                self.send(revision=bad)
        for bad in ("", "  ",None,{},"x"*60001):
            with self.subTest(text=type(bad)), self.assertRaises(DotMailboxError):
                self.send(text=bad)
        with self.assertRaises(DotMailboxDenied):
            DotScope("synthetic-owner",self.cid,"synthetic-parent-dot",frozenset({"execute"}))

    def test_file_identity_rejects_native_or_foreign_database(self):
        wrong = self.root / "conversations.sqlite3"
        with self.assertRaises(DotMailboxDenied):
            DotMailbox(wrong)
        self.assertFalse(wrong.exists())
        foreign = self.root / "foreign" / "dot-mailbox.sqlite3"
        foreign.parent.mkdir()
        with sqlite3.connect(foreign) as database:
            database.execute("CREATE TABLE conversations(id TEXT)")
        before = foreign.read_bytes()
        with self.assertRaises(DotMailboxDenied):
            DotMailbox(foreign)
        self.assertEqual(before,foreign.read_bytes())

    def test_reopen_preserves_scope_messages_replies_and_idempotency(self):
        self.send()
        expected = self.reply()
        self.service.close()
        self.service = DotConversationService(self.path,enabled=True)
        self.assertEqual(self.service.read(self.scope,message_id="test-message-1")["message"]["reply_text"],expected["text"])
        self.assertEqual(expected,self.reply())
        self.assertEqual(self.service.read(self.scope)["revision"],3)

    def test_concurrent_stale_writers_accept_only_one(self):
        def writer(mid):
            service = DotConversationService(self.path,enabled=True)
            try:
                service.send(self.cid,"synthetic-owner",message_id=mid,
                             text="[合成并发消息]",expected_revision=1)
                return "accepted"
            except DotMailboxConflict:
                return "conflict"
            finally:
                service.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer,("parallel-a","parallel-b")))
        self.assertCountEqual(results,["accepted","conflict"])
        self.assertEqual(self.service.read(self.scope)["revision"],2)

    def test_no_native_runtime_or_client_is_called(self):
        with mock.patch("engine.codex_app_server.CodexAppServerClient",side_effect=AssertionError("native client called")) as client, \
             mock.patch("engine.conversation_runtime.ConversationRuntime.start_conversation",side_effect=AssertionError("native start")) as start, \
             mock.patch("engine.conversation_runtime.ConversationRuntime.send_message",side_effect=AssertionError("native send")) as send:
            self.send()
            self.service.read(self.scope)
            self.reply()
            self.service.public_snapshot(self.cid,"synthetic-owner")
            client.assert_not_called()
            start.assert_not_called()
            send.assert_not_called()

    def scope_file(self,operations=None):
        path = self.root / "isolated-scope.json"
        path.write_text(json.dumps({"schema_version":1,"environment":"isolated-prototype",
            "database":"dot-mailbox.sqlite3","owner_account_id":"synthetic-owner",
            "conversation_id":self.cid,"parent_dot_ref":"synthetic-parent-dot",
            "operations":operations or ["read","reply"]}),encoding="utf-8")
        return path

    def cli(self,*arguments,text=None):
        command=[sys.executable,str(Path(__file__).resolve().parents[1]/"scripts/dot_mailbox.py"),
                 "--isolated-test","--scope",str(self.scope_file()),*arguments]
        environment=os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"]="1"
        return subprocess.run(command,input=text,capture_output=True,text=True,
                              encoding="utf-8",env=environment,timeout=10)

    def test_real_cli_subprocess_read_reply_and_conflict(self):
        self.send()
        read=self.cli("read","--conversation-id",self.cid,"--message-id","test-message-1")
        self.assertEqual(read.returncode,0,read.stderr)
        self.assertEqual(json.loads(read.stdout)["revision"],2)
        body="[合成 CLI 回复] 精确保留\n"
        arguments=("reply","--conversation-id",self.cid,"--message-id","test-message-1",
                   "--reply-id","cli-reply","--expected-revision","2")
        result=self.cli(*arguments,text=body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.service.public_snapshot(self.cid,"synthetic-owner")["messages"][0]["reply"]["text"],body)
        self.assertEqual(self.cli(*arguments,text=body).returncode,0)
        self.assertEqual(self.cli(*arguments,text="[合成不同回复]").returncode,2)

    def test_cli_rejects_other_conversation_and_production_scope(self):
        result=self.cli("read","--conversation-id","not-authorized")
        self.assertEqual(result.returncode,2)
        path=self.scope_file()
        with mock.patch("engine.dot_mailbox_cli.STATE",self.root):
            with self.assertRaises(DotMailboxDenied):
                load_scope(path,isolated_test=True)

    def test_synthetic_production_selector_defaults_off_and_checks_revocation_and_modes(self):
        from engine import dot_mailbox_cli as cli
        scope_path=self.root/"dot-relay"/"scope.json"
        scope_path.parent.mkdir(mode=0o700)
        value={"schema_version":1,"environment":"production-dot","enabled":True,
               "database":"dot-mailbox.sqlite3","owner_account_id":"synthetic-owner",
               "conversation_id":self.cid,"parent_dot_ref":"synthetic-parent-dot","operations":["read"]}
        scope_path.write_text(json.dumps(value),encoding="utf-8");scope_path.chmod(0o600)
        with mock.patch.object(cli,"DOT_CONVERSATION_ENABLED",False):
            with self.assertRaises(DotMailboxDenied):load_scope(scope_path)
        with mock.patch.multiple(cli,DOT_CONVERSATION_ENABLED=True,DOT_LOCAL_RELAY_ENABLED=True,
                                 DOT_LOCAL_RELAY_SCOPE=scope_path,DOT_MAILBOX_DB=self.path,
                                 DOT_OWNER_ACCOUNT_ID="synthetic-owner",DOT_PARENT_REF="synthetic-parent-dot"):
            database,scope=load_scope(scope_path)
            self.assertEqual(database,self.path)
            self.assertEqual(scope.operations,frozenset({"read"}))
            value["enabled"]=False;scope_path.write_text(json.dumps(value),encoding="utf-8")
            with self.assertRaises(DotMailboxDenied):load_scope(scope_path)
            value["enabled"]=True;scope_path.write_text(json.dumps(value),encoding="utf-8")
            scope_path.chmod(0o644)
            with self.assertRaises(DotMailboxDenied):load_scope(scope_path)
            scope_path.chmod(0o600)

    def test_sqlite_read_only_connection_rejects_every_write(self):
        self.send()
        reader = DotConversationService(self.path,enabled=True,read_only=True)
        try:
            self.assertEqual(reader.read(self.scope)["revision"],2)
            with self.assertRaises(DotMailboxDenied):
                reader.reply(self.scope,message_id="test-message-1",reply_id="no-write",
                             text="[合成只读越权]",expected_revision=2)
            with self.assertRaises(sqlite3.OperationalError):
                reader.mailbox._db.execute("DELETE FROM dot_messages")
            self.assertEqual(self.service.read(self.scope)["revision"],2)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
