"""Real ASGI/router/SQLite and Jinja tests with clearly synthetic login identity."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from engine.dot_mailbox import DotConversationService,DotScope
from engine.dot_delivery import DotDeliveryStore
from routes import dot_conversations
from shared import config
from middleware.request_origin import RequestOriginMiddleware


class ASGIResponse:
    def __init__(self, messages):
        start=next(m for m in messages if m["type"]=="http.response.start")
        self.status_code=start["status"]
        self.headers={k.decode():v.decode() for k,v in start["headers"]}
        self.text=b"".join(m.get("body",b"") for m in messages if m["type"]=="http.response.body").decode("utf-8")

    def json(self):
        return json.loads(self.text)


class ASGIClient:
    """Calls the real ASGI app without network, browser credentials or httpx."""
    def __init__(self, app):
        self.app=app

    async def request(self,method,path,*,data=None,headers=None):
        body=urlencode(data or {}).encode()
        fields={"host":"dot-prototype.invalid","origin":"https://dot-prototype.invalid","content-type":"application/x-www-form-urlencoded",
                "content-length":str(len(body)),**(headers or {})}
        scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1",
               "method":method,"scheme":"https","path":path,"raw_path":path.encode(),
               "query_string":b"","root_path":"","headers":[(k.encode(),v.encode()) for k,v in fields.items()],
               "client":("127.0.0.1",12345),"server":("dot-prototype.invalid",443)}
        result=[]
        async def receive():
            return {"type":"http.request","body":body,"more_body":False}
        async def send(message):
            result.append(message)
        await self.app(scope,receive,send)
        return ASGIResponse(result)

    async def get(self,path,**values):
        return await self.request("GET",path,**values)

    async def post(self,path,**values):
        return await self.request("POST",path,**values)


class DotRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="dot-asgi-test-")
        self.path=Path(self.temporary.name)/"dot-mailbox.sqlite3"
        self.service=DotConversationService(self.path,enabled=True)
        self.delivery=DotDeliveryStore(Path(self.temporary.name)/"delivery.sqlite3")
        self.patchers=[
            mock.patch.object(dot_conversations,"DOT_CONVERSATION_ENABLED",True),
            mock.patch.object(dot_conversations,"DOT_OWNER_ACCOUNT_ID","synthetic-owner"),
            mock.patch.object(dot_conversations,"DOT_PARENT_REF","synthetic-dot"),
            mock.patch.object(dot_conversations,"dot_service",return_value=self.service),
            mock.patch.object(dot_conversations,"dot_delivery",return_value=self.delivery),
            mock.patch.object(dot_conversations,"require_auth",return_value=None),
            mock.patch.object(dot_conversations,"logged_in",side_effect=lambda request: request.headers.get("x-test-owner","synthetic-owner")),
        ]
        for patcher in self.patchers: patcher.start()
        app=FastAPI()
        app.include_router(dot_conversations.router)
        app.add_middleware(RequestOriginMiddleware,router=app.router)
        self.client=ASGIClient(app)

    async def asyncTearDown(self):
        self.service.close()
        self.delivery.close()
        for patcher in reversed(self.patchers): patcher.stop()
        self.temporary.cleanup()

    async def create(self):
        response=await self.client.post("/settings/dot/open")
        self.assertEqual(response.status_code,303,response.text)
        return response.headers["location"].rsplit("/",1)[1]

    async def test_create_send_parent_reply_page_snapshot_and_duplicate(self):
        with mock.patch("engine.codex_app_server.CodexAppServerClient",side_effect=AssertionError("native client")) as native:
            cid=await self.create()
            self.assertEqual(cid,await self.create())
            payload={"message_id":"synthetic-ui-message","text":"[合成 ASGI 用户输入] 不是手机实测","expected_revision":"1"}
            first=await self.client.post("/settings/dot/"+cid+"/messages",data=payload)
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.json()["conversation"]["revision"],2)
            self.assertEqual((await self.client.post("/settings/dot/"+cid+"/messages",data=payload)).json()["conversation"]["revision"],2)
            scope=DotScope("synthetic-owner",cid,"synthetic-dot",frozenset({"read","reply"}))
            body="[合成父回复，用于机制测试]\n原样保留换行\n"
            self.service.reply(scope,message_id="synthetic-ui-message",reply_id="synthetic-reply",text=body,expected_revision=2)
            snapshot=(await self.client.get("/settings/dot/"+cid+"/snapshot")).json()
            self.assertEqual(snapshot["messages"][0]["reply"]["text"],body)
            page=await self.client.get("/settings/dot/"+cid)
            self.assertEqual(page.status_code,200)
            self.assertIn('data-conversation-kind="dot"',page.text)
            self.assertIn("/static/dot-conversation.js",page.text)
            self.assertIn('href="/projects/history" aria-label="返回"',page.text)
            self.assertNotIn("/static/conversation.js",page.text)
            self.assertNotIn('id="conversation-model"',page.text)
            self.assertNotIn('id="stop-conversation"',page.text)
            self.assertNotIn('id="conversation-tier-toggle"',page.text)
            marker='<script id="conversation-initial-snapshot" type="application/json">'
            embedded=json.loads(page.text.split(marker,1)[1].split("</script>",1)[0])
            self.assertEqual(embedded,snapshot)
            native.assert_not_called()

    async def test_reply_unread_receipts_are_exact_visible_generation_and_owner_scoped(self):
        cid=await self.create()
        scope=DotScope("synthetic-owner",cid,"synthetic-dot",frozenset({"read","reply"}))
        self.service.send(cid,"synthetic-owner",message_id="one",text="secret synthetic input",expected_revision=1)
        self.service.reply(scope,message_id="one",reply_id="r1",text="secret synthetic reply",expected_revision=2)
        status=(await self.client.get("/settings/dot/status")).json()
        self.assertTrue(status["unread"]); self.assertEqual(status["reply_generation"],3)
        self.assertNotIn("secret",json.dumps(status))
        # Snapshot/page reads alone cannot clear unread (background prefetch).
        await self.client.get("/settings/dot/"+cid)
        self.assertTrue((await self.client.get("/settings/dot/status")).json()["unread"])
        self.service.send(cid,"synthetic-owner",message_id="two",text="input two",expected_revision=3)
        self.service.reply(scope,message_id="two",reply_id="r2",text="reply two",expected_revision=4)
        base="/settings/dot/"+cid+"/read"
        for headers in [{"x-test-owner":"foreign"},{"origin":"https://foreign.invalid"}]:
            self.assertEqual((await self.client.post(base,data={"generation":5},headers=headers)).status_code,403)
        self.assertEqual((await self.client.post(base,data={"generation":6})).status_code,400)
        self.assertEqual((await self.client.post(base,data={"generation":3})).status_code,200)
        self.assertTrue((await self.client.get("/settings/dot/status")).json()["unread"])
        self.assertEqual((await self.client.post(base,data={"generation":5})).status_code,200)
        self.assertFalse((await self.client.get("/settings/dot/status")).json()["unread"])
        self.service.send(cid,"synthetic-owner",message_id="three",text="pending",expected_revision=5)
        status=(await self.client.get("/settings/dot/status")).json()
        self.assertFalse(status["unread"]);self.assertTrue(status["waiting"])

    async def test_disabled_feature_and_wrong_owner_do_not_open_store(self):
        with mock.patch.object(dot_conversations,"DOT_CONVERSATION_ENABLED",False):
            response=await self.client.post("/settings/dot/open")
            self.assertEqual(response.status_code,503)
            self.assertFalse(self.path.exists())
        response=await self.client.post("/settings/dot/open",headers={"x-test-owner":"foreign-owner"})
        self.assertEqual(response.status_code,403)
        self.assertFalse(self.path.exists())

    async def test_foreign_or_changed_parent_scope_never_leaks_messages(self):
        cid=await self.create()
        self.assertEqual((await self.client.get("/settings/dot/"+cid+"/snapshot",headers={"x-test-owner":"foreign-owner"})).status_code,403)
        with mock.patch.object(dot_conversations,"DOT_PARENT_REF","another-dot"):
            self.assertEqual((await self.client.get("/settings/dot/"+cid+"/snapshot")).status_code,403)
            result=await self.client.post("/settings/dot/"+cid+"/messages",data={"message_id":"no-write","text":"[合成越界]","expected_revision":1})
            self.assertEqual(result.status_code,403)
        self.assertEqual((await self.client.get("/settings/dot/"+cid+"/snapshot")).json()["conversation"]["revision"],1)

    async def test_origin_and_revision_conflicts_retain_database_state(self):
        cid=await self.create()
        payload={"message_id":"one","text":"[合成消息]","expected_revision":1}
        for headers in ({"origin":"https://foreign.invalid"},{"origin":"null"},{"sec-fetch-site":"cross-site"}):
            response=await self.client.post("/settings/dot/"+cid+"/messages",data=payload,headers=headers)
            self.assertEqual(response.status_code,403)
        result=await self.client.post("/settings/dot/"+cid+"/messages",data=payload,headers={"origin":"https://dot-prototype.invalid"})
        self.assertEqual(result.status_code,200,result.text)
        payload["message_id"]="two"
        self.assertEqual((await self.client.post("/settings/dot/"+cid+"/messages",data=payload)).status_code,409)
        self.assertEqual(len((await self.client.get("/settings/dot/"+cid+"/snapshot")).json()["messages"]),1)

    async def test_browser_has_no_reply_endpoint_and_no_native_control_api(self):
        cid=await self.create()
        for action in ("reply","interrupt","compact","settings","start","reconcile"):
            self.assertEqual((await self.client.post("/settings/dot/"+cid+"/"+action)).status_code,404)

    async def test_api_login_redirect_becomes_json_401(self):
        with mock.patch.object(dot_conversations,"require_auth",return_value=RedirectResponse("/login",303)):
            response=await self.client.get("/settings/dot/not-created/snapshot")
            self.assertEqual(response.status_code,401)
            self.assertEqual(response.json()["error"]["code"],"authentication_required")
            self.assertFalse(self.path.exists())

    async def test_user_text_cannot_break_initial_snapshot_script(self):
        cid=await self.create()
        text='[合成 XSS 测试]</script><script>window.PWNED=1</script>'
        response=await self.client.post("/settings/dot/"+cid+"/messages",data={"message_id":"xss","text":text,"expected_revision":1})
        self.assertEqual(response.status_code,200)
        page=await self.client.get("/settings/dot/"+cid)
        self.assertNotIn('</script><script>window.PWNED',page.text)
        self.assertIn('\\u003c/script\\u003e',page.text)

    async def test_deployed_port_survives_gateway_host_rewrite(self):
        origin="https://dot-prototype.invalid:18443"
        with mock.patch.object(config,"PASSKEY_ORIGIN",origin):
            response=await self.client.post("/settings/dot/open",headers={"origin":origin})
            self.assertEqual(response.status_code,303,response.text)
            self.assertTrue(response.headers["location"].startswith("/settings/dot/"))
            cid=response.headers["location"].rsplit("/",1)[1]
            payload={"message_id":"gateway-port","text":"[合成网关改写]","expected_revision":1}
            for wrong in ("https://dot-prototype.invalid","https://dot-prototype.invalid:18444"):
                response=await self.client.post("/settings/dot/"+cid+"/messages",data=payload,headers={"origin":wrong})
                self.assertEqual(response.status_code,403)
            self.assertEqual((await self.client.get("/settings/dot/"+cid+"/snapshot")).json()["conversation"]["revision"],1)
            response=await self.client.post("/settings/dot/"+cid+"/messages",data=payload,headers={"origin":origin})
            self.assertEqual(response.status_code,200,response.text)

    async def test_privacy_native_form_opens_but_opaque_fetch_cannot_write(self):
        with mock.patch.object(config,"PASSKEY_ORIGIN","https://dot-prototype.invalid:18443"):
            headers={"origin":"null","sec-fetch-site":"same-origin","sec-fetch-mode":"navigate","sec-fetch-dest":"empty"}
            response=await self.client.post("/settings/dot/open",headers=headers)
            self.assertEqual(response.status_code,303,response.text)
            cid=response.headers["location"].rsplit("/",1)[1]
            payload={"message_id":"opaque-fetch","text":"[合成非导航请求]","expected_revision":1}
            headers.update({"sec-fetch-mode":"cors","content-type":"application/json"})
            response=await self.client.post("/settings/dot/"+cid+"/messages",data=payload,headers=headers)
            self.assertEqual(response.status_code,403)
            self.assertEqual((await self.client.get("/settings/dot/"+cid+"/snapshot")).json()["messages"],[])


if __name__ == "__main__":
    unittest.main()
