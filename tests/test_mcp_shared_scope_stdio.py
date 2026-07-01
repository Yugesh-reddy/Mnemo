"""Separate direct servers (one per coding agent) share project and global memory.

Each agent runs its own stdio server with its own MNEMO_ACTOR. Memories written in a
project are visible to every agent in that project and to no other project; global
memories are visible everywhere. History records which agent made each change.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from mnemo.config import get_settings

WIDGET = "github.com/acme/widget"


def payload(result, *, code=None):
    assert result.isError == (code is not None), result.content
    items = [item.text for item in result.content if isinstance(item, TextContent)]
    assert len(items) == 1
    value = json.loads(items[0])
    if code:
        assert value["code"] == code
    return value


def server_env(dsn: str, namespace: str, actor: str, project: str | None) -> dict[str, str]:
    env = {
        **os.environ,
        "MNEMO_DSN": dsn,
        "MNEMO_BACKEND": "hash",
        "MNEMO_EMBED_DIM": str(get_settings().embed_dim),
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_NAMESPACE": namespace,
        "MNEMO_USER_ID": "owner",
        "MNEMO_ACTOR": actor,
    }
    env.pop("MNEMO_AGENT_ID", None)
    env.pop("MNEMO_PROJECT", None)
    if project is not None:
        env["MNEMO_PROJECT"] = project
    return env


@asynccontextmanager
async def agent(env: dict[str, str], cwd: Path | None = None) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mnemo.mcp_direct"], env=env, cwd=cwd
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


def create(subject: str, predicate: str, value: str, **extra: str) -> dict[str, str]:
    return dict(subject=subject, predicate=predicate, value=value, request_id=str(uuid4()), **extra)


async def test_agents_share_project_memory_with_attribution_and_isolation(
    _disposable_test_db, clean_memory
):
    namespace = "shared-" + uuid4().hex
    claude_env = server_env(_disposable_test_db, namespace, "claude-code", WIDGET)
    codex_env = server_env(_disposable_test_db, namespace, "codex", WIDGET)
    other_env = server_env(_disposable_test_db, namespace, "codex", "github.com/acme/other")
    async with asyncio.timeout(60):
        async with (
            agent(claude_env) as claude,
            agent(codex_env) as codex,
            agent(other_env) as other,
        ):
            created = payload(
                await claude.call_tool(
                    "memory_create", create("project", "test_command", "make test-db")
                )
            )
            assert created["scope"] == "project"
            fact_id = created["fact_id"]

            hits = payload(await codex.call_tool("memory_search", {"query": "test_command"}))
            assert [(h["fact_id"], h["scope"]) for h in hits["hits"]] == [(fact_id, "project")]
            current = payload(await codex.call_tool("memory_get", {"fact_id": fact_id}))
            assert current["value"] == "make test-db" and current["scope"] == "project"
            changed = payload(
                await codex.call_tool(
                    "memory_update",
                    {
                        "fact_id": fact_id,
                        "value": "make test",
                        "expected_event_id": current["current_event_id"],
                        "request_id": str(uuid4()),
                    },
                )
            )
            assert changed["status"] == "applied" and changed["scope"] == "project"

            history = payload(await claude.call_tool("memory_history", {"fact_id": fact_id}))
            assert [(e["op"], e["actor"]) for e in history["entries"]] == [
                ("UPDATE", "codex"),
                ("ADD", "claude-code"),
            ]

            # Another project neither finds nor addresses this project's memory.
            assert payload(await other.call_tool("memory_search", {"query": "test_command"})) == {
                "hits": []
            }
            payload(await other.call_tool("memory_get", {"fact_id": fact_id}), code="NOT_FOUND")
            payload(await other.call_tool("memory_history", {"fact_id": fact_id}), code="NOT_FOUND")

            # Global memories are visible from every project.
            shell = payload(
                await claude.call_tool(
                    "memory_create", create("user", "preferred_shell", "zsh", scope="global")
                )
            )
            assert shell["scope"] == "global"
            hits = payload(await other.call_tool("memory_search", {"query": "preferred_shell"}))
            assert [(h["fact_id"], h["scope"]) for h in hits["hits"]] == [
                (shell["fact_id"], "global")
            ]
            got = payload(await other.call_tool("memory_get", {"fact_id": shell["fact_id"]}))
            assert got["scope"] == "global" and got["value"] == "zsh"


async def test_project_comes_from_the_server_working_directory(
    _disposable_test_db, clean_memory, tmp_path
):
    namespace = "shared-cwd-" + uuid4().hex
    repo = tmp_path / "widget"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/Acme/Widget.git"],
        cwd=repo,
        check=True,
    )
    plain = tmp_path / "scratch"
    plain.mkdir()
    in_repo = server_env(_disposable_test_db, namespace, "claude-code", None)
    async with asyncio.timeout(60):
        async with (
            agent(in_repo, cwd=repo / "src") as claude,
            agent(server_env(_disposable_test_db, namespace, "codex", WIDGET)) as codex,
            agent(server_env(_disposable_test_db, namespace, "codex", None), cwd=plain) as outside,
        ):
            created = payload(
                await claude.call_tool("memory_create", create("project", "linter", "ruff"))
            )
            assert created["scope"] == "project"
            hits = payload(await codex.call_tool("memory_search", {"query": "linter"}))
            assert [h["fact_id"] for h in hits["hits"]] == [created["fact_id"]]

            # Outside any repository only the global scope exists.
            note = payload(
                await outside.call_tool("memory_create", create("user", "editor", "helix"))
            )
            assert note["scope"] == "global"
            payload(
                await outside.call_tool(
                    "memory_create", create("user", "editor", "vim", scope="project")
                ),
                code="INVALID_INPUT",
            )
            assert payload(await outside.call_tool("memory_search", {"query": "linter"})) == {
                "hits": []
            }


async def test_actor_defaults_to_agent_id_when_unset(_disposable_test_db, clean_memory):
    env = server_env(_disposable_test_db, "shared-legacy-" + uuid4().hex, "unused", WIDGET)
    env.pop("MNEMO_ACTOR")
    env["MNEMO_AGENT_ID"] = "legacy-agent"
    async with asyncio.timeout(30):
        async with agent(env) as client:
            created = payload(
                await client.call_tool("memory_create", create("project", "ci", "github"))
            )
            history = payload(
                await client.call_tool("memory_history", {"fact_id": created["fact_id"]})
            )
            assert [e["actor"] for e in history["entries"]] == ["legacy-agent"]
