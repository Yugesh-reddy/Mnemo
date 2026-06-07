"""M5: MCP server smoke tests — tool registration + an end-to-end round-trip.

Tools are exercised in-process (the @tool decorator returns the original callable)
against the test DB via dsn/embedder overrides, with a truncate teardown.
"""

from __future__ import annotations

import pytest

import mnemo.mcp_server as srv

EXPECTED_TOOLS = {
    "memory_add",
    "memory_search",
    "memory_get",
    "memory_blame",
    "memory_revert",
    "memory_diff",
    "memory_log",
    "memory_observe",
}


async def test_all_tools_registered_with_schemas() -> None:
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    assert EXPECTED_TOOLS <= set(tools)
    for name in EXPECTED_TOOLS:
        assert tools[name].description and tools[name].description.strip()
        assert tools[name].inputSchema  # JSON schema present for the client


@pytest.fixture
def mcp_overrides(_disposable_test_db, fake_embedder, clean_memory):
    srv.set_overrides(dsn=_disposable_test_db, embedder=fake_embedder)
    yield
    srv.reset_overrides()


async def test_mcp_add_search_blame_revert_roundtrip(mcp_overrides) -> None:
    added = await srv.memory_add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    assert added["op"] == "ADD"
    fact_id, add_event = added["fact_id"], added["event_id"]

    updated = await srv.memory_add(
        "user", "preferred_database", "MongoDB", provenance="agent_inference"
    )
    assert updated["op"] == "UPDATE"

    results = await srv.memory_search("database")
    assert any(f["object_text"] == "MongoDB" for f in results)

    history = await srv.memory_blame(subject="user", predicate="preferred_database")
    assert [e["op"] for e in history] == ["ADD", "UPDATE"]

    reverted = await srv.memory_revert(fact_id, add_event)
    assert reverted["op"] == "REVERT"

    got = await srv.memory_get(fact_id)
    assert got is not None and got["object_text"] == "PostgreSQL"


async def test_mcp_observe_then_log(mcp_overrides) -> None:
    res = await srv.memory_observe("t1", "I use Postgres.", "sess-1")
    assert res["status"] == "observed"
    # the enqueued turn is visible via search by session (fast-cache tier)
    hits = await srv.memory_search("Postgres", session_id="sess-1")
    assert any(h["source"] == "fast_cache" for h in hits)
