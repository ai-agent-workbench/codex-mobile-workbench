#!/usr/bin/env bash
# Run the Codex Web unit suite with a Linux-native temporary directory.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${CODEX_WEB_PYTHON:-$APP_DIR/.venv/bin/python}"

# The ChatGPT desktop WSL backend can inherit Windows TEMP/TMP values that
# resolve under /mnt/c. Keep permission-sensitive fixtures on WSL ext4.
unset TEMP TMP TMPDIR

cd "$APP_DIR"
# Match discover's import path for legacy shared fixtures during targeted runs.
export PYTHONPATH="$APP_DIR/tests${PYTHONPATH:+:$PYTHONPATH}"
if (($#)); then
    if [[ $1 == ui ]]; then
        shift
        exec "$PYTHON_BIN" scripts/run_ui_checks.py "$@"
    fi
    exec "$PYTHON_BIN" -m unittest "$@"
fi
exec "$PYTHON_BIN" -m unittest discover tests -v
