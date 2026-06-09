"""Foundation integrity regressions that require PostgreSQL transactions."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest

from mnemo.core import MnemoStore
from mnemo.db import register_vector


async def test_foreign_event_and_cross_scope_operations_fail(store, db, fake_embedder) -> None:
    own = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    other = MnemoStore(db, fake_embedder, namespace="other")
    foreign = await other.add("user", "name", "Ari", provenance="direct_user_statement")

    with pytest.raises(ValueError, match="does not belong"):
        await store.revert(own.fact_id, foreign.event_id)
    with pytest.raises(ValueError, match="not found"):
        await other.revert(own.fact_id, own.event_id)
    assert await other.blame(fact_id=own.fact_id) == []
    assert await other.log(fact_id=own.fact_id) == []


async def test_revert_and_archive_preserve_payload_lineage_and_embedding(store, db) -> None:
    vector = [0.0] * store.settings.embed_dim
    vector[0] = 1.0
    source = {"turn_ids": ["turn-json"], "doc": "profile.json"}
    event = await store.add(
        "user",
        "profile",
        {"language": "Python", "level": 4},
        provenance="document",
        source_span=source,
        embedding=vector,
        importance=8,
        write_score=0.83,
    )
    archived = await store.archive_if_head(
        event.fact_id, event.event_id, actor="test", reason="archive regression"
    )
    assert archived is not None
    restored = await store.revert(event.fact_id, event.event_id)

    rows = await db.fetch(
        """SELECT object_json, embedding::text, source_span, importance, write_score
           FROM memory_event WHERE event_id = ANY($1::uuid[]) ORDER BY seq""",
        [event.event_id, archived.event_id, restored.event_id],
    )
    assert len(rows) == 3
    assert all(row["object_json"] == rows[0]["object_json"] for row in rows)
    assert all(row["embedding"] == rows[0]["embedding"] for row in rows)
    assert all(json.loads(row["source_span"]) == source for row in rows)
    assert all(row["importance"] == 8 for row in rows)
    assert all(row["write_score"] == pytest.approx(0.83) for row in rows)


async def test_archive_rechecks_selected_head(store) -> None:
    first = await store.add("user", "project", "alpha", provenance="direct_user_statement")
    await store.add("user", "project", "beta", provenance="direct_user_statement")
    assert (
        await store.archive_if_head(
            first.fact_id, first.event_id, actor="test", reason="stale candidate"
        )
        is None
    )
    current = await store.get(first.fact_id)
    assert current is not None and current.object_text == "beta"


async def test_event_payload_guard_allows_only_bookkeeping(store, db) -> None:
    event = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    with pytest.raises(asyncpg.CheckViolationError, match="payload is immutable"):
        async with db.transaction():
            await db.execute(
                "UPDATE memory_event SET object_text='changed' WHERE event_id=$1", event.event_id
            )
    with pytest.raises(asyncpg.CheckViolationError, match="cannot be deleted"):
        async with db.transaction():
            await db.execute("DELETE FROM memory_event WHERE event_id=$1", event.event_id)
    await db.execute(
        "UPDATE memory_event SET recall_count=recall_count+1 WHERE event_id=$1", event.event_id
    )


async def test_two_connection_updates_form_one_head_chain(
    _disposable_test_db: str, fake_embedder
) -> None:
    namespace = f"race-{uuid4()}"
    conn_a, conn_b = await asyncio.gather(
        asyncpg.connect(_disposable_test_db), asyncpg.connect(_disposable_test_db)
    )
    await asyncio.gather(register_vector(conn_a), register_vector(conn_b))
    try:
        store_a = MnemoStore(conn_a, fake_embedder, namespace=namespace)
        store_b = MnemoStore(conn_b, fake_embedder, namespace=namespace)
        vectors = [[0.0] * store_a.settings.embed_dim for _ in range(3)]
        for index, vector in enumerate(vectors):
            vector[index] = 1.0
        first = await store_a.add(
            "user",
            "favorite_color",
            "blue",
            embedding=vectors[0],
            provenance="direct_user_statement",
        )
        await asyncio.gather(
            store_a.add(
                "user",
                "favorite_color",
                "green",
                embedding=vectors[1],
                provenance="direct_user_statement",
            ),
            store_b.add(
                "user",
                "favorite_color",
                "red",
                embedding=vectors[2],
                provenance="direct_user_statement",
            ),
        )
        rows = await conn_a.fetch(
            "SELECT event_id, parent_event_id, superseded_by FROM memory_event "
            "WHERE fact_id=$1 ORDER BY seq",
            first.fact_id,
        )
        head = await conn_a.fetchval(
            "SELECT current_event_id FROM memory_fact WHERE fact_id=$1", first.fact_id
        )
        assert len(rows) == 3
        assert rows[1]["parent_event_id"] == rows[0]["event_id"]
        assert rows[2]["parent_event_id"] == rows[1]["event_id"]
        assert rows[0]["superseded_by"] == rows[1]["event_id"]
        assert rows[1]["superseded_by"] == rows[2]["event_id"]
        assert head == rows[2]["event_id"]
    finally:
        await asyncio.gather(conn_a.close(), conn_b.close())
