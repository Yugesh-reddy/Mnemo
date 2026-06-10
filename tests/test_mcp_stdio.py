"""An external MCP client checks the actual server lifecycle and background queue."""

import asyncio
import json
import os
import sys
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_external_stdio_observation_is_processed(_disposable_test_db, clean_memory):
    env = {
        **os.environ,
        "MNEMO_DSN": _disposable_test_db,
        "MNEMO_NAMESPACE": "stdio-" + uuid4().hex,
        "MNEMO_WORKER_ENABLED": "true",
        "MNEMO_WORKER_POLL_SECONDS": "0.02",
        "MNEMO_BACKEND": "ollama",
        "MNEMO_VERIFIER_BACKEND": "heuristic",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemo.mcp_server"], env=env)
    async with asyncio.timeout(15):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = await client.list_tools()
                assert {"memory_observe", "memory_health", "memory_decisions"} <= {
                    t.name for t in listing.tools
                }
                result = await client.call_tool(
                    "memory_observe",
                    {
                        "turn_id": "assistant",
                        "session_id": "stdio",
                        "text": "Assistant claims must not become user facts.",
                        "role": "assistant",
                    },
                )
                assert not result.isError
                while True:
                    health = await client.call_tool("memory_health", {})
                    payload = json.loads(health.content[0].text)
                    if payload["statuses"].get("done") == 1:
                        break
                    await asyncio.sleep(0.02)
                result = await client.call_tool("memory_decisions", {"turn_id": "assistant"})
                assert not result.isError
                assert "assistant turns are excluded" in str(result)
