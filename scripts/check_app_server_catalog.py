#!/usr/bin/env python3
"""Read-only App Server protocol and model-catalog probe."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from engine.codex_app_server import AppServerError, CodexAppServerClient


async def probe(socket_path: str) -> tuple[str, int]:
    client = CodexAppServerClient(socket_path)
    try:
        await client.connect()
        models = await client.list_all_models(
            include_hidden=False, force_refresh=True
        )
        defaults = [model for model in models if model.get("isDefault")]
        if len(defaults) != 1:
            raise AppServerError("model catalog must contain exactly one default")
        return client.server_version, len(models)
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        default="/run/codex-web-app-server/app-server.sock",
    )
    args = parser.parse_args()
    try:
        version, model_count = asyncio.run(probe(args.socket))
    except (AppServerError, OSError, ValueError) as error:
        print(
            "app_server_catalog=failed "
            f"error={type(error).__name__} detail={str(error)[:180]}",
            file=sys.stderr,
        )
        return 1
    print(
        f"app_server_catalog=ok version={version} models={model_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
