"""Per-instance outbound network policy without changing WSL-wide settings.

The policy is deliberately environment-only: callers pass its returned copy to
children, while the caller's ``os.environ`` and Windows proxy settings remain
untouched.  It does not probe a proxy or claim connectivity.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Mapping
from urllib.parse import urlsplit

PROXY_KEYS = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY',
              'https_proxy', 'http_proxy', 'all_proxy')
NO_PROXY_KEYS = ('NO_PROXY', 'no_proxy')
LOOPBACK = ('localhost', '127.0.0.1', '::1')
MODES = frozenset({'inherit', 'direct', 'proxy'})
LABELS = {'inherit': '沿用现有 WSL 配置', 'direct': '明确直连', 'proxy': '指定代理'}


def valid_proxy(value: object) -> bool:
    """Return whether *value* is a safe HTTP(S) proxy URL.

    Do not normalize or return the URL: it may carry encoded credentials and
    must stay in the private profile/environment only.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(ord(char) < 32 or char.isspace() or char == '\\' for char in value) or re.search(r'%(?![0-9A-Fa-f]{2})', value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
            or not parsed.hostname or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)):
        return False
    host = parsed.hostname
    if any(ord(char) < 32 or char.isspace() or char in r'\/?#@' for char in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        return False
    return bool(host) and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                             for label in host.rstrip('.').split('.'))



def _with_loopback(value: str) -> str:
    items = [item.strip() for item in value.split(',') if item.strip()]
    lower = {item.lower() for item in items}
    items.extend(item for item in LOOPBACK if item not in lower)
    return ','.join(items)


def policy(settings: Mapping[str, object], parent: Mapping[str, str]) -> dict[str, str]:
    """Build the complete proxy/NO_PROXY environment for one child process.

    Missing keys are explicitly represented as empty strings so a systemd user
    manager cannot leak stale manager-level proxy values into a service.
    """
    mode = settings.get('network_mode')
    selected = settings.get('network_proxy', '')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('invalid network mode')
    if mode == 'proxy' and not valid_proxy(selected):
        raise ValueError('invalid proxy configuration')
    output: dict[str, str] = {}
    if mode == 'proxy':
        output.update({key: selected for key in PROXY_KEYS})
    elif mode == 'direct':
        output.update({key: '' for key in PROXY_KEYS})
    else:
        # Requests/pip treat an empty lower-case variable as an explicit
        # removal of its upper-case alias.  Emitting a missing alias as empty
        # therefore silently disabled an inherited HTTPS_PROXY.  Normalize
        # each pair using the conventional lower-case precedence, including
        # an explicitly empty value, before suppressing systemd leftovers.
        for key in ('http_proxy', 'https_proxy', 'all_proxy'):
            value = parent[key] if key in parent else parent.get(key.upper(), '')
            output[key] = output[key.upper()] = value
    bypass = parent['no_proxy'] if 'no_proxy' in parent else parent.get('NO_PROXY', '')
    output['NO_PROXY'] = output['no_proxy'] = _with_loopback(bypass)
    return output


def summary(settings: Mapping[str, object], parent: Mapping[str, str]) -> dict[str, object]:
    """Non-secret state for inspection UI; this is not a connectivity check."""
    mode = settings.get('network_mode', 'inherit')
    selected = policy(settings, parent)
    configured = any(selected[key] for key in PROXY_KEYS)
    return {
        'mode': mode,
        'effective_mode': 'proxy' if configured else 'direct',
        'proxy_configured': configured,
        'description': LABELS[mode] + ('；检测到代理配置' if configured else '；无显式 HTTP 代理')
                       + '。这是配置状态，未测试连通性。',
        'model_connection': 'not_checked',
    }


def pip_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """Keep pip.conf/PIP_CONFIG_FILE from overriding the selected route.

    pip --isolated still loads global configuration and PIP_CONFIG_FILE.
    Its documented null-file setting disables those files without placing a
    proxy credential on the command line or changing the caller's settings.
    """
    return {**parent, 'PIP_CONFIG_FILE': os.devnull}
