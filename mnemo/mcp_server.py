"""MCP server (stdio) exposing Mnemo's verbs as tools (spec §9).

Any MCP client (Claude Code, Cursor, …) can add / search / blame / revert / diff /
log / observe agent memory. Tools wrap the async core; each call uses a short-lived
connection. Run with ``make mcp`` (``python -m mnemo.mcp_server``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from mcp.server.fastmcp import FastMCP

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector
from mnemo.embedder import build_embedder

mcp = FastMCP("mnemo")

# Test seam: override the DSN / embedder without touching global config.
_OVERRIDE: dict[str, Any] = {}


def set_overrides(*, dsn: str | None = None, embedder: Any | None = None) -> None:
    if dsn is not None:
        _OVERRIDE["dsn"] = dsn
    if embedder is not None:
        _OVERRIDE["embedder"] = embedder


def reset_overrides() -> None:
    _OVERRIDE.clear()


async def _run(fn: Any) -> Any:
    settings = get_settings()
    dsn = _OVERRIDE.get("dsn", settings.dsn)
    embedder = _OVERRIDE.get("embedder") or build_embedder(settings)
    conn = await asyncpg.connect(dsn)
    await register_vector(conn)
    try:
        store = MnemoStore(conn, embedder, settings=settings)
        return await fn(store)
    finally:
        await conn.close()


@mcp.tool()
async def memory_add(
    subject: str,
    predicate: str,
    object: str,
    provenance: str = "agent_inference",
    confidence: float = 1.0,
    actor: str | None = None,
) -> dict:
    """Store or update a fact (subject, predicate, object). Returns the new event."""
    event = await _run(
        lambda s: s.add(
            subject, predicate, object, provenance=provenance, confidence=confidence, actor=actor
        )
    )
    return event.model_dump(mode="json")


@mcp.tool()
async def memory_search(query: str, k: int = 8, session_id: str | None = None) -> list[dict]:
    """Search current memory for facts relevant to a query (semantic + keyword)."""
    facts = await _run(lambda s: s.search(query, k=k, session_id=session_id))
    return [f.model_dump(mode="json") for f in facts]


@mcp.tool()
async def memory_get(fact_id: str) -> dict | None:
    """Get the current believed value of a fact by id."""
    fact = await _run(lambda s: s.get(UUID(fact_id)))
    return fact.model_dump(mode="json") if fact else None


@mcp.tool()
async def memory_blame(
    fact_id: str | None = None, subject: str | None = None, predicate: str | None = None
) -> list[dict]:
    """Show a fact's full history — which turn/source introduced each belief, and when."""
    fid = UUID(fact_id) if fact_id else None
    events = await _run(lambda s: s.blame(fact_id=fid, subject=subject, predicate=predicate))
    return [e.model_dump(mode="json") for e in events]


@mcp.tool()
async def memory_revert(fact_id: str, to_event_id: str) -> dict:
    """Roll a fact back to a previous event's value (history is preserved)."""
    event = await _run(lambda s: s.revert(UUID(fact_id), UUID(to_event_id)))
    return event.model_dump(mode="json")


@mcp.tool()
async def memory_diff(commit_a: str, commit_b: str) -> dict:
    """Diff believed state between two commits, as added / removed / changed entries."""
    diff = await _run(lambda s: s.diff(UUID(commit_a), UUID(commit_b)))
    return diff.model_dump(mode="json")


@mcp.tool()
async def memory_log(fact_id: str | None = None, limit: int = 50) -> list[dict]:
    """Event history for a fact (newest first), or the recent global timeline."""
    fid = UUID(fact_id) if fact_id else None
    events = await _run(lambda s: s.log(fact_id=fid, limit=limit))
    return [e.model_dump(mode="json") for e in events]


@mcp.tool()
async def memory_observe(turn_id: str, text: str, session_id: str, role: str = "user") -> dict:
    """Record a conversation turn for immediate recall + async fact extraction."""
    await _run(lambda s: s.observe(turn_id, text, session_id, role=role))
    return {"status": "observed", "turn_id": turn_id}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
