#!/usr/bin/env bash
# Reinstall the offline-pinned native interaction schema validator dependencies.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WHEELS=${1:-${NATIVE_INTERACTIONS_WHEELS_DIR:-}}
if [[ -z "$WHEELS" || ! -d "$WHEELS" ]]; then
  echo "请以第一个参数或 NATIVE_INTERACTIONS_WHEELS_DIR 提供经验证的离线 wheel 目录" >&2
  exit 1
fi
"$ROOT/.venv/bin/python" -m pip install --no-index --find-links "$WHEELS" --require-hashes -r "$ROOT/requirements-native-interactions.lock"
"$ROOT/.venv/bin/python" -m pip check
