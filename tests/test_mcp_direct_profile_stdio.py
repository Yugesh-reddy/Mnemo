"""A real stdio client exercises the guarded profile and durable retry errors."""

import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

import asyncpg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from mnemo.config import get_settings


def payload(result, *, code=None):
    assert result.isError == (code is not None), result.content
    items = [item.text for item in result.content if isinstance(item, TextContent)]
    assert len(items) == 1
    value = json.loads(items[0])
    if code:
        assert value["code"] == code and isinstance(value["details"], dict)
    return value


async def test_direct_profile_ollama_starts_without_background_models(_disposable_test_db):
    env = {
        **os.environ,
        "MNEMO_DSN": _disposable_test_db,
        "MNEMO_BACKEND": "ollama",
        "MNEMO_OLLAMA_HOST": "http://127.0.0.1:1",
        "MNEMO_EMBED_DIM": str(get_settings().embed_dim),
        "MNEMO_WORKER_ENABLED": "true",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemo.mcp_direct"], env=env)
    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert len((await client.list_tools()).tools) == 6
                payload(
                    await client.call_tool("memory_get", {"fact_id": str(uuid4())}),
                    code="NOT_FOUND",
                )


async def test_direct_profile_lifecycle_and_errors(_disposable_test_db, clean_memory):
    namespace = "direct-profile-" + uuid4().hex
    env = {
        **os.environ,
        "MNEMO_DSN": _disposable_test_db,
        "MNEMO_BACKEND": "hash",
        "MNEMO_EMBED_DIM": str(get_settings().embed_dim),
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_NAMESPACE": namespace,
        "MNEMO_USER_ID": "profile-user",
        "MNEMO_AGENT_ID": "profile-agent",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemo.mcp_direct"], env=env)
    async with asyncio.timeout(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert {tool.name for tool in (await client.list_tools()).tools} == {
                    "memory_create",
                    "memory_get",
                    "memory_search",
                    "memory_update",
                    "memory_history",
                    "memory_revert",
                }
                args = dict(
                    subject="user",
                    predicate="preferred_database",
                    value="PostgreSQL",
                    request_id=str(uuid4()),
                )
                first = payload(await client.call_tool("memory_create", args))
                payload(
                    await client.call_tool("memory_create", {**args, "request_id": str(uuid4())}),
                    code="ALREADY_EXISTS",
                )
                change_args = dict(
                    fact_id=first["fact_id"],
                    value="MySQL",
                    expected_event_id=first["event_id"],
                    request_id=str(uuid4()),
                )
                changed = payload(await client.call_tool("memory_update", change_args))
                retry = payload(await client.call_tool("memory_update", change_args))
                assert retry["replayed"] and retry["event_id"] == changed["event_id"]
                payload(
                    await client.call_tool("memory_update", {**change_args, "value": "SQLite"}),
                    code="REQUEST_ID_REUSED",
                )
                stale = payload(
                    await client.call_tool(
                        "memory_update",
                        {**change_args, "value": "SQLite", "request_id": str(uuid4())},
                    ),
                    code="REVISION_CONFLICT",
                )
                assert stale["details"]["current_event_id"] == changed["event_id"]
                history = payload(
                    await client.call_tool(
                        "memory_history", {"fact_id": first["fact_id"], "limit": 2}
                    )
                )
                assert history["current_event_id"] == changed["event_id"]
                assert [row["op"] for row in history["entries"]] == ["UPDATE", "ADD"]
                restore_args = dict(
                    fact_id=first["fact_id"],
                    to_event_id=first["event_id"],
                    expected_event_id=changed["event_id"],
                    request_id=str(uuid4()),
                )
                restored = payload(await client.call_tool("memory_revert", restore_args))
                assert restored["value"] == "PostgreSQL"
                current = payload(
                    await client.call_tool("memory_get", {"fact_id": first["fact_id"]})
                )
                assert current["current_event_id"] == restored["event_id"]
                assert current["value"] == "PostgreSQL" and current["trust_level"] == "low"
                historic = payload(
                    await client.call_tool(
                        "memory_get", {"fact_id": first["fact_id"], "event_id": changed["event_id"]}
                    )
                )
                assert historic["value"] == "MySQL" and historic["restorable"]
                hits = payload(await client.call_tool("memory_search", {"query": "PostgreSQL"}))[
                    "hits"
                ]
                assert hits[0]["event_id"] == restored["event_id"]
                assert {hit["value"] for hit in hits} == {"PostgreSQL"}
                other = payload(
                    await client.call_tool(
                        "memory_create",
                        {**args, "predicate": "name", "value": "Ada", "request_id": str(uuid4())},
                    )
                )
                payload(
                    await client.call_tool(
                        "memory_revert",
                        {
                            **restore_args,
                            "to_event_id": other["event_id"],
                            "expected_event_id": restored["event_id"],
                            "request_id": str(uuid4()),
                        },
                    ),
                    code="INVALID_RESTORE_TARGET",
                )
                payload(
                    await client.call_tool("memory_get", {"fact_id": str(uuid4())}),
                    code="NOT_FOUND",
                )
                payload(
                    await client.call_tool("memory_get", {"fact_id": "invalid"}),
                    code="INVALID_INPUT",
                )
                payload(await client.call_tool("memory_get", {}), code="INVALID_INPUT")
                payload(
                    await client.call_tool("memory_create", {**args, "value": 123}),
                    code="INVALID_INPUT",
                )
                payload(
                    await client.call_tool(
                        "memory_get", {"fact_id": first["fact_id"], "event_id": ""}
                    ),
                    code="INVALID_INPUT",
                )

    async with asyncio.timeout(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                retry = payload(await client.call_tool("memory_update", change_args))
                assert retry["replayed"] and retry["event_id"] == changed["event_id"]
                retry = payload(await client.call_tool("memory_revert", restore_args))
                assert retry["replayed"] and retry["event_id"] == restored["event_id"]
                current = payload(
                    await client.call_tool("memory_get", {"fact_id": first["fact_id"]})
                )
                assert current["current_event_id"] == restored["event_id"]
                page = payload(
                    await client.call_tool(
                        "memory_history", {"fact_id": first["fact_id"], "limit": 2}
                    )
                )
                tail = payload(
                    await client.call_tool(
                        "memory_history",
                        {"fact_id": first["fact_id"], "cursor": page["next_cursor"]},
                    )
                )
                assert [entry["op"] for entry in page["entries"] + tail["entries"]] == [
                    "REVERT",
                    "UPDATE",
                    "ADD",
                ]

    conn = await asyncpg.connect(_disposable_test_db)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memory_event WHERE fact_id=$1", UUID(first["fact_id"])
            )
            == 3
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM memory_mutation_receipt WHERE namespace=$1", namespace
            )
            == 4
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM memory_event WHERE actor='profile-agent'")
            == 4
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
        assert await conn.fetchval("SELECT sum(recall_count) FROM memory_event") == 0
    finally:
        await conn.close()
