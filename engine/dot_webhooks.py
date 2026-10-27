"""Bounded, DNS-pinned HTTPS POSTs for MCP Events; no listener or credentials.

Contract: OpenAI MCP Events and Standard Webhooks, reviewed 2026-10-02.
Outbound access is an independent opt-in. Tests inject a synthetic transport.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlencode, urlsplit


MAX_BODY = 262_144
SAFE_CONTENT_TYPES = frozenset({"application/json","application/problem+json","text/plain",
                                "text/html","application/octet-stream","other","missing","invalid"})


def safe_content_type(value):
    if value is None or value=="":
        return "missing"
    if not isinstance(value,str) or len(value)>256 or any(ord(c)<32 for c in value):
        return "invalid"
    mime = value.partition(';')[0].strip().lower()
    return mime if mime in SAFE_CONTENT_TYPES else "other"


class RelayError(ValueError):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def json_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),
                      allow_nan=False).encode("utf-8")


def signing_key(secret):
    try:
        if not isinstance(secret,str) or not secret.startswith("whsec_") or len(secret) > 128:
            raise ValueError
        key = base64.b64decode(secret[6:],validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError
        return key
    except (ValueError,TypeError) as error:
        raise RelayError(-32602,"invalid webhook signing material") from error


def webhook_headers(subscription_id, webhook_id, body, secret, timestamp, previous_secret=None):
    if not isinstance(body,bytes) or len(body) > MAX_BODY:
        raise RelayError(-32602,"webhook body exceeds the supported limit")
    stamp = str(int(timestamp))
    signed = (webhook_id+"."+stamp+".").encode("utf-8") + body
    signatures = []
    for value in (secret,previous_secret):
        if value is not None:
            digest = hmac.new(signing_key(value),signed,hashlib.sha256).digest()
            signature = "v1,"+base64.b64encode(digest).decode("ascii")
            if signature not in signatures:
                signatures.append(signature)
    return {"Content-Type":"application/json","webhook-id":webhook_id,
            "webhook-timestamp":stamp,"webhook-signature":" ".join(signatures),
            "X-MCP-Subscription-Id":subscription_id}


def callback_url(url, allowed_hosts):
    try:
        if not isinstance(url,str) or len(url) > 2048 or any(ord(c) <= 32 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None,443)):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if len(host) > 253 or host.endswith(".") or "%" in host:
            raise ValueError
        if host not in allowed_hosts:
            # Hostname-only feedback lets the operator review the platform's
            # actual callback host. Never echo its path, query or signing key.
            raise RelayError(-32602,"callback hostname is not approved",
                             {"reason":"callback_host_not_approved","hostname":host})
        return parsed,host
    except RelayError:
        raise
    except (ValueError,UnicodeError) as error:
        raise RelayError(-32602,"callback must use an approved HTTPS host on port 443") from error


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: bytes = b""
    content_type: str = "missing"
    body_oversized: bool = False


def explicit_loopback_proxy(value):
    """An operator-selected, credential-free local HTTP CONNECT endpoint."""
    if value in (None, ""):
        return None
    try:
        if not isinstance(value,str) or len(value)>256 or any(ord(c)<=32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme!="http" or parsed.hostname not in {"127.0.0.1","::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port is None or not 1<=parsed.port<=65535):
            raise ValueError
        return parsed.hostname,parsed.port
    except (ValueError,TypeError,UnicodeError) as error:
        raise RelayError(-32602,"webhook proxy must be an explicit credential-free loopback HTTP endpoint") from error


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, *, proxy=None):
        super().__init__(host,443,timeout=10,context=ssl.create_default_context())
        self.address = address
        self.proxy = proxy
        if proxy is not None:
            # CONNECT uses the already validated public IP, never proxy-side
            # hostname resolution. TLS still authenticates the callback host.
            self.set_tunnel("["+address+"]" if ":" in address else address,443)

    def connect(self):
        raw = socket.create_connection(self.proxy or (self.address,443),timeout=self.timeout)
        try:
            if self.proxy is not None:
                self.sock = raw
                self._tunnel()  # http.client accepts only 200; no redirect follow.
            self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:
            raw.close()
            self.sock = None
            raise


class GooglePublicIPv4Resolver:
    """Explicit DoH for this relay; fixed verified resolver, no system changes."""

    def __init__(self, proxy, *, connection_factory=None):
        if proxy is None:
            raise RelayError(-32602,"public webhook DNS requires an explicit local proxy")
        self.connection_factory = connection_factory or (
            lambda: _PinnedHTTPSConnection("dns.google","8.8.8.8",proxy=proxy))

    def __call__(self, host, port, *, type, family):
        if port!=443 or type!=socket.SOCK_STREAM or family!=socket.AF_INET:
            raise RelayError(-32602,"public webhook DNS supports only HTTPS IPv4")
        connection = self.connection_factory()
        try:
            query = urlencode({"name":host,"type":"A","cd":"0","edns_client_subnet":"0.0.0.0/0"})
            connection.request("GET","/resolve?"+query,headers={"Accept":"application/dns-json"})
            response = connection.getresponse()
            body = response.read(16385)
            if response.status!=200 or len(body)>16384:
                raise ValueError
            data = json.loads(body)
            if (not isinstance(data,dict) or data.get("Status")!=0 or data.get("TC") is True
                    or data.get("CD") is True or data.get("Question")!=[{"name":host+".","type":1}]):
                raise ValueError
            records = data.get("Answer")
            if not isinstance(records,list):
                raise ValueError
            addresses = list(dict.fromkeys(row["data"] for row in records
                                           if isinstance(row,dict) and row.get("type")==1))
            if not addresses:
                raise ValueError
            for address in addresses:
                value = ipaddress.ip_address(address)
                if not isinstance(value,ipaddress.IPv4Address) or not value.is_global:
                    raise RelayError(-32602,"callback resolves outside the public IPv4 network")
            return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(address,443)) for address in addresses]
        finally:
            connection.close()


class SecureWebhookPoster:
    """No redirects or implicit proxies; validate every resolution before connect.

    This is for one approved private relay, not arbitrary user-supplied URLs.
    The exact callback hostname allowlist is a future operator decision.
    """

    def __init__(self, allowed_hosts, *, enabled=False, resolver=None, connection_factory=None, proxy=None,
                 public_dns=""):
        self.allowed_hosts = frozenset(allowed_hosts)
        self.enabled = enabled is True
        self.proxy = explicit_loopback_proxy(proxy)
        if public_dns not in ("", "google"):
            raise RelayError(-32602,"unsupported explicit webhook DNS provider")
        selected_resolver = GooglePublicIPv4Resolver(self.proxy) if public_dns=="google" else socket.getaddrinfo
        self.resolver = resolver or selected_resolver
        self.connection_factory = connection_factory or (
            lambda host,address: _PinnedHTTPSConnection(host,address,proxy=self.proxy))

    def validate_url(self, url):
        return callback_url(url,self.allowed_hosts)

    def post(self, url, body, headers):
        if not self.enabled:
            raise RelayError(-32014,"outbound webhook delivery is disabled",{"feature":"outbound"})
        parsed,host = self.validate_url(url)
        if not isinstance(body,bytes) or len(body) > MAX_BODY:
            raise RelayError(-32602,"webhook body exceeds the supported limit")
        connection = None
        try:
            # The approved local proxy route uses IPv4 only. Do not resolve or
            # connect through an IPv6 alias supplied by local network software.
            resolution = {"type":socket.SOCK_STREAM}
            if self.proxy is not None:
                resolution["family"] = socket.AF_INET
            resolved = self.resolver(host,443,**resolution)
            addresses = list(dict.fromkeys(item[4][0] for item in resolved))
            if not addresses:
                raise RelayError(-32015,"callback endpoint unavailable",{"reason":"connection_refused"})
            for address in addresses:
                value = ipaddress.ip_address(address)
                if self.proxy is not None and not isinstance(value,ipaddress.IPv4Address):
                    raise RelayError(-32602,"local webhook proxy requires public IPv4 destinations")
                if isinstance(value,ipaddress.IPv6Address) and value.ipv4_mapped is not None:
                    value = value.ipv4_mapped
                if not value.is_global:
                    raise RelayError(-32602,"callback resolves outside the public network")
            connection = self.connection_factory(host,addresses[0])
            path = parsed.path or "/"
            if parsed.query:
                path += "?"+parsed.query
            # CONNECT targets the pinned IP. The inner HTTP authority must
            # still name the validated callback, as the TLS SNI already does.
            authority = "["+host+"]" if ":" in host else host
            if parsed.port is not None:
                authority += ":"+str(parsed.port)
            request_headers = {name:value for name,value in headers.items() if name.lower()!="host"}
            request_headers["Host"] = authority
            # http.client does not follow redirects or consult proxy variables.
            connection.request("POST",path,body=body,headers=request_headers)
            response = connection.getresponse()
            response_body = response.read(4097)
            oversized = len(response_body) > 4096
            if oversized:
                response_body = b""  # An oversized challenge response cannot verify.
            return WebhookResponse(response.status,response_body,
                                   safe_content_type(response.getheader("Content-Type")),oversized)
        except RelayError:
            raise
        except ssl.SSLError as error:
            raise RelayError(-32015,"callback endpoint unavailable",{"reason":"tls_error"}) from error
        except TimeoutError as error:
            raise RelayError(-32015,"callback endpoint unavailable",{"reason":"timeout"}) from error
        except (OSError,ValueError,http.client.HTTPException) as error:
            raise RelayError(-32015,"callback endpoint unavailable",{"reason":"connection_refused"}) from error
        finally:
            if connection is not None:
                connection.close()
