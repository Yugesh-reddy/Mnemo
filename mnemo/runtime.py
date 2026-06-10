"""Owned background tasks shared by the worker CLI and MCP lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector, validate_embedding_dimension
from mnemo.decay import decay_sweep
from mnemo.embedder import build_embedder
from mnemo.extraction import ExtractionWorker, build_extractor
from mnemo.quality import build_verifier

logger = logging.getLogger(__name__)


async def scheduled_decay(
    pool: asyncpg.Pool, embedder: Any, settings: Settings, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            async with pool.acquire() as conn:
                scopes = await conn.fetch(
                    "SELECT DISTINCT namespace, user_id, agent_id FROM memory_fact "
                    "WHERE status='active'"
                )
                for scope in scopes:
                    store = MnemoStore(conn, embedder, settings=settings, **dict(scope))
                    archived = await decay_sweep(store)
                    if archived:
                        logger.info("Archived %s facts in scope %s", archived, tuple(scope))
        except Exception:
            logger.exception("Decay sweep failed; will retry at the next interval")
        try:
            await asyncio.wait_for(stop.wait(), settings.decay_interval_seconds)
        except TimeoutError:
            pass


@asynccontextmanager
async def background_runtime(
    settings: Settings,
    *,
    embedder: Any | None = None,
    extractor: Any | None = None,
    verifier: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    owned = []
    pool = None
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    try:
        if embedder is None:
            embedder = build_embedder(settings)
            owned.append(embedder)

        async def init(conn: asyncpg.Connection) -> None:
            await register_vector(conn)
            await validate_embedding_dimension(conn, settings.embed_dim)

        pool = await asyncpg.create_pool(settings.dsn, init=init, min_size=2, max_size=6)
        if settings.worker_enabled:
            if extractor is None:
                extractor = build_extractor(settings)
                owned.append(extractor)
            if verifier is None:
                verifier = await asyncio.to_thread(build_verifier, settings)
                owned.append(verifier)

            async def consume() -> None:
                while not stop.is_set():
                    try:
                        async with pool.acquire() as conn:
                            worker = ExtractionWorker(
                                conn, embedder, extractor, verifier, settings=settings
                            )
                            await worker.run(stop=stop, poll_interval=settings.worker_poll_seconds)
                    except Exception:
                        logger.exception("Worker connection failed; reconnecting")
                        try:
                            await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
                        except TimeoutError:
                            pass

            tasks = [
                asyncio.create_task(consume()),
                asyncio.create_task(scheduled_decay(pool, embedder, settings, stop)),
            ]
        yield {"pool": pool, "embedder": embedder, "stop": stop}
    finally:
        stop.set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if pool is not None:
            await pool.close()
        for component in reversed(owned):
            close = getattr(component, "close", None)
            if close:
                await asyncio.to_thread(close)
