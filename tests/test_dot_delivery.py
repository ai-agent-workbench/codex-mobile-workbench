"""Synthetic state, fake sender; never reads the real notification target."""
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest import mock
from engine.dot_delivery import DotDeliveryStore,deliver_pending
from engine.dot_mailbox import DotMailboxError,DotMailboxDenied
from engine.dot_notifications import notification_link

class DotDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"delivery.sqlite3"
        self.store=DotDeliveryStore(self.path)
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.store.close)
        self.now=datetime.now(timezone.utc)
        self.sender=mock.Mock(return_value=(True,"sent"))
        self.kw=dict(owner="owner",cid="dot",parent="parent",sender=self.sender,link="https://example.invalid/settings/dot",now=self.now)
    def summary(self, revision, *, age=0):
        return {"reply_generation":revision,"replies":[{"id":"reply-"+str(revision),"written_revision":revision,"created_at":(self.now-timedelta(seconds=age)).isoformat()}]}
    def test_first_enable_never_sends_historical_replies(self):
        self.assertEqual(deliver_pending(self.store,self.summary(3),**self.kw),0)
        self.sender.assert_not_called()
    def test_new_reply_is_once_across_polls_retries_and_processes(self):
        self.store.arm("owner","dot","parent",3)
        self.assertEqual(deliver_pending(self.store,self.summary(5),**self.kw),1)
        other=DotDeliveryStore(self.path)
        try:self.assertEqual(deliver_pending(other,self.summary(5),**self.kw),0)
        finally:other.close()
        self.assertEqual(self.sender.call_count,1)
        title,body=self.sender.call_args.args
        self.assertEqual(title,"dot 有新回复")
        self.assertEqual(body,"dot 有新回复\n\n[打开工作台](https://example.invalid/settings/dot)")
        self.assertEqual(self.store.notice_state("owner","dot","reply:reply-5"),"sent")
    def test_failed_or_crashed_attempt_is_not_retried(self):
        self.store.arm("owner","dot","parent",0)
        self.sender.side_effect=TimeoutError()
        deliver_pending(self.store,self.summary(3),**self.kw)
        self.assertEqual(self.store.notice_state("owner","dot","reply:reply-3"),"unknown")
        deliver_pending(self.store,self.summary(3),**self.kw)
        self.assertEqual(self.sender.call_count,1)
        self.assertTrue(self.store.claim("owner","dot","reply:reply-5"))
        deliver_pending(self.store,self.summary(5),**self.kw)
        self.assertEqual(self.sender.call_count,1)
    def test_old_future_malformed_replies_never_send(self):
        self.store.arm("owner","dot","parent",0)
        for revision,age in [(3,601),(5,-1)]:
            deliver_pending(self.store,self.summary(revision,age=age),**self.kw)
            self.assertEqual(self.store.notice_state("owner","dot","reply:reply-"+str(revision)),"expired")
        value=self.summary(7);value["replies"][0]["created_at"]="invalid"
        deliver_pending(self.store,value,**self.kw)
        self.sender.assert_not_called()
    def test_seen_cursor_is_monotonic_scoped_and_does_not_clear_new_reply(self):
        self.assertEqual(self.store.seen("owner","dot"),0)
        self.assertEqual(self.store.mark_read("owner","dot",3,latest=5),3)
        self.assertLess(self.store.seen("owner","dot"),5)
        self.assertEqual(self.store.mark_read("owner","dot",0,latest=5),3)
        self.assertEqual(self.store.seen("other","dot"),0)
        for value in [True,-1,6,"3"]:
            with self.assertRaises(DotMailboxError):self.store.mark_read("owner","dot",value,latest=5)
    def test_changed_parent_binding_is_denied_and_database_is_private(self):
        self.store.arm("owner","dot","parent",0)
        with self.assertRaises(DotMailboxDenied):self.store.arm("owner","dot","other",0)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_link_uses_existing_https_origin_only(self):
        self.assertEqual(notification_link("https://example.invalid:8443/", "https://example.invalid:8443/settings/dot"),"https://example.invalid:8443/settings/dot")
        for value in ["http://example.invalid","https://user:pass@example.invalid","https://example.invalid/other","https://example.invalid?q=secret"]:
            with self.assertRaises(ValueError):notification_link(value,"https://example.invalid/settings/dot")
        for value in ["https://other.invalid/settings/dot", "https://example.invalid/files", "https://example.invalid/settings/dot?token=x"]:
            with self.assertRaises(ValueError):notification_link("https://example.invalid",value)
        self.assertEqual(notification_link("https://example.invalid:8443"),"https://example.invalid:8443/settings/dot")
        link="https://example.invalid/"+"a"*40+"/settings/dot"
        self.assertEqual(notification_link("https://example.invalid",link),link)

    def test_approved_test_notice_is_deduped_without_reply_or_read_mutations(self):
        from contextlib import ExitStack
        from engine.dot_notifications import send_approved_test_notice,APPROVED_REPLY_CONVERSATION_ID
        from shared import config
        from routes import dot_conversations
        service=mock.Mock()
        service.existing.return_value={"id":APPROVED_REPLY_CONVERSATION_ID}
        with ExitStack() as stack:
            for name,value in [("DOT_NTFY_ENABLED",True),("DOT_OWNER_ACCOUNT_ID","owner"),("DOT_PARENT_REF","parent"),("PASSKEY_ORIGIN","https://example.invalid"),("DOT_NTFY_LINK","https://example.invalid/settings/dot")]:
                stack.enter_context(mock.patch.object(config,name,value))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_available",return_value=True))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_service",return_value=service))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_delivery",return_value=self.store))
            probe=stack.enter_context(mock.patch("engine.dot_notifications.check_notification_link",return_value=(True,303)))
            sender=stack.enter_context(mock.patch("shared.security.notify_task_completion",return_value=(True,"sent")))
            self.assertEqual(send_approved_test_notice(),{"attempted":True,"state":"sent"})
            self.assertEqual(send_approved_test_notice(),{"attempted":False,"state":"sent"})
            sender.assert_called_once()
            probe.assert_called_once()
            service.reply.assert_not_called();service.send.assert_not_called()
            self.assertEqual(self.store.seen("owner",APPROVED_REPLY_CONVERSATION_ID),0)

    def test_link_probe_is_anonymous_bounded_and_requires_same_origin_login_redirect(self):
        import urllib.error
        from engine.dot_notifications import check_notification_link
        opener=mock.Mock()
        with mock.patch("urllib.request.build_opener",return_value=opener):
            for location,expected in [("/login",True),("https://example.invalid/login",True),("https://other.invalid/login",False),("/unexpected",False)]:
                opener.open.side_effect=urllib.error.HTTPError("https://example.invalid/settings/dot",303,"redirect",{"Location":location},None)
                self.assertEqual(check_notification_link("https://example.invalid/settings/dot"),(expected,303))
            request=opener.open.call_args.args[0]
            self.assertNotIn("Cookie",request.headers)
            self.assertNotIn("Authorization",request.headers)
            self.assertEqual(opener.open.call_args.kwargs["timeout"],10)
            opener.open.side_effect=TimeoutError()
            self.assertEqual(check_notification_link("https://example.invalid/settings/dot"),(False,0))

    def test_failed_runtime_link_prevents_background_and_test_notification(self):
        from contextlib import ExitStack
        from engine.dot_notifications import _tick,send_approved_test_notice,APPROVED_REPLY_CONVERSATION_ID
        from shared import config
        from routes import dot_conversations
        service=mock.Mock();service.existing.return_value={"id":APPROVED_REPLY_CONVERSATION_ID}
        service.mailbox.delivery_summary.return_value=self.summary(3)
        with ExitStack() as stack:
            for name,value in [("DOT_NTFY_ENABLED",True),("DOT_OWNER_ACCOUNT_ID","owner"),("DOT_PARENT_REF","parent"),("PASSKEY_ORIGIN","https://example.invalid"),("DOT_NTFY_LINK","")]:
                stack.enter_context(mock.patch.object(config,name,value))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_available",return_value=True))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_service",return_value=service))
            stack.enter_context(mock.patch.object(dot_conversations,"dot_delivery",return_value=self.store))
            stack.enter_context(mock.patch("engine.dot_notifications.check_notification_link",return_value=(False,403)))
            sender=stack.enter_context(mock.patch("shared.security.notify_task_completion"))
            _tick(arm_only=True)
            service.mailbox.delivery_summary.return_value=self.summary(5)
            _tick()
            self.assertEqual(send_approved_test_notice(),{"attempted":False,"state":"link_unverified","http_status":403})
            sender.assert_not_called()
            self.assertEqual(self.store.notice_state("owner",APPROVED_REPLY_CONVERSATION_ID,"link:canonical"),"unverified_403")
            service.reply.assert_not_called()
