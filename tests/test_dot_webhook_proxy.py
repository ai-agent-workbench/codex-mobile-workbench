"""Security boundaries for explicit local CONNECT; all network I/O synthetic."""
import io
import json
import os
import socket
import ssl
import unittest
from unittest import mock

from engine.dot_webhooks import (GooglePublicIPv4Resolver, RelayError, SecureWebhookPoster,
                                 _PinnedHTTPSConnection, explicit_loopback_proxy)


class ExplicitWebhookProxyTests(unittest.TestCase):
    def test_only_credential_free_literal_loopback_and_explicit_port(self):
        self.assertIsNone(explicit_loopback_proxy(None))
        self.assertIsNone(explicit_loopback_proxy(""))
        self.assertEqual(explicit_loopback_proxy("http://127.0.0.1:7897"),("127.0.0.1",7897))
        self.assertEqual(explicit_loopback_proxy("http://[::1]:7897/"),("::1",7897))
        invalid = ("http://127.0.0.1", "http://localhost:7897", "http://10.0.0.1:7897",
                   "http://proxy.example.test:7897", "https://127.0.0.1:7897",
                   "http://synthetic:password@127.0.0.1:7897", "http://127.0.0.1:7897/private",
                   "http://127.0.0.1:7897/?credential-like", "http://127.0.0.1:7897/#fragment",
                   "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:7897\r\n")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RelayError) as caught:
                explicit_loopback_proxy(value)
            self.assertNotIn(value,str(caught.exception))

    def test_environment_does_not_enable_proxy(self):
        with mock.patch.dict(os.environ,{"HTTPS_PROXY":"http://127.0.0.1:7897",
                                         "HTTP_PROXY":"http://127.0.0.1:7897"}):
            poster = SecureWebhookPoster({"callback.example.test"},enabled=True)
            self.assertIsNone(poster.proxy)
            self.assertIsNone(poster.connection_factory("callback.example.test","8.8.8.8").proxy)

    def connection(self, response=b"HTTP/1.1 200 Connection Established\r\n\r\n"):
        raw = mock.Mock()
        raw.makefile.return_value = io.BytesIO(response)
        context = ssl.create_default_context()
        wrap = mock.Mock(return_value=mock.Mock())
        with mock.patch("engine.dot_webhooks.ssl.create_default_context",return_value=context):
            connection = _PinnedHTTPSConnection("callback.example.test","8.8.8.8",proxy=("127.0.0.1",7897))
        return raw,context,wrap,connection

    def test_connect_pins_public_ip_and_tls_authenticates_original_hostname(self):
        raw,context,wrap,connection = self.connection()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        with mock.patch("engine.dot_webhooks.socket.create_connection",return_value=raw) as create, \
                mock.patch.object(context,"wrap_socket",wrap):
            connection.connect()
        create.assert_called_once_with(("127.0.0.1",7897),timeout=10)
        self.assertTrue(raw.sendall.call_args.args[0].startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        wrap.assert_called_once_with(raw,server_hostname="callback.example.test")
        connection.close()

    def test_connect_redirect_is_rejected_before_tls_or_application_data(self):
        raw,context,wrap,connection = self.connection(
            b"HTTP/1.1 302 Found\r\nLocation: http://private.example.test/\r\n\r\n")
        with mock.patch("engine.dot_webhooks.socket.create_connection",return_value=raw), \
                mock.patch.object(context,"wrap_socket",wrap), self.assertRaises(OSError):
            connection.connect()
        wrap.assert_not_called()
        raw.close.assert_called()
        self.assertIsNone(connection.sock)

    def test_tls_error_closes_proxy_socket(self):
        raw,context,wrap,connection = self.connection()
        wrap.side_effect = ssl.SSLCertVerificationError("synthetic untrusted certificate")
        with mock.patch("engine.dot_webhooks.socket.create_connection",return_value=raw), \
                mock.patch.object(context,"wrap_socket",wrap), self.assertRaises(ssl.SSLCertVerificationError):
            connection.connect()
        raw.close.assert_called()
        self.assertIsNone(connection.sock)

    def test_proxy_does_not_bypass_callback_or_public_dns_validation(self):
        for destination in ("127.0.0.1", "10.0.0.1", "::1", "::ffff:127.0.0.1"):
            resolver = mock.Mock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",(destination,443))])
            factory = mock.Mock()
            poster = SecureWebhookPoster({"callback.example.test"},enabled=True,resolver=resolver,
                                         connection_factory=factory,proxy="http://127.0.0.1:7897")
            with self.assertRaises(RelayError):
                poster.post("https://callback.example.test/",b"{}",{})
            factory.assert_not_called()
            resolver.reset_mock()
            with self.assertRaises(RelayError):
                poster.post("https://unapproved.example.test/",b"{}",{})
            resolver.assert_not_called()

    def test_proxy_queries_ipv4_only_and_pins_all_public_answers(self):
        resolver = mock.Mock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",443))])
        response = mock.Mock(status=204)
        response.read.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        factory = mock.Mock(return_value=connection)
        poster = SecureWebhookPoster({"callback.example.test"},enabled=True,resolver=resolver,
                                     connection_factory=factory,proxy="http://127.0.0.1:7897")
        self.assertEqual(poster.post("https://callback.example.test/",b"{}",{}).status,204)
        resolver.assert_called_once_with("callback.example.test",443,type=socket.SOCK_STREAM,family=socket.AF_INET)
        factory.assert_called_once_with("callback.example.test","8.8.8.8")
        # A mixed public/private IPv4 response is rejected in full.
        resolver.return_value.append((socket.AF_INET,socket.SOCK_STREAM,6,"",("192.168.1.1",443)))
        factory.reset_mock()
        with self.assertRaises(RelayError):
            poster.post("https://callback.example.test/",b"{}",{})
        factory.assert_not_called()
        # IPv6 cannot enter the CONNECT path, even if a resolver ignores family.
        resolver.return_value = [(socket.AF_INET6,socket.SOCK_STREAM,6,"",("2606:4700:4700::1111",443))]
        with self.assertRaises(RelayError):
            poster.post("https://callback.example.test/",b"{}",{})
        factory.assert_not_called()


class ExplicitPublicDnsTests(unittest.TestCase):
    def payload(self, addresses=("8.8.4.4",)):
        return {"Status":0,"TC":False,"CD":False,
                "Question":[{"name":"callback.example.test.","type":1}],
                "Answer":[{"type":1,"data":address} for address in addresses]}

    def resolver(self, payload, status=200, body=None):
        response = mock.Mock(status=status)
        response.read.return_value = json.dumps(payload).encode() if body is None else body
        connection = mock.Mock()
        connection.getresponse.return_value = response
        resolver = GooglePublicIPv4Resolver(("127.0.0.1",7897),connection_factory=lambda:connection)
        return resolver,connection,response

    def resolve(self, resolver):
        return resolver("callback.example.test",443,type=socket.SOCK_STREAM,family=socket.AF_INET)

    def test_fixed_resolver_address_and_authenticated_name(self):
        resolver = GooglePublicIPv4Resolver(("127.0.0.1",7897))
        connection = resolver.connection_factory()
        self.assertEqual((connection.host,connection.address,connection.proxy),
                         ("dns.google","8.8.8.8",("127.0.0.1",7897)))
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode,ssl.CERT_REQUIRED)
        connection.close()

    def test_bounded_question_and_public_answers_without_sensitive_data(self):
        resolver,connection,response = self.resolver(self.payload(("8.8.4.4","8.8.8.8")))
        result = self.resolve(resolver)
        self.assertEqual([row[4][0] for row in result],["8.8.4.4","8.8.8.8"])
        method,path = connection.request.call_args.args
        self.assertEqual(method,"GET")
        self.assertIn("name=callback.example.test",path)
        self.assertIn("cd=0",path)
        self.assertIn("edns_client_subnet=0.0.0.0%2F0",path)
        response.read.assert_called_once_with(16385)
        connection.close.assert_called_once()

    def test_redirects_errors_oversize_and_wrong_question_fail_closed(self):
        wrong = self.payload()
        wrong["Question"] = [{"name":"different.example.test.","type":1}]
        cases = [(self.payload(),302,None),(self.payload(),200,b"x"*16385),
                 (wrong,200,None),({"Status":2},200,None),(self.payload(()),200,None),
                 (self.payload(),200,b"invalid-json")]
        for payload,status,body in cases:
            resolver,connection,_ = self.resolver(payload,status,body)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.resolve(resolver)
            connection.close.assert_called_once()

    def test_private_mixed_and_ipv6_answers_are_rejected_in_full(self):
        for addresses in (("127.0.0.1",),("8.8.4.4","192.168.1.1"),
                          ("::1",),("2606:4700:4700::1111",)):
            resolver,connection,_ = self.resolver(self.payload(addresses))
            with self.subTest(addresses=addresses), self.assertRaises(RelayError):
                self.resolve(resolver)
            connection.close.assert_called_once()

    def test_explicit_selection_requires_proxy_and_allowlist_before_dns(self):
        for options in ({"public_dns":"google"},{"public_dns":"other","proxy":"http://127.0.0.1:7897"}):
            with self.assertRaises(RelayError):
                SecureWebhookPoster({"callback.example.test"},**options)
        resolver = mock.Mock()
        poster = SecureWebhookPoster({"callback.example.test"},enabled=True,proxy="http://127.0.0.1:7897",
                                     public_dns="google",resolver=resolver)
        with self.assertRaises(RelayError):
            poster.post("https://unapproved.example.test/",b"{}",{})
        resolver.assert_not_called()


if __name__=="__main__":
    unittest.main()
