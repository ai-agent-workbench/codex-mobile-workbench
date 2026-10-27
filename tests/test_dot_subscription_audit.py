"""Audit output stays independent of private RPC data and stdout framing."""
import io
import json
import unittest

from engine.dot_mcp import audit_subscription, serve_stdio


class SubscriptionAuditTests(unittest.TestCase):
    def record(self, request, response):
        stream = io.StringIO()
        audit_subscription(request,response,stream)
        raw = stream.getvalue()
        self.assertTrue(raw.startswith("DOT_SUBSCRIBE_AUDIT "))
        record = json.loads(raw.split(" ",1)[1])
        base = {"timestamp","error_code","reason","hostname"}
        self.assertTrue(base<=set(record)<=base|{"verification"})
        self.assertTrue(record["timestamp"].endswith("Z"))
        return record,raw

    def test_hostname_denial_omits_request_ids_urls_signatures_and_body(self):
        request = {"method":"events/subscribe","id":"PRIVATE_RPC_ID",
                   "params":{"delivery":{"url":"https://callback.example.test/PRIVATE_PATH?PRIVATE_QUERY",
                                          "secret":"PRIVATE_SIGNING_SECRET"},"body":"PRIVATE_BODY"}}
        response = {"error":{"code":-32602,"message":"PRIVATE_ERROR_TEXT",
                              "data":{"reason":"callback_host_not_approved","hostname":"callback.example.test",
                                      "signature":"PRIVATE_SIGNATURE"}}}
        record,raw = self.record(request,response)
        self.assertEqual(record["error_code"],-32602)
        self.assertEqual(record["reason"],"callback_host_not_approved")
        self.assertEqual(record["hostname"],"callback.example.test")
        self.assertNotIn("PRIVATE_",raw)
        self.assertNotIn("https://",raw)

    def test_untrusted_error_fields_and_invalid_hostnames_are_not_copied(self):
        for value in ("https://host.test/PRIVATE_PATH", "host.test?PRIVATE_QUERY", "host.test\nPRIVATE_BODY", "-bad.test"):
            record,raw = self.record({"method":"events/subscribe"},
                {"error":{"code":-32602,"message":"PRIVATE_ERROR",
                          "data":{"reason":"callback_host_not_approved","hostname":value}}})
            self.assertIsNone(record["hostname"])
            self.assertNotIn("PRIVATE_",raw)
        record,raw = self.record({"method":"events/subscribe"},
            {"error":{"code":-32603,"data":{"reason":"PRIVATE_REASON","hostname":"PRIVATE_HOST"}}})
        self.assertEqual(record["reason"],"relay_unavailable")
        self.assertIsNone(record["hostname"])
        self.assertNotIn("PRIVATE_",raw)

    def test_success_notifications_and_outbound_disabled_have_fixed_metadata(self):
        request = {"method":"events/subscribe","params":{"secret":"PRIVATE_SECRET"}}
        for response,reason,code in ((None,"notification_ignored",None),
                                     ({"result":{"secret":"PRIVATE_RESULT"}},"accepted",None),
                                     ({"error":{"code":-32014,"data":{"feature":"outbound"}}},"outbound_disabled",-32014)):
            record,raw = self.record(request,response)
            self.assertEqual((record["reason"],record["error_code"]),(reason,code))
            self.assertIsNone(record["hostname"])
            self.assertNotIn("PRIVATE_",raw)

    def test_wire_responses_remain_stdout_only_and_read_calls_are_not_logged(self):
        class Server:
            def process(self, request, principal):
                return {"jsonrpc":"2.0","id":request["id"],"error":{"code":-32602}}
        requests = [{"jsonrpc":"2.0","id":1,"method":"tools/list"},
                    {"jsonrpc":"2.0","id":2,"method":"events/subscribe","params":{"secret":"PRIVATE_SECRET"}}]
        incoming = io.BytesIO(b"".join(json.dumps(r).encode()+b"\n" for r in requests))
        outgoing,audit = io.BytesIO(),io.StringIO()
        serve_stdio(Server(),"synthetic-principal",incoming,outgoing,audit_stream=audit)
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies],[1,2])
        self.assertEqual(len(audit.getvalue().splitlines()),1)
        self.assertNotIn(b"DOT_SUBSCRIBE_AUDIT",outgoing.getvalue())
        self.assertNotIn("PRIVATE_",audit.getvalue())

    def test_audit_io_failure_does_not_change_protocol_response(self):
        class BrokenStream:
            def write(self, value):
                raise OSError("synthetic unavailable sink")
        class Server:
            def process(self, request, principal):
                return {"jsonrpc":"2.0","id":1,"error":{"code":-32602}}
        outgoing = io.BytesIO()
        serve_stdio(Server(),"synthetic-principal",io.BytesIO(b'{"method":"events/subscribe"}\n'),
                    outgoing,audit_stream=BrokenStream())
        self.assertEqual(json.loads(outgoing.getvalue())["error"]["code"],-32602)

    def test_verification_audit_copies_only_typed_fixed_classifications(self):
        metadata = {"http_status":401,"content_type":"application/json","response_is_json":True,
                    "response_is_object":True,"challenge_field_present":False,
                    "challenge_field_is_string":False,"challenge_matches":False,
                    "within_deadline":True,"response_oversized":False,"result":"http_status_rejected",
                    "body":"PRIVATE_BODY","challenge":"PRIVATE_CHALLENGE","url":"PRIVATE_URL",
                    "token":"PRIVATE_TOKEN","signature":"PRIVATE_SIGNATURE"}
        response = {"error":{"code":-32015,"data":{"reason":"challenge_failed","verification":metadata}}}
        record,raw = self.record({"method":"events/subscribe"},response)
        self.assertEqual(set(record['verification']),set(metadata)-{'body','challenge','url','token','signature'})
        self.assertNotIn("PRIVATE_",raw)
        metadata.update(http_status="PRIVATE_STATUS",content_type="PRIVATE_CONTENT_TYPE",
                        response_is_json="PRIVATE_BOOLEAN",result="PRIVATE_RESULT")
        record,raw = self.record({"method":"events/subscribe"},response)
        for key in ('http_status','content_type','response_is_json','result'):
            self.assertNotIn(key,record['verification'])
        self.assertNotIn("PRIVATE_",raw)


if __name__ == "__main__":
    unittest.main()
