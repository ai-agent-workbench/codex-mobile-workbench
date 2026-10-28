"""Exercise real curl/pip against an isolated HTTPS origin and CONNECT proxy.

No public network, accounts, production state, or real proxy configuration.
Run using the candidate interpreter, passing the candidate source directory.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import zipfile


sys.path.insert(0, sys.argv[1])
from shared.instance_network import policy, pip_environment


def wheel_bytes():
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('onboarding_fixture.py', 'VALUE = 42\n')
        archive.writestr('onboarding_fixture-0.0.0.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: onboarding-fixture\nVersion: 0.0.0\n')
        archive.writestr('onboarding_fixture-0.0.0.dist-info/WHEEL',
                         'Wheel-Version: 1.0\nGenerator: local-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('onboarding_fixture-0.0.0.dist-info/RECORD', '')
    return target.getvalue()


def main():
    wheel = wheel_bytes()
    visits = []
    connects = []

    class Origin(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            visits.append(self.path)
            if self.path.startswith('/simple/onboarding-fixture'):
                data = (b'<a href="/files/onboarding_fixture-0.0.0-py3-none-any.whl#sha256='
                        + hashlib.sha256(wheel).hexdigest().encode() + b'">fixture</a>')
                content_type = 'text/html'
            elif self.path.startswith('/files/'):
                data, content_type = wheel, 'application/octet-stream'
            else:
                data, content_type = b'local-only', 'text/plain'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            host, port = self.path.rsplit(':', 1)
            if host != '127.0.0.2' or int(port) != origin.server_port:
                self.send_error(403)
                return
            connects.append(True)
            with socket.create_connection(('127.0.0.2', origin.server_port), timeout=3) as upstream:
                self.send_response(200, 'Connection established')
                self.end_headers()
                peers = [self.connection, upstream]
                try:
                    while True:
                        ready, _, _ = select.select(peers, [], [], 4)
                        if not ready:
                            return
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is self.connection else self.connection).sendall(data)
                except (OSError, ConnectionError):
                    return

    with tempfile.TemporaryDirectory(prefix='onboarding-local-net-') as directory:
        root = Path(directory)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=local-fixture', '-addext', 'subjectAltName=IP:127.0.0.2',
                        '-keyout', str(key), '-out', str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        origin = Server(('127.0.0.2', 0), Origin)
        origin.socket = context.wrap_socket(origin.socket, server_side=True)
        proxy = Server(('127.0.0.1', 0), Proxy)
        for server in (origin, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            address = 'https://127.0.0.2:' + str(origin.server_port)
            proxy_url = 'http://fixture-user:fixture-password@127.0.0.1:' + str(proxy.server_port)
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                dead_proxy = 'http://127.0.0.1:' + str(reserve.getsockname()[1])
            parent_pip = root / 'parent-pip.conf'
            parent_pip.write_text('[global]\nproxy = ' + dead_proxy + '\n')
            base = {'PATH': os.environ['PATH'], 'HOME': str(root), 'LANG': 'C.UTF-8',
                    'REQUESTS_CA_BUNDLE': str(cert), 'PIP_CONFIG_FILE': str(parent_pip)}
            scenarios = [
                ('inherit_without_proxy', 'inherit', '', {}, True, False),
                ('inherit_existing_proxy', 'inherit', '', {'HTTPS_PROXY': proxy_url}, True, True),
                ('direct_overrides_bad_parent', 'direct', '', {'HTTPS_PROXY': dead_proxy}, True, False),
                ('specified_overrides_bad_parent', 'proxy', proxy_url, {'HTTPS_PROXY': dead_proxy}, True, True),
                ('specified_failure_no_fallback', 'proxy', dead_proxy, {}, False, False),
                ('no_proxy_is_explicit_bypass', 'proxy', dead_proxy, {'NO_PROXY': '127.0.0.2'}, True, False),
            ]
            results = []
            for label, mode, configured, inherited, success, via_proxy in scenarios:
                selected = {'network_mode': mode, 'network_proxy': configured}
                env = {**base, **policy(selected, inherited)}
                for tool in ('curl', 'pip'):
                    previous_visits, previous_connects = len(visits), len(connects)
                    if tool == 'curl':
                        args = ['curl', '--disable', '--silent', '--show-error', '--fail', '--max-time', '5',
                                '--cacert', str(cert), address + '/probe']
                    else:
                        destination = root / (label + '-wheel')
                        destination.mkdir()
                        args = [sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                                'download', '--no-deps', '--no-cache-dir', '--retries', '0', '--timeout', '3',
                                '--index-url', address + '/simple/', '--dest', str(destination), 'onboarding-fixture==0.0.0']
                    child_env = pip_environment(env) if tool == 'pip' else env
                    done = subprocess.run(args, env=child_env, capture_output=True, timeout=12)
                    passed = ((done.returncode == 0) == success
                              and (len(connects) > previous_connects) == via_proxy
                              and (success or len(visits) == previous_visits))
                    if tool == 'pip' and success:
                        obtained = list(destination.glob('*.whl'))
                        passed = passed and len(obtained) == 1 and obtained[0].read_bytes() == wheel
                    result = {'case': label, 'tool': tool, 'expected_success': success,
                              'success': done.returncode == 0, 'proxy_used': len(connects) > previous_connects,
                              'origin_requested': len(visits) > previous_visits, 'passed': passed}
                    results.append(result)
                    print(json.dumps(result), flush=True)
            if not all(item['passed'] for item in results):
                raise SystemExit(1)
            print(json.dumps({'passed': True, 'cases': len(results), 'public_network_used': False,
                              'real_credentials_used': False}), flush=True)
        finally:
            for server in (proxy, origin):
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
