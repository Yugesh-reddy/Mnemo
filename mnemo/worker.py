"""Run extraction and scheduled decay until SIGINT/SIGTERM."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from mnemo.audit import queue_health
from mnemo.config import get_settings
from mnemo.db import connect
from mnemo.runtime import background_runtime


async def run(*, health: bool = False) -> None:
    settings = get_settings()
    if health:
        conn = await connect()
        try:
            result = await queue_health(
                conn,
                namespace=settings.namespace,
                user_id=settings.user_id,
                agent_id=settings.agent_id,
            )
            print(json.dumps(result, default=str, indent=2))
        finally:
            await conn.close()
        return
    settings = settings.model_copy(update={"worker_enabled": True})
    async with background_runtime(settings) as runtime:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runtime["stop"].set)
        try:
            await runtime["stop"].wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(health=args.health))


if __name__ == "__main__":
    main()
