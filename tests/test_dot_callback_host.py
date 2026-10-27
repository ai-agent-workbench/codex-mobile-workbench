"""Capture actual HTTP bytes with memory sockets; never connect or subscribe."""
import io
import socket
import ssl
import unittest
from unittest import mock
from tests import test_dot_mcp_events as fixtures
from engine.dot_webhooks import (RelayError, SecureWebhookPoster, _PinnedHTTPSConnection,
                                 json_bytes, webhook_headers)


class MemorySocket:
    def __init__(self):
        self.parts = []

    def sendall(self, data):
        self.parts.append(data)

    def makefile(self, mode):
        return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                          b'Content-Length: 2\r\n\r\n{}')

    def close(self):
        pass


class CallbackHostWireTests(unittest.TestCase):
    def capture(self, url=fixtures.CALLBACK, *, host='callback.example.test',
                address='8.8.8.8', proxy=False, extra_headers=None):
        endpoint = ('127.0.0.1',7897) if proxy else None
        captured = []
        def connection_factory(original_host, pinned_address):
            connection = _PinnedHTTPSConnection(original_host,pinned_address,proxy=endpoint)
            memory = MemorySocket()
            connection.sock = memory
            captured.append((connection,memory))
            return connection
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        resolver = mock.Mock(return_value=[(family,socket.SOCK_STREAM,6,'',(address,443))])
        poster = SecureWebhookPoster({host},enabled=True,resolver=resolver,
                                     connection_factory=connection_factory,
                                     proxy='http://127.0.0.1:7897' if proxy else None)
        body = json_bytes({'type':'verification','challenge':'synthetic-wire-nonce'})
        headers = webhook_headers('sub_synthetic','msg_verification_synthetic',body,
                                  fixtures.TEST_SECRET,1_790_000_000)
        headers.update(extra_headers or {})
        original_headers = dict(headers)
        with mock.patch('engine.dot_webhooks.socket.create_connection',side_effect=AssertionError('no network')):
            response = poster.post(url,body,headers)
        self.assertEqual(response.status,200)
        self.assertEqual(headers,original_headers)  # Caller/signature material stays unchanged.
        connection,memory = captured[0]
        wire = b''.join(memory.parts)
        raw_headers,wire_body = wire.split(b'\r\n\r\n',1)
        lines = raw_headers.split(b'\r\n')
        pairs = [line.split(b':',1) for line in lines[1:]]
        hosts = [value.strip().decode('ascii') for name,value in pairs if name.lower()==b'host']
        self.assertEqual(len(hosts),1)
        self.assertEqual(wire_body,body)
        wire_headers = {name.decode('ascii'):value.strip().decode('ascii') for name,value in pairs}
        # Existing independent receiver checks HMAC over the transmitted bytes.
        self.assertEqual(fixtures.SyntheticReceiver().post(url,wire_body,wire_headers).status,200)
        self.assertEqual(connection.host,host)
        self.assertEqual(connection.address,address)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode,ssl.CERT_REQUIRED)
        if proxy:
            self.assertEqual(connection.proxy,endpoint)
            self.assertEqual(connection._tunnel_host,address)
        return hosts[0],lines[0],wire_body

    def test_direct_and_proxy_send_original_callback_host_and_same_signed_body(self):
        direct = self.capture()
        proxied = self.capture(proxy=True)
        self.assertEqual(direct,proxied)
        self.assertEqual(proxied[0],'callback.example.test')

    def test_explicit_443_is_preserved_in_direct_and_proxy_authority(self):
        for proxy in (False,True):
            with self.subTest(proxy=proxy):
                host,_,_ = self.capture('https://callback.example.test:443/verify',proxy=proxy)
                self.assertEqual(host,'callback.example.test:443')

    def test_input_host_variants_cannot_override_validated_authority(self):
        host,_,_ = self.capture(proxy=True,extra_headers={'host':'wrong.example.test',
                                                        'HOST':'second-wrong.example.test'})
        self.assertEqual(host,'callback.example.test')

    def test_path_and_query_are_preserved_without_changing_signed_body(self):
        _,request_line,_ = self.capture('https://callback.example.test/verify%2Froute?token=synthetic%2Bvalue',proxy=True)
        self.assertEqual(request_line,b'POST /verify%2Froute?token=synthetic%2Bvalue HTTP/1.1')

    def test_existing_direct_ipv6_transport_and_literal_authority(self):
        host,_,_ = self.capture(address='2606:4700:4700::1111')
        self.assertEqual(host,'callback.example.test')
        host,_,_ = self.capture('https://[2606:4700:4700::1111]:443/verify',
                              host='2606:4700:4700::1111',address='2606:4700:4700::1111')
        self.assertEqual(host,'[2606:4700:4700::1111]:443')

    def test_wrong_url_host_or_port_is_rejected_before_dns_or_transport(self):
        resolver,factory = mock.Mock(),mock.Mock()
        poster = SecureWebhookPoster({'callback.example.test'},enabled=True,resolver=resolver,
                                     connection_factory=factory,proxy='http://127.0.0.1:7897')
        for url in ('https://wrong.example.test/','https://callback.example.test:444/'):
            with self.subTest(url=url),self.assertRaises(RelayError):
                poster.post(url,b'{}',{'Host':'callback.example.test'})
        resolver.assert_not_called()
        factory.assert_not_called()


if __name__=='__main__':
    unittest.main()
