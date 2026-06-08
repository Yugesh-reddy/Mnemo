"""M7: the web UI's critical path — search → blame → revert through HTTP.

Drives the ASGI app with the get_store dependency overridden to the disposable test
connection (rolled back per test), so the whole flow runs without a live pool.
"""

from __future__ import annotations

import asyncpg
import httpx
from httpx import ASGITransport

from mnemo.core import MnemoStore
from web.app import app, get_store


async def test_list_shows_tier_and_importance(db: asyncpg.Connection, fake_embedder) -> None:
    store = MnemoStore(db, fake_embedder)
    await store.add(
        "user",
        "preferred_database",
        "PostgreSQL",
        provenance="direct_user_statement",
        importance=8,
        tier="durable",
    )

    async def _override():
        yield MnemoStore(db, fake_embedder)

    app.dependency_overrides[get_store] = _override
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/")
            assert "durable" in r.text
            assert "imp 8" in r.text
    finally:
        app.dependency_overrides.clear()


async def test_list_blame_revert_flow(db: asyncpg.Connection, fake_embedder) -> None:
    store = MnemoStore(db, fake_embedder)
    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")

    async def _override():
        yield MnemoStore(db, fake_embedder)

    app.dependency_overrides[get_store] = _override
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # List view shows the current (MongoDB) value.
            r = await client.get("/")
            assert r.status_code == 200
            assert "MongoDB" in r.text

            # Fact detail shows the full blame history.
            r = await client.get(f"/fact/{e1.fact_id}")
            assert "ADD" in r.text and "UPDATE" in r.text
            assert "PostgreSQL" in r.text and "MongoDB" in r.text

            # Revert to the original ADD event.
            r = await client.post(f"/fact/{e1.fact_id}/revert/{e1.event_id}", follow_redirects=True)
            assert r.status_code == 200
            assert "REVERT" in r.text

            # HEAD is PostgreSQL again — behavior changed.
            r = await client.get("/")
            assert "PostgreSQL" in r.text
            current = await store.get(e1.fact_id)
            assert current is not None and current.object_text == "PostgreSQL"
    finally:
        app.dependency_overrides.clear()
