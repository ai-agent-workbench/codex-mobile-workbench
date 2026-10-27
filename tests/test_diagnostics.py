import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
import routes.diagnostics as intake
import shared.diagnostics as diagnostics
from shared.diagnostics import DiagnosticsMiddleware, DiagnosticsWriter, redact

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self._writer, self._configured = diagnostics._WRITER, diagnostics._CONFIGURED
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, diagnostics, "_WRITER", self._writer)
        self.addCleanup(setattr, diagnostics, "_CONFIGURED", self._configured)
    def test_masking_rotation_and_write_failure(self):
        self.assertEqual(redact({'request_id':'a'*32})['request_id'], 'a'*32)
        self.assertEqual(redact({'cookie':'bad'})['cookie'], '[REDACTED]')
        writer = DiagnosticsWriter(self.path, normal_max_bytes=70, normal_backups=2)
        self.assertTrue(writer.write({'a':'x'*60})); self.assertTrue(writer.write({'a':'y'*60}))
        self.assertTrue((self.path/'events.jsonl.1').exists())
        with patch('shared.diagnostics.os.open', side_effect=OSError('denied')): self.assertFalse(writer.write({'a':1}))

    def test_local_evidence_keeps_network_metadata_not_credentials(self):
        value=redact({'url':'https://example.test/files/'+'a'*64+'?page=3&sort=time',
                      'client_ip':'192.0.2.1','tls_session_reused':'r','hex':'abcd',
                      'accessToken':'do-not-keep','session_id':'do-not-keep'})
        self.assertEqual(value['client_ip'],'192.0.2.1')
        self.assertEqual(value['tls_session_reused'],'r')
        self.assertIn('a'*64+'?page=3&sort=time',value['url'])
        self.assertNotIn('do-not-keep',json.dumps(value))
        self.assertNotIn('private-code',redact('code=private-code&sort=time'))
        self.assertNotIn('a'*40,redact('https://example.test/'+'a'*40+'/home?page=3'))
        for url in ['wss://example.test/'+'a'*40+'/ws','/login?pwa=manifest&entry=%2F'+'s'*32,
                    '/login?%74oken=DO_NOT_PERSIST','/login?%2574oken=DO_NOT_PERSIST',
                    '/login?next=%2F'+'s'*32+'%2Fhome',
                    '/login?next=%2Flogin%3Ftoken%3DDO_NOT_PERSIST',
                    '/login?next=https%3A%2F%2Fuser%3ADO_NOT_PERSIST%40example.test',
                    '/login?next=https%3A%2F%2Fuser%40name%3ADO_NOT_PERSIST%40example.test',
                    '/login?next=%25252F'+'s'*32,
                    '/login?next=%2F'+'%73'*32]:
            clean=redact(url)
            self.assertNotIn('DO_NOT_PERSIST',clean)
            self.assertNotIn('s'*32,clean)
            self.assertNotIn('a'*40,clean)
        self.assertNotIn('DO_NOT_PERSIST',redact('wss://user@name:DO_NOT_PERSIST@example.test/ws'))
    def test_http_stream_and_exception_correlation(self):
        async def run():
            diagnostics._WRITER = DiagnosticsWriter(self.path)
            out=[]
            async def good(scope, receive, send):
                await send({'type':'http.response.start','status':201,'headers':[]})
                await send({'type':'http.response.body','body':b'a','more_body':True})
                await send({'type':'http.response.body','body':b'bc'})
            async def receive(): return {'type':'http.request','body':b''}
            async def send(message): out.append(message)
            await DiagnosticsMiddleware(good)({'type':'http','path':'/x','method':'GET','headers':[(b'x-request-id',b'client-id-123'),(b'x-diagnostics-page-id',b'page-id-123'),(b'x-diagnostics-edge-id',b'edge-id-123'),(b'x-diagnostics-wsl-id',b'wsl-id-123'),(b'x-diagnostics-operation-id',b'operation-id-123')]},receive,send)
            self.assertNotEqual(dict(out[0]['headers'])[b'x-request-id'], b'client-id-123')
            record=json.loads((self.path/'events.jsonl').read_text().splitlines()[-1])
            self.assertEqual(record['response_bytes'],3)
            self.assertTrue(record['response_complete'])
            self.assertEqual(record['edge_id'],'edge-id-123')
            self.assertEqual(record['operation_id'],'operation-id-123')
            self.assertEqual(dict(out[0]['headers'])[b'x-diagnostics-wsl-id'],b'wsl-id-123')
            async def bad(scope,receive,send): raise ValueError('token=not-safe')
            with self.assertRaises(ValueError): await DiagnosticsMiddleware(bad)({'type':'http','path':'/bad','method':'GET','headers':[]},receive,send)
        asyncio.run(run())
        self.assertNotIn('not-safe',(self.path/'errors.jsonl').read_text())
    def test_intake_auth_schema_and_ack_after_write(self):
        async def run():
            async def receive(): return {'type':'http.request','body':json.dumps({'events':[event]}).encode(),'more_body':False}
            scope={'type':'http','method':'POST','path':'/settings/diagnostics/client-events','headers':[(b'host',b'testserver'),(b'origin',b'https://testserver'),(b'content-type',b'application/json')]}
            with patch.object(intake,'logged_in',lambda request:True), patch.object(diagnostics,'_WRITER',DiagnosticsWriter(self.path)):
                response=await intake.ingest(Request(scope,receive)); self.assertEqual(response.status_code,200)
                with patch.object(diagnostics.get_writer(),'write',return_value=False):
                    event['event_id']='event-trace-456'; response=await intake.ingest(Request(scope,receive)); self.assertEqual(response.status_code,503)
        event={'event_id':'event-trace-123','ts':'2026-01-01T00:00:00.000Z','type':'js_error','page_id':'page-trace-123','action_id':'action-trace-123','tab_id':'tab-trace-123','seq':1,'data':{'message':'password=bad'}}
        asyncio.run(run()); self.assertNotIn('bad',(self.path/'errors.jsonl').read_text())

    def test_v1_intake_ignores_private_queue_byte_cache(self):
        async def run():
            event={'event_id':'event-trace-dq-123','ts':'2026-01-01T00:00:00.000Z','type':'click','page_id':'page-trace-dq-123','action_id':'action-trace-dq-123','tab_id':'tab-trace-dq-123','seq':1,'data':{'id':'old-reader-fixture'},'_dq':{'bytes':321}}
            async def receive(): return {'type':'http.request','body':json.dumps({'events':[event]}).encode(),'more_body':False}
            scope={'type':'http','method':'POST','path':'/settings/diagnostics/client-events','headers':[(b'host',b'testserver'),(b'origin',b'https://testserver'),(b'content-type',b'application/json')]}
            with patch.object(intake,'logged_in',lambda request:True), patch.object(diagnostics,'_WRITER',DiagnosticsWriter(self.path)):
                response=await intake.ingest(Request(scope,receive)); self.assertEqual(response.status_code,200)
            stored=json.loads((self.path/'events.jsonl').read_text().splitlines()[-1])
            self.assertEqual(stored['data']['id'],'old-reader-fixture')
            self.assertNotIn('_dq',stored)
        asyncio.run(run())
    def test_websocket_disconnect_and_logging_stack(self):
        async def run():
            diagnostics._WRITER = DiagnosticsWriter(self.path)
            async def app(scope, receive, send):
                await receive(); await send({'type':'websocket.close','code':1006})
            async def receive(): return {'type':'websocket.disconnect','code':1001}
            async def send(message): pass
            await DiagnosticsMiddleware(app)({'type':'websocket','path':'/ws','headers':[]},receive,send)
        asyncio.run(run())
        events=[json.loads(line) for line in (self.path/'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['close_code'],1006)
        self.assertEqual(events[-1]['disconnect_code'],1001)
        self.assertEqual(events[-1]['close_sent_code'],1006)
        self.assertLessEqual(events[-1]['disconnect_ms'],events[-1]['close_sent_ms'])
        self.assertIn('ws_disconnect_received',[e['kind'] for e in events])
        diagnostics._WRITER = DiagnosticsWriter(self.path)
        handler = diagnostics._DiagnosticsLogHandler(); logger = __import__('logging').getLogger('test.diagnostic.stack'); logger.addHandler(handler)
        try:
            try: raise RuntimeError('token=secret')
            except RuntimeError: logger.exception('failed')
        finally: logger.removeHandler(handler)
        self.assertIn('RuntimeError', (self.path/'errors.jsonl').read_text())
        self.assertNotIn('secret', (self.path/'errors.jsonl').read_text())
    def test_intake_rejects_unauthenticated_cross_origin_and_oversize(self):
        async def call(headers, body=b'{}'):
            async def receive(): return {'type':'http.request','body':body,'more_body':False}
            return await intake.ingest(Request({'type':'http','method':'POST','path':'/settings/diagnostics/client-events','headers':headers},receive))
        with patch.object(intake,'logged_in',lambda request:False):
            self.assertEqual(asyncio.run(call([])).status_code,401)
        with patch.object(intake,'logged_in',lambda request:True):
            self.assertEqual(asyncio.run(call([(b'host',b'testserver'),(b'origin',b'https://evil.test')])).status_code,403)
            self.assertEqual(asyncio.run(call([(b'host',b'testserver'),(b'origin',b'https://testserver'),(b'content-length',b'2000000')])).status_code,413)

    def test_framework_500_keeps_correlation_without_exposing_stack(self):
        from fastapi import FastAPI
        from app import diagnostic_server_error
        app = FastAPI()
        app.add_middleware(DiagnosticsMiddleware)
        app.add_exception_handler(Exception, diagnostic_server_error)
        @app.get("/bad")
        def bad(): raise RuntimeError("private failure marker")
        diagnostics._WRITER = DiagnosticsWriter(self.path)
        async def exercise():
            messages=[]
            async def receive(): return {"type":"http.request", "body":b"", "more_body":False}
            async def send(message): messages.append(message)
            scope={"type":"http", "asgi":{"version":"3.0"}, "scheme":"http", "method":"GET", "path":"/bad", "raw_path":b"/bad", "query_string":b"", "headers":[], "server":("fixture",80), "client":("127.0.0.1",1), "root_path":""}
            with self.assertRaises(RuntimeError): await app(scope,receive,send)
            return messages
        messages=asyncio.run(exercise())
        self.assertEqual(messages[0]["status"], 500)
        self.assertNotIn(b"private failure marker", messages[-1]["body"])
        request_id = dict(messages[0]["headers"])[b"x-request-id"].decode()
        self.assertEqual(len(request_id), 32)
        self.assertIn(request_id, (self.path/"errors.jsonl").read_text())

    def test_pending_release_is_recorded_at_startup(self):
        (self.path/"releases").mkdir()
        (self.path/"releases"/"pending-publish").write_text("20260930-120000\n20260930-110000\n")
        with patch.object(diagnostics, "STATE", self.path), patch.object(diagnostics, "_RELEASE", "unknown"), \
             patch.object(diagnostics, "_CONFIGURED", False), \
             patch.dict(os.environ, {"CODEX_WEB_DIAGNOSTICS_DIR":str(self.path/"logs"), "CODEX_WEB_RELEASE":""}), \
             patch.object(__import__('logging').getLogger(), 'addHandler'):
            diagnostics.configure_diagnostics()
            event=json.loads((self.path/"logs"/"events.jsonl").read_text().splitlines()[0])
            self.assertEqual(event['release'], '20260930-120000')
            self.assertEqual((self.path/"logs"/"events.jsonl").stat().st_mode & 0o777, 0o600)

if __name__ == '__main__': unittest.main()
