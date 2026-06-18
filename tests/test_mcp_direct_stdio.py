"""The legacy MCP write/history/revert lifecycle works without a model server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from mnemo.config import get_settings


def _payload(result: CallToolResult) -> Any:
    assert not result.isError, result.content
    items = [json.loads(item.text) for item in result.content if isinstance(item, TextContent)]
    return items[0] if len(items) == 1 else items


async def test_external_lifecycle_postgres_mysql_postgres(
    _disposable_test_db: str, clean_memory
) -> None:
    namespace = "direct-" + uuid4().hex
    env = {
        **os.environ,
        "MNEMO_DSN": _disposable_test_db,
        "MNEMO_BACKEND": "hash",
        "MNEMO_EMBED_DIM": str(get_settings().embed_dim),
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_SEARCH_REINFORCE": "false",
        "MNEMO_NAMESPACE": namespace,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemo.mcp_server"], env=env)
    async with asyncio.timeout(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                first = _payload(
                    await client.call_tool(
                        "memory_add",
                        {
                            "subject": "user",
                            "predicate": "preferred_database",
                            "object": "PostgreSQL",
                            "provenance": "direct_user_statement",
                        },
                    )
                )
                changed = _payload(
                    await client.call_tool(
                        "memory_add",
                        {"subject": "user", "predicate": "preferred_database", "object": "MySQL"},
                    )
                )
                fact_id = first["fact_id"]
                assert first["op"] == "ADD" and changed["op"] == "UPDATE"
                assert changed["fact_id"] == fact_id
                current = _payload(await client.call_tool("memory_get", {"fact_id": fact_id}))
                assert current["object_text"] == "MySQL"
                assert current["event_id"] == changed["event_id"]

                history = _payload(await client.call_tool("memory_blame", {"fact_id": fact_id}))
                assert [(event["op"], event["object_text"]) for event in history] == [
                    ("ADD", "PostgreSQL"),
                    ("UPDATE", "MySQL"),
                ]
                restored = _payload(
                    await client.call_tool(
                        "memory_revert", {"fact_id": fact_id, "to_event_id": first["event_id"]}
                    )
                )
                assert restored["op"] == "REVERT" and restored["object_text"] == "PostgreSQL"
                current = _payload(await client.call_tool("memory_get", {"fact_id": fact_id}))
                assert current["object_text"] == "PostgreSQL"
                assert current["event_id"] == restored["event_id"]
                hits = _payload(
                    await client.call_tool("memory_search", {"query": "preferred_database"})
                )
                hits = hits if isinstance(hits, list) else [hits]
                assert [hit["value"] for hit in hits if hit["fact_id"] == fact_id] == ["PostgreSQL"]

    # A fresh server process must read the persisted restored revision.
    async with asyncio.timeout(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                current = _payload(await client.call_tool("memory_get", {"fact_id": fact_id}))
                assert current["object_text"] == "PostgreSQL"
                assert current["event_id"] == restored["event_id"]

    conn = await asyncpg.connect(_disposable_test_db)
    try:
        rows = await conn.fetch(
            "SELECT event_id, op, object_text, superseded_by FROM memory_event "
            "WHERE fact_id=$1 ORDER BY seq",
            UUID(fact_id),
        )
        assert [(row["op"], row["object_text"]) for row in rows] == [
            ("ADD", "PostgreSQL"),
            ("UPDATE", "MySQL"),
            ("REVERT", "PostgreSQL"),
        ]
        assert [str(row["event_id"]) for row in rows] == [
            first["event_id"],
            changed["event_id"],
            restored["event_id"],
        ]
        assert rows[0]["superseded_by"] == rows[1]["event_id"]
        assert rows[1]["superseded_by"] == rows[2]["event_id"]
        assert rows[2]["superseded_by"] is None
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memory_current WHERE fact_id=$1", UUID(fact_id)
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM extraction_job WHERE namespace=$1", namespace)
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM quality_decision WHERE namespace=$1", namespace
            )
            == 0
        )
    finally:
        await conn.close()
