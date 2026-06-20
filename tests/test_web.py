"""M7: the web UI's critical path — search → blame → revert through HTTP.

Drives the ASGI app with the get_store dependency overridden to the disposable test
connection (rolled back per test), so the whole flow runs without a live pool.
"""

from __future__ import annotations

from html.parser import HTMLParser
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from mnemo.core import MnemoStore
from web.app import app, get_store


class Forms(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self.action: str | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs["action"]
            self.forms[self.action] = {}
        elif tag == "input" and self.action and "name" in attrs:
            self.forms[self.action][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.action = None


@pytest.fixture
async def web_client(store):
    async def override():
        yield store

    app.dependency_overrides[get_store] = override
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


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
    e2 = await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")

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
            action = f"/fact/{e1.fact_id}/revert/{e1.event_id}"
            form = Forms(r.text).forms[action]
            assert form["expected_event_id"] == str(e2.event_id)
            assert UUID(form["request_id"]).version == 4
            r = await client.post(action, data=form, follow_redirects=True)
            assert r.status_code == 200
            assert "REVERT" in r.text

            # HEAD is PostgreSQL again — behavior changed.
            r = await client.get("/")
            assert "PostgreSQL" in r.text
            current = await store.get(e1.fact_id)
            assert current is not None and current.object_text == "PostgreSQL"
    finally:
        app.dependency_overrides.clear()


async def test_stale_revert_shows_new_head_without_writing(store, web_client):
    first = await store.add("user", "database", "PostgreSQL")
    await store.add("user", "database", "MySQL")
    url = f"/fact/{first.fact_id}"
    action = f"{url}/revert/{first.event_id}"
    original = Forms((await web_client.get(url)).text).forms[action]
    latest = await store.add("user", "database", "SQLite")
    response = await web_client.post(action, data=original)
    assert response.status_code == 409
    assert "This memory changed since you loaded the page" in response.text
    assert "SQLite" in response.text and 'role="alert"' in response.text
    assert (await store.get(first.fact_id)).event_id == latest.event_id
    assert len(await store.blame(fact_id=first.fact_id)) == 3
    assert await store.conn.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 0
    refreshed = Forms(response.text).forms[action]
    assert refreshed["expected_event_id"] == str(latest.event_id)
    assert refreshed["request_id"] != original.get("request_id")
    assert (await web_client.post(action, data=refreshed)).status_code == 303
    assert (await store.get(first.fact_id)).value == "PostgreSQL"


async def test_revert_form_retry_preserves_later_head_and_source_trust(store, web_client):
    first = await store.add("user", "database", "PostgreSQL")
    await store.add("user", "database", "MySQL")
    url = f"/fact/{first.fact_id}"
    action = f"{url}/revert/{first.event_id}"
    form = Forms((await web_client.get(url)).text).forms[action]
    assert (await web_client.post(action, data=form)).status_code == 303
    restored = await store.get(first.fact_id)
    assert restored.provenance == "agent_inference" and restored.trust_level == "low"
    assert (await store._get_event(restored.event_id)).actor == "ui"
    latest = await store.add("user", "database", "SQLite")
    assert (await web_client.post(action, data=form)).status_code == 303
    assert (await store.get(first.fact_id)).event_id == latest.event_id
    assert len(await store.blame(fact_id=first.fact_id)) == 4
    assert await store.conn.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 1


async def test_revert_requires_both_guard_fields(store, web_client):
    first = await store.add("user", "database", "PostgreSQL")
    latest = await store.add("user", "database", "MySQL")
    for form in ({}, {"expected_event_id": str(latest.event_id)}, {"request_id": str(uuid4())}):
        response = await web_client.post(
            f"/fact/{first.fact_id}/revert/{first.event_id}", data=form
        )
        assert response.status_code == 422
    assert (await store.get(first.fact_id)).event_id == latest.event_id
    assert len(await store.blame(fact_id=first.fact_id)) == 2


@pytest.mark.parametrize("state", ["session", "invalidated"])
async def test_unrestorable_revisions_have_no_form_and_cannot_be_posted(store, web_client, state):
    if state == "session":
        target = await store.add("user", "database", "PostgreSQL", tier="session", session_id="s")
    else:
        first = await store.add("user", "database", "PostgreSQL")
        target = await store.invalidate(first.fact_id)
    latest = await store.add("user", "database", "MySQL")
    url = f"/fact/{target.fact_id}"
    action = f"{url}/revert/{target.event_id}"
    assert action not in Forms((await web_client.get(url)).text).forms
    before = len(await store.blame(fact_id=target.fact_id))
    response = await web_client.post(
        action,
        data={
            "expected_event_id": str(latest.event_id),
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 409
    assert (await store.get(target.fact_id)).event_id == latest.event_id
    assert len(await store.blame(fact_id=target.fact_id)) == before


async def test_unknown_revert_fact_returns_not_found(web_client):
    response = await web_client.post(
        f"/fact/{uuid4()}/revert/{uuid4()}",
        data={
            "expected_event_id": str(uuid4()),
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 404


async def test_manual_correction_is_explicit_human_review(store, web_client):
    first = await store.add("user", "database", "PostgreSQL")
    response = await web_client.get(f"/fact/{first.fact_id}")
    assert "Manual correction" in response.text
    response = await web_client.post(f"/fact/{first.fact_id}/edit", data={"value": "SQLite"})
    assert response.status_code == 303
    current = await store.get(first.fact_id)
    assert current.value == "SQLite"
    assert current.provenance == "human_review" and current.trust_level == "high"


async def test_operations_explains_a_rejected_memory(db, fake_embedder):
    from mnemo.extraction import ExtractionWorker
    from mnemo.models import ExtractedFact

    class Unsupported:
        def extract(self, text, role):
            return [ExtractedFact(subject="user", predicate="location", object="Portland")]

    store = MnemoStore(db, fake_embedder)
    await store.observe("denial", "I do not live in Portland.", "s")
    await ExtractionWorker(db, fake_embedder, Unsupported()).process_one()
    assert not await store.search("Portland")

    async def override():
        yield store

    app.dependency_overrides[get_store] = override
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get("/operations", params={"turn_id": "denial"})
        assert response.status_code == 200
        assert "rejected" in response.text and "Portland" in response.text
        assert "denial" in response.text and "Queue and archival activity" in response.text
    finally:
        app.dependency_overrides.clear()
