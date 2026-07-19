"""When the store or the embedding service is missing, agents get an error that says how
to fix it, and the server keeps running so memory works once the problem is fixed."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from mnemo.db import apply_migrations
from mnemo.sqlite import connect
from tests.test_mcp_shared_scope_stdio import WIDGET, agent, payload, server_env

CREATE = {"subject": "project", "predicate": "linter", "value": "ruff"}


def ollama_env(db: str, host: str) -> dict[str, str]:
    env = server_env(db, "service-errors-" + uuid4().hex, "codex", WIDGET)
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


async def test_a_missing_store_says_to_install_and_works_once_it_exists(tmp_path):
    path = tmp_path / "home" / ".mnemo" / "mnemo.db"
    env = server_env(str(path), "x", "codex", WIDGET)
    async with asyncio.timeout(30):
        async with agent(env) as client:
            assert len((await client.list_tools()).tools) == 7
            error = payload(
                await client.call_tool("memory_search", {"query": "linter"}),
                code="SERVICE_UNAVAILABLE",
            )
            assert error["details"]["service"] == "store"
            assert "mnemo install" in error["message"] and str(path) in error["message"]
            assert not path.exists()  # a tool call never creates the store

            conn = await connect(path)
            try:
                await apply_migrations(conn)
            finally:
                await conn.close()
            created = payload(await client.call_tool("memory_create", CREATE))
            assert created["status"] == "applied"


async def test_an_unmigrated_store_says_to_install(tmp_path):
    path = tmp_path / "mnemo.db"
    (await connect(path)).raw.close()
    async with asyncio.timeout(30):
        async with agent(server_env(str(path), "x", "codex", WIDGET)) as client:
            error = payload(
                await client.call_tool("memory_create", CREATE), code="SERVICE_UNAVAILABLE"
            )
            assert "mnemo install" in error["message"]


async def test_a_store_from_a_newer_mnemo_says_to_upgrade(_disposable_test_db):
    conn = await connect(_disposable_test_db)
    try:
        await conn.execute("INSERT INTO schema_migrations (filename) VALUES ('9999_next.sql')")
    finally:
        await conn.close()
    async with asyncio.timeout(30):
        async with agent(server_env(_disposable_test_db, "x", "codex", WIDGET)) as client:
            error = payload(
                await client.call_tool("memory_create", CREATE), code="SERVICE_UNAVAILABLE"
            )
            assert "newer Mnemo" in error["message"]
