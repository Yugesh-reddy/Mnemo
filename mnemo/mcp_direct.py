"""Six guarded MCP tools over direct memory, with no extraction or decay runtime.

Scope and actor come from local configuration. Each coding agent runs its own server
with its own MNEMO_ACTOR; they share memory through two scopes. The project scope
belongs to the repository the server was started in (or MNEMO_PROJECT) and is shared
by every agent working there; the global scope is shared everywhere. Reads never
filter by actor. Domain errors and invalid tool arguments return JSON
{code, message, details} with MCP's isError flag set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

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
from mnemo.project import detect_project

SCOPES = ("project", "global")
REQUEST_ID_NAMESPACE = UUID("6d0e2f3a-4c1b-5e8a-9f7d-2b3c4d5e6f70")
"""Readable request IDs map to uuid5(REQUEST_ID_NAMESPACE, text), a stable retry key."""
MAX_REQUEST_ID = 200

_pool: asyncpg.Pool | None = None
_embedder: Embedder | None = None
_lanes: dict[str, str] = {}  # scope -> namespace, resolved once per server process


def _settings() -> Settings:
    return get_settings().model_copy(update={"worker_enabled": False, "search_reinforce": False})


def _resolve_lanes(settings: Settings) -> dict[str, str]:
    """Project scope first (when there is a project), then the global namespace."""
    project = settings.project or detect_project(Path.cwd())
    lanes = {"project": f"{settings.namespace}@{project}"} if project else {}
    lanes["global"] = settings.namespace
    return lanes


def _actor(settings: Settings) -> str:
    return settings.actor or settings.agent_id


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    global _pool, _embedder, _lanes
    settings = _settings()
    _lanes = _resolve_lanes(settings)
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
        "Shared memory for the coding agents on this machine. Search it before answering "
        "questions about this project's conventions, commands, decisions or ongoing work, "
        "or the user's preferences: another agent may already know. When the user tells "
        "you something worth keeping for later sessions (a convention, command, decision, "
        "handoff note, correction or preference), save it with memory_create, or "
        "memory_update if a memory on that subject exists, and only say it is saved after "
        "the call succeeds. Don't save small talk, one-off questions or secrets. Memories "
        "belong to this project unless the user says they apply to all projects "
        "(scope='global'). Before changing a memory, read it and pass its current_event_id "
        "as expected_event_id; on REVISION_CONFLICT re-read and reconsider. request_id is "
        "optional; reuse one only to retry the identical change. Use history to pick a "
        "restore target, and ask the user when several changes fit 'undo that'. Undoing "
        "creation is unsupported."
    ),
    lifespan=lifespan,
)


def _id(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise MnemoError(
            ErrorCode.INVALID_INPUT,
            f"{field} must be an ID returned by memory_search, memory_get or memory_history",
            field=field,
        ) from exc


def _request_id(value: str | None) -> UUID:
    """A UUID is used as given, other text is hashed to a stable UUID, none gets a new one."""
    if value is None or not value.strip():
        return uuid4()
    if len(value) > MAX_REQUEST_ID or "\x00" in value:
        raise MnemoError(
            ErrorCode.INVALID_INPUT,
            f"request_id must be text of at most {MAX_REQUEST_ID} characters",
            field="request_id",
        )
    try:
        return UUID(value)
    except ValueError:
        return uuid5(REQUEST_ID_NAMESPACE, value)


def _scope(requested: str | None) -> str:
    if requested is None:
        return next(iter(_lanes))
    if requested not in SCOPES:
        raise MnemoError(ErrorCode.INVALID_INPUT, "scope must be 'project' or 'global'")
    if requested not in _lanes:
        raise MnemoError(
            ErrorCode.INVALID_INPUT,
            "This server has no project (not started in a git repository and "
            "MNEMO_PROJECT is unset); use scope='global'",
        )
    return requested


async def _scope_of(conn: asyncpg.Connection, settings: Settings, fact_id: UUID) -> str:
    """The scope holding fact_id. Unknown IDs use the first scope, which reports NOT_FOUND."""
    namespace = await conn.fetchval(
        "SELECT namespace FROM memory_fact WHERE fact_id=$1 AND namespace=ANY($2::text[]) "
        "AND user_id=$3 AND agent_id=$4",
        fact_id,
        list(_lanes.values()),
        settings.user_id,
        settings.agent_id,
    )
    return next((scope for scope, ns in _lanes.items() if ns == namespace), next(iter(_lanes)))


async def _run[T](
    fn: Callable[[DirectMemory], Awaitable[T]],
    *,
    scope: str | None = None,
    fact_id: str | None = None,
) -> tuple[T, str]:
    """Run fn in one scope: the one holding fact_id, else the requested/default scope."""
    if _pool is None or _embedder is None:
        raise RuntimeError("Direct MCP lifespan is not running")
    settings = _settings()
    try:
        async with _pool.acquire() as conn:
            if fact_id is not None:
                scope = await _scope_of(conn, settings, _id(fact_id, "fact_id"))
            scope = _scope(scope)
            store = MnemoStore(
                conn,
                _embedder,
                settings=settings,
                namespace=_lanes[scope],
                user_id=settings.user_id,
                agent_id=settings.agent_id,
            )
            return await fn(DirectMemory(store)), scope
    except ValueError as exc:
        raise MnemoError(ErrorCode.INVALID_INPUT, str(exc)) from exc


def _scoped(result: Any, scope: str) -> dict:
    return {**result.model_dump(mode="json"), "scope": scope}


@mcp.tool()
async def memory_create(
    subject: str,
    predicate: str,
    value: str,
    request_id: str | None = None,
    scope: str | None = None,
) -> dict:
    """Save a new memory for later sessions and other agents: a convention, command,
    decision, handoff note, correction or the user's preference. Use it whenever the user
    asks you to remember or note something lasting; never claim to remember without it.
    Search first: if a memory on the same subject exists (ALREADY_EXISTS returns its
    fact_id), use memory_update. scope: 'project' (default inside a repository) or
    'global' (applies to all the user's projects). request_id is optional: send the same
    value again only to retry this exact change.
    """
    result, used = await _run(
        lambda d: d.create(
            subject,
            predicate,
            value,
            request_id=_request_id(request_id),
            actor=_actor(_settings()),
        ),
        scope=scope,
    )
    return _scoped(result, used)


@mcp.tool()
async def memory_get(fact_id: str, event_id: str | None = None) -> dict:
    """Get current value and current_event_id for a guarded update/revert. With event_id,
    get that exact historical value and whether it is restorable. Unknown: NOT_FOUND.
    """
    result, scope = await _run(
        lambda d: d.get(
            _id(fact_id, "fact_id"),
            _id(event_id, "event_id") if event_id is not None else None,
        ),
        fact_id=fact_id,
    )
    return _scoped(result, scope)


@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> dict:
    """Search shared memory (this project and global). Do this before answering questions
    about the project's conventions, commands, decisions or ongoing work, or the user's
    preferences: another agent may have saved them. Hits include fact_id, event_id and
    scope. Similar text never authorizes an overwrite; read with memory_get first.
    """
    hits: list[dict] = []
    for lane in list(_lanes):
        found, scope = await _run(lambda d: d.search(query, limit=limit), scope=lane)
        hits += [_scoped(hit, scope) for hit in found]
    hits.sort(key=lambda hit: hit["score"], reverse=True)  # stable: project first on ties
    return {"hits": hits[:limit]}


@mcp.tool()
async def memory_update(
    fact_id: str, value: str, expected_event_id: str, request_id: str | None = None
) -> dict:
    """Change a memory that changed or was wrong, e.g. when the user corrects or withdraws
    it (write the current truth, such as "no on-call handoff day"). Pass the
    current_event_id you read as expected_event_id. REVISION_CONFLICT: re-read and
    reconsider. Identical value: status=no_change. request_id is optional: send the same
    value again only to retry this exact change.
    """
    result, scope = await _run(
        lambda d: d.update(
            _id(fact_id, "fact_id"),
            value,
            expected_event_id=_id(expected_event_id, "expected_event_id"),
            request_id=_request_id(request_id),
            actor=_actor(_settings()),
        ),
        fact_id=fact_id,
    )
    return _scoped(result, scope)


@mcp.tool()
async def memory_history(fact_id: str, cursor: str | None = None, limit: int = 20) -> dict:
    """Newest-first revisions with value previews, provenance, actor, current_event_id
    and next_cursor. Use returned IDs to select a revert target, never a reconstructed
    old value. Ask the user if several revisions could match 'undo that'.
    """
    page, scope = await _run(
        lambda d: d.history(_id(fact_id, "fact_id"), cursor=cursor, limit=limit),
        fact_id=fact_id,
    )
    return _scoped(page, scope)


@mcp.tool()
async def memory_revert(
    fact_id: str, to_event_id: str, expected_event_id: str, request_id: str | None = None
) -> dict:
    """Restore a historical event's exact value as a NEW revision; history stays intact.
    Requires the current_event_id you read. Revert-to-current is no_change. For ambiguity,
    ask the user first. Undoing creation is unsupported (UNSUPPORTED_OPERATION).
    request_id is optional, for identical retries only; on conflict re-read and reconsider.
    """
    result, scope = await _run(
        lambda d: d.revert(
            _id(fact_id, "fact_id"),
            _id(to_event_id, "to_event_id"),
            expected_event_id=_id(expected_event_id, "expected_event_id"),
            request_id=_request_id(request_id),
            actor=_actor(_settings()),
        ),
        fact_id=fact_id,
    )
    return _scoped(result, scope)


def main() -> None:
    from mnemo import __version__

    parser = argparse.ArgumentParser(description="Run six guarded memory tools over MCP stdio.")
    parser.add_argument("--version", action="version", version=f"mnemo-mcp-direct {__version__}")
    parser.parse_args()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
