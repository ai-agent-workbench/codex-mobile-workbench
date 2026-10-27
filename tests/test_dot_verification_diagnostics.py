"""Classify real protocol failures using synthetic receivers; no real callback."""
import json
import unittest
from unittest import mock
from tests import test_dot_mcp_events as fixtures
from engine.dot_webhooks import WebhookResponse, json_bytes, safe_content_type


class CallbackVerificationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DotMcpEventsTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def failure(self, response):
        with mock.patch.object(self.case.receiver,'post',return_value=response):
            result = self.case.request('events/subscribe',self.case.subscription())
        self.assertEqual(result['error']['code'],-32015)
        self.assertFalse(self.case.events_path.exists())
        encoded = json.dumps(result)
        self.assertNotIn('PRIVATE_',encoded)
        self.assertNotIn(fixtures.TEST_SECRET,encoded)
        return result['error']['data']['verification']

    def test_status_json_structure_missing_wrong_and_oversized_are_distinct(self):
        cases = [
            (WebhookResponse(401,json_bytes({'error':'PRIVATE_RESPONSE'}),'application/json'),
             'http_status_rejected',True,False),
            (WebhookResponse(302,b'PRIVATE_REDIRECT_BODY','text/html'),'http_status_rejected',False,False),
            (WebhookResponse(204,b'','missing'),'response_not_json',False,False),
            (WebhookResponse(200,b'PRIVATE_PLAIN_TEXT','text/plain'),'response_not_json',False,False),
            (WebhookResponse(200,json_bytes([]),'application/json'),'response_not_object',True,False),
            (WebhookResponse(200,json_bytes({}),'application/json'),'challenge_missing',True,False),
            (WebhookResponse(200,json_bytes({'challenge':123}),'application/json'),'challenge_not_string',True,True),
            (WebhookResponse(200,json_bytes({'challenge':'PRIVATE_WRONG_NONCE'}),'application/json'),
             'challenge_mismatch',True,True),
            (WebhookResponse(200,b'','application/json',True),'response_oversized',False,False)]
        for response,reason,is_json,present in cases:
            with self.subTest(reason=reason):
                detail = self.failure(response)
                self.assertEqual(detail['result'],reason)
                self.assertEqual(detail['http_status'],response.status)
                self.assertEqual(detail['content_type'],response.content_type)
                self.assertEqual(detail['response_is_json'],is_json)
                self.assertEqual(detail['challenge_field_present'],present)
                self.assertFalse(detail['challenge_matches'])

    def test_valid_echo_after_deadline_still_fails_and_reports_matching_boolean(self):
        def delayed(event):
            self.case.clock.advance(11)
        self.case.receiver.hook = delayed
        result = self.case.request('events/subscribe',self.case.subscription())
        detail = result['error']['data']['verification']
        self.assertTrue(detail['challenge_matches'])
        self.assertFalse(detail['within_deadline'])
        self.assertEqual(detail['result'],'verification_deadline_exceeded')
        self.assertFalse(self.case.events_path.exists())

    def test_signed_control_envelope_and_echo_keep_official_contract(self):
        result = self.case.subscribe()
        _,body,headers = self.case.receiver.posts[0]
        envelope = json.loads(body)
        self.assertEqual(set(envelope),{'type','challenge'})
        self.assertEqual(envelope['type'],'verification')
        self.assertEqual(headers['Content-Type'],'application/json')
        self.assertTrue(headers['webhook-id'].startswith('msg_verification_'))
        self.assertTrue(headers['webhook-timestamp'].isdigit())
        self.assertTrue(headers['webhook-signature'].startswith('v1,'))
        self.assertEqual(headers['X-MCP-Subscription-Id'],result['id'])
        # SyntheticReceiver independently verified HMAC over these exact bytes.
        self.assertNotIn(envelope['challenge'],json.dumps(result))

    def test_content_type_classification_never_returns_unknown_or_parameter_values(self):
        for value,expected in ((None,'missing'),('application/json; PRIVATE_CHARSET','application/json'),
                               ('text/html; PRIVATE_TOKEN','text/html'),
                               ('application/PRIVATE_SECRET','other'),('PRIVATE_HEADER\r\n','invalid')):
            self.assertEqual(safe_content_type(value),expected)


if __name__=='__main__':
    unittest.main()
