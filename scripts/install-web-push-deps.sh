#!/usr/bin/env bash
# Install the separately locked Web Push sender into this app's virtualenv.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "Web Push 依赖安装失败：应用虚拟环境不存在" >&2
    exit 1
fi
if [[ -z "${HTTPS_PROXY:-${https_proxy:-${ALL_PROXY:-${all_proxy:-}}}}}" ]]; then
    echo "Web Push 依赖安装失败：未检测到受控代理环境" >&2
    exit 1
fi

export PIP_DISABLE_PIP_VERSION_CHECK=1
"$PYTHON" -m pip install \
    --no-cache-dir \
    --require-hashes \
    -r "$APP_DIR/requirements-webpush-build.lock"
"$PYTHON" -m pip install \
    --no-cache-dir \
    --no-build-isolation \
    --require-hashes \
    -r "$APP_DIR/requirements-webpush.lock"
"$PYTHON" -m pip check
"$PYTHON" - <<'PY'
import base64
import importlib.metadata as metadata
import os
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import webpush

if not callable(webpush):
    raise SystemExit("pywebpush sender is unavailable")

# Exercise VAPID signing and RFC 8188 payload encryption without making a
# network request or persisting any ephemeral key material.
receiver = ec.generate_private_key(ec.SECP256R1())
receiver_public = receiver.public_key().public_bytes(
    serialization.Encoding.X962,
    serialization.PublicFormat.UncompressedPoint,
)
sender = ec.generate_private_key(ec.SECP256R1())

def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

response = mock.Mock(status_code=201, reason="Created", text="", headers={})
with mock.patch("requests.post", return_value=response) as post:
    result = webpush(
        subscription_info={
            "endpoint": "https://fcm.googleapis.com/fcm/send/runtime-check",
            "keys": {"p256dh": encoded(receiver_public), "auth": encoded(os.urandom(16))},
        },
        data='{"check":true}',
        vapid_private_key=Vapid(private_key=sender),
        vapid_claims={"sub": "https://localhost"},
        timeout=1,
        ttl=0,
    )
if result.status_code != 201 or post.call_count != 1:
    raise SystemExit("pywebpush cryptographic runtime check failed")
print("Web Push 依赖安装完成：pywebpush " + metadata.version("pywebpush"))
PY
