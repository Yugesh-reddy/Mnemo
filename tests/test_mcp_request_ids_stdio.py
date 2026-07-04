"""Agent-friendly request IDs: optional, any text; retries still replay and reuse is caught."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4, uuid5

from mnemo.mcp_direct import REQUEST_ID_NAMESPACE
from tests.test_mcp_shared_scope_stdio import WIDGET, agent, payload, server_env


async def test_request_ids_are_optional_and_readable(_disposable_test_db, clean_memory):
    env = server_env(_disposable_test_db, "request-ids-" + uuid4().hex, "codex", WIDGET)
    args = {"subject": "project", "predicate": "deploy day", "value": "Tuesday"}
    async with asyncio.timeout(30):
        async with agent(env) as client:
            created = payload(await client.call_tool("memory_create", args))
            assert created["status"] == "applied" and UUID(created["request_id"])

            # A readable request_id is a stable retry key.
            first = payload(
                await client.call_tool(
                    "memory_update",
                    {
                        "fact_id": created["fact_id"],
                        "value": "Thursday",
                        "expected_event_id": created["event_id"],
                        "request_id": "move-deploys-to-thursday",
                    },
                )
            )
            assert first["request_id"] == str(
                uuid5(REQUEST_ID_NAMESPACE, "move-deploys-to-thursday")
            )
            retry = payload(
                await client.call_tool(
                    "memory_update",
                    {
                        "fact_id": created["fact_id"],
                        "value": "Thursday",
                        "expected_event_id": created["event_id"],
                        "request_id": "move-deploys-to-thursday",
                    },
                )
            )
            assert retry["replayed"] and retry["event_id"] == first["event_id"]
            payload(
                await client.call_tool(
                    "memory_update",
                    {
                        "fact_id": created["fact_id"],
                        "value": "Friday",
                        "expected_event_id": first["event_id"],
                        "request_id": "move-deploys-to-thursday",
                    },
                ),
                code="REQUEST_ID_REUSED",
            )

            # Without a request_id every call is its own change.
            changed = payload(
                await client.call_tool(
                    "memory_update",
                    {
                        "fact_id": created["fact_id"],
                        "value": "Friday",
                        "expected_event_id": first["event_id"],
                    },
                )
            )
            assert changed["status"] == "applied" and changed["request_id"] != first["request_id"]
            restored = payload(
                await client.call_tool(
                    "memory_revert",
                    {
                        "fact_id": created["fact_id"],
                        "to_event_id": first["event_id"],
                        "expected_event_id": changed["event_id"],
                        "request_id": "",
                    },
                )
            )
            assert restored["value"] == "Thursday"
            history = payload(
                await client.call_tool("memory_history", {"fact_id": created["fact_id"]})
            )
            assert [entry["op"] for entry in history["entries"]] == [
                "REVERT",
                "UPDATE",
                "UPDATE",
                "ADD",
            ]


async def test_malformed_ids_get_an_actionable_error(_disposable_test_db, clean_memory):
    env = server_env(_disposable_test_db, "request-ids-bad-" + uuid4().hex, "codex", WIDGET)
    async with asyncio.timeout(30):
        async with agent(env) as client:
            error = payload(
                await client.call_tool("memory_get", {"fact_id": "deploy-day"}),
                code="INVALID_INPUT",
            )
            assert "fact_id" in error["message"] and "memory_search" in error["message"]
            assert error["details"] == {"field": "fact_id"}
            error = payload(
                await client.call_tool(
                    "memory_create",
                    {"subject": "s", "predicate": "p", "value": "v", "request_id": "x" * 201},
                ),
                code="INVALID_INPUT",
            )
            assert error["details"] == {"field": "request_id"}
