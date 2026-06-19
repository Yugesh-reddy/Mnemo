"""Six guarded MCP tools over direct memory, with no extraction or decay runtime.

Scope and actor come from local configuration. Domain errors and invalid tool
arguments return JSON {code, message, details} with MCP's isError flag set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector, validate_embedding_dimension
from mnemo.direct import DirectMemory
from mnemo.embedder import Embedder, build_embedder
from mnemo.errors import ErrorCode, MnemoError

_pool: asyncpg.Pool | None = None
_embedder: Embedder | None = None


def _settings() -> Settings:
    return get_settings().model_copy(update={"worker_enabled": False, "search_reinforce": False})


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    global _pool, _embedder
    settings = _settings()
    embedder = build_embedder(settings)
    pool = None
    try:

        async def init(conn: asyncpg.Connection) -> None:
            await register_vector(conn)
            await validate_embedding_dimension(conn, settings.embed_dim)

        pool = await asyncpg.create_pool(settings.dsn, init=init, min_size=1, max_size=5)
        _pool, _embedder = pool, embedder
        yield {"pool": pool, "embedder": embedder}
    finally:
        _pool, _embedder = None, None
        try:
            if pool is not None:
                await pool.close()
        finally:
            close = getattr(embedder, "close", None)
            if close:
                await asyncio.to_thread(close)


class _DirectMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            # FastMCP prefixes exceptions with prose. Follow its exception chain
            # instead of parsing messages so clients receive the original JSON.
            cause: BaseException | None = exc
            while cause is not None:
                error = None
                if isinstance(cause, MnemoError):
                    error = cause
                elif isinstance(cause, ValidationError):
                    error = MnemoError(ErrorCode.INVALID_INPUT, "Invalid tool arguments")
                if error is not None:
                    return CallToolResult(
                        isError=True,
                        content=[TextContent(type="text", text=json.dumps(error.to_dict()))],
                    )
                cause = cause.__cause__
            raise


mcp = _DirectMCP(
    "mnemo-direct",
    instructions=(
        "Search and get before changing existing memory. Use returned fact/event IDs; "
        "pass the current_event_id you read as expected_event_id. On REVISION_CONFLICT, "
        "re-read and reconsider. Reuse request_id only for an identical retry. Use history "
        "to select a restore target. When several changes fit 'undo that', ask the user "
        "which one before mutating. Undoing creation is unsupported."
    ),
    lifespan=lifespan,
)


async def _run[T](fn: Callable[[DirectMemory], Awaitable[T]]) -> T:
    if _pool is None or _embedder is None:
        raise RuntimeError("Direct MCP lifespan is not running")
    settings = _settings()
    try:
        async with _pool.acquire() as conn:
            store = MnemoStore(
                conn,
                _embedder,
                settings=settings,
                namespace=settings.namespace,
                user_id=settings.user_id,
                agent_id=settings.agent_id,
            )
            return await fn(DirectMemory(store))
    except ValueError as exc:
        raise MnemoError(ErrorCode.INVALID_INPUT, str(exc)) from exc


@mcp.tool()
async def memory_create(subject: str, predicate: str, value: str, request_id: str) -> dict:
    """Create a NEW memory. ALREADY_EXISTS returns its fact_id; use memory_update instead.
    Generate request_id once per intended change and reuse it only for identical retries.
    """
    result = await _run(
        lambda d: d.create(
            subject,
            predicate,
            value,
            request_id=UUID(request_id),
            actor=_settings().agent_id,
        )
    )
    return result.model_dump(mode="json")


@mcp.tool()
async def memory_get(fact_id: str, event_id: str | None = None) -> dict:
    """Get current value and current_event_id for a guarded update/revert. With event_id,
    get that exact historical value and whether it is restorable. Unknown: NOT_FOUND.
    """
    result = await _run(
        lambda d: d.get(
            UUID(fact_id),
            UUID(event_id) if event_id is not None else None,
        )
    )
    return result.model_dump(mode="json")


@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> dict:
    """Find candidate memories; hits include fact_id and event_id. Similarity never
    authorizes a merge or overwrite. Confirm the current value with memory_get first.
    """
    hits = await _run(lambda d: d.search(query, limit=limit))
    return {"hits": [hit.model_dump(mode="json") for hit in hits]}


@mcp.tool()
async def memory_update(fact_id: str, value: str, expected_event_id: str, request_id: str) -> dict:
    """Change a memory using the current_event_id you read as expected_event_id.
    REVISION_CONFLICT: re-read and reconsider. Identical value: status=no_change.
    Reuse request_id only when retrying this exact change with the same parameters.
    """
    result = await _run(
        lambda d: d.update(
            UUID(fact_id),
            value,
            expected_event_id=UUID(expected_event_id),
            request_id=UUID(request_id),
            actor=_settings().agent_id,
        )
    )
    return result.model_dump(mode="json")


@mcp.tool()
async def memory_history(fact_id: str, cursor: str | None = None, limit: int = 20) -> dict:
    """Newest-first revisions with value previews, provenance, actor, current_event_id
    and next_cursor. Use returned IDs to select a revert target, never a reconstructed
    old value. Ask the user if several revisions could match 'undo that'.
    """
    page = await _run(lambda d: d.history(UUID(fact_id), cursor=cursor, limit=limit))
    return page.model_dump(mode="json")


@mcp.tool()
async def memory_revert(
    fact_id: str, to_event_id: str, expected_event_id: str, request_id: str
) -> dict:
    """Restore a historical event's exact value as a NEW revision; history stays intact.
    Requires the current_event_id you read. Revert-to-current is no_change. For ambiguity,
    ask the user first. Undoing creation is unsupported (UNSUPPORTED_OPERATION).
    Reuse request_id only for an identical retry; on conflict re-read and reconsider.
    """
    result = await _run(
        lambda d: d.revert(
            UUID(fact_id),
            UUID(to_event_id),
            expected_event_id=UUID(expected_event_id),
            request_id=UUID(request_id),
            actor=_settings().agent_id,
        )
    )
    return result.model_dump(mode="json")


def main() -> None:
    from mnemo import __version__

    parser = argparse.ArgumentParser(description="Run six guarded memory tools over MCP stdio.")
    parser.add_argument("--version", action="version", version=f"mnemo-mcp-direct {__version__}")
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
