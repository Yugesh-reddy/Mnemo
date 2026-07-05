"""When Postgres or the embedding service is missing, agents get an error that says how to
fix it, and the server keeps running so memory works once the service is back."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from mnemo.errors import ErrorCode, MnemoError
from tests.test_mcp_shared_scope_stdio import WIDGET, agent, payload, server_env

CREATE = {"subject": "project", "predicate": "linter", "value": "ruff"}


def ollama_env(dsn: str, host: str) -> dict[str, str]:
    env = server_env(dsn, "service-errors-" + uuid4().hex, "codex", WIDGET)
    return env | {"MNEMO_BACKEND": "ollama", "MNEMO_OLLAMA_HOST": host}


async def test_ollama_down_names_the_service_and_the_fix(_disposable_test_db, clean_memory):
    async with asyncio.timeout(30):
        async with agent(ollama_env(_disposable_test_db, "http://127.0.0.1:1")) as client:
            error = payload(
                await client.call_tool("memory_create", CREATE), code="SERVICE_UNAVAILABLE"
            )
            assert error["details"]["service"] == "ollama"
            assert "ollama serve" in error["message"] and "127.0.0.1:1" in error["message"]
            # Reads that need no embedding still work.
            payload(
                await client.call_tool("memory_get", {"fact_id": str(uuid4())}), code="NOT_FOUND"
            )


class MissingModel(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = json.dumps({"error": 'model "nomic-embed-text" not found, try pulling it first'})
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args) -> None:
        pass


async def test_missing_embedding_model_says_to_pull_it(_disposable_test_db, clean_memory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), MissingModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        async with asyncio.timeout(30):
            async with agent(ollama_env(_disposable_test_db, host)) as client:
                error = payload(
                    await client.call_tool("memory_search", {"query": "linter"}),
                    code="SERVICE_UNAVAILABLE",
                )
                assert "ollama pull nomic-embed-text" in error["message"]
    finally:
        server.shutdown()


async def test_postgres_down_still_starts_the_server_and_says_make_up(clean_memory):
    env = server_env("postgresql://mnemo:mnemo@127.0.0.1:1/mnemo", "x", "codex", WIDGET)
    async with asyncio.timeout(30):
        async with agent(env) as client:
            assert len((await client.list_tools()).tools) == 6
            error = payload(
                await client.call_tool("memory_search", {"query": "linter"}),
                code="SERVICE_UNAVAILABLE",
            )
            assert error["details"]["service"] == "postgres"
            assert "make up" in error["message"] and "127.0.0.1:1" in error["message"]


async def test_unmigrated_database_says_to_migrate(_disposable_test_db):
    name = "mnemo_unmigrated_" + uuid4().hex[:8]
    parts = urlsplit(_disposable_test_db)
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = urlunsplit(parts._replace(path="/" + name))
        async with asyncio.timeout(30):
            async with agent(server_env(dsn, "x", "codex", WIDGET)) as client:
                error = payload(
                    await client.call_tool("memory_create", CREATE), code="SERVICE_UNAVAILABLE"
                )
                assert "make migrate" in error["message"]
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_the_pool_opens_once_postgres_is_reachable(monkeypatch):
    import mnemo.mcp_direct as srv

    attempts: list[int] = []
    pool = object()

    async def open_pool(settings):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionRefusedError(61, "Connect call failed")
        return pool

    monkeypatch.setattr(srv, "_open_pool", open_pool)
    monkeypatch.setattr(srv, "_pool", None)
    monkeypatch.setattr(srv, "_pool_lock", asyncio.Lock())
    with pytest.raises(MnemoError) as failed:
        await srv._get_pool(srv._settings())
    assert failed.value.code == ErrorCode.SERVICE_UNAVAILABLE
    assert await srv._get_pool(srv._settings()) is pool
    assert await srv._get_pool(srv._settings()) is pool and len(attempts) == 2
