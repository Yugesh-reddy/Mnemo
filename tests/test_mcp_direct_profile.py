"""Direct MCP schemas and isolation from the extraction runtime."""

import os
import subprocess
import sys

import pytest

from mnemo.config import Settings


async def test_direct_profile_has_exactly_six_guarded_tool_schemas():
    import mnemo.mcp_direct as srv

    required = {
        "memory_create": {"subject", "predicate", "value", "request_id"},
        "memory_get": {"fact_id"},
        "memory_search": {"query"},
        "memory_update": {"fact_id", "value", "expected_event_id", "request_id"},
        "memory_history": {"fact_id"},
        "memory_revert": {"fact_id", "to_event_id", "expected_event_id", "request_id"},
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


def test_direct_profile_import_does_not_load_background_components():
    code = """
import sys
import mnemo.mcp_direct
background = {'mnemo.extraction', 'mnemo.quality', 'mnemo.runtime', 'mnemo.decay'}
assert not background & sys.modules.keys()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "MNEMO_BACKEND": "hash", "MNEMO_WORKER_ENABLED": "false"},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["hash", "ollama"])
async def test_direct_profile_lifespan_never_starts_background_work(
    _disposable_test_db, fake_embedder, monkeypatch, backend
):
    import mnemo.mcp_direct as srv
    import mnemo.runtime as runtime

    settings = Settings(
        _env_file=None,
        dsn=_disposable_test_db,
        backend=backend,
        embed_dim=fake_embedder.dim,
        worker_enabled=backend == "ollama",
    )
    monkeypatch.setattr(srv, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "build_embedder", lambda settings: fake_embedder)

    def forbidden(*args, **kwargs):
        raise AssertionError("Direct MCP must not start extraction, verification or decay")

    for name in ("background_runtime", "build_extractor", "build_verifier", "scheduled_decay"):
        monkeypatch.setattr(runtime, name, forbidden)
    async with srv.lifespan(srv.mcp):
        assert srv._pool is not None
        assert not srv._settings().worker_enabled
        assert not srv._settings().search_reinforce
    assert srv._pool is None and srv._embedder is None
