"""Direct MCP tool schemas, tool text and server lifespan."""

import pytest

from mnemo.config import Settings


async def test_direct_profile_has_exactly_seven_guarded_tool_schemas():
    import mnemo.mcp_direct as srv

    required = {
        "memory_create": {"subject", "predicate", "value"},
        "memory_get": {"fact_id"},
        "memory_search": {"query"},
        "memory_update": {"fact_id", "value", "expected_event_id"},
        "memory_history": {"fact_id"},
        "memory_revert": {"fact_id", "to_event_id", "expected_event_id"},
        "memory_retract": {"fact_id", "expected_event_id"},
    }
    tools = {tool.name: tool for tool in await srv.mcp.list_tools()}
    assert set(tools) == set(required)
    for name, fields in required.items():
        assert set(tools[name].inputSchema["required"]) == fields
        assert (
            not {"namespace", "user_id", "agent_id", "actor", "provenance", "trust_level"}
            & tools[name].inputSchema["properties"].keys()
        )
        assert tools[name].description.strip()
    for name in ("memory_create", "memory_update"):
        source = tools[name].inputSchema["properties"]["source"]
        assert source["enum"] == ["user", "tool", "agent"] and source["default"] == "agent"


async def test_tool_text_tells_agents_when_to_use_memory():
    """Codex never saved without being told when to (docs/agent-eval baseline)."""
    import mnemo.mcp_direct as srv

    tools = {tool.name: tool.description.lower() for tool in await srv.mcp.list_tools()}
    assert "remember" in tools["memory_create"] and "never claim" in tools["memory_create"]
    assert "before answering" in tools["memory_search"]
    assert "correct" in tools["memory_update"]
    instructions = srv.mcp.instructions.lower()
    assert "before answering" in instructions and "save" in instructions


@pytest.mark.parametrize("backend", ["hash", "ollama"])
async def test_direct_profile_lifespan_holds_the_embedder_for_its_lifetime(
    _disposable_test_db, fake_embedder, monkeypatch, backend
):
    import mnemo.mcp_direct as srv

    settings = Settings(
        _env_file=None,
        db=_disposable_test_db,
        backend=backend,
        embed_dim=fake_embedder.dim,
    )
    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "build_embedder", lambda settings: fake_embedder)

    async with srv.lifespan(srv.mcp):
        assert srv._embedder is fake_embedder
        assert not srv._settings().search_reinforce
    assert srv._embedder is None
