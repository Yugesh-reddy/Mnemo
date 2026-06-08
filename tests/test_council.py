"""C1: council quality/decay schema + model wiring (spec §3).

Covers the new memory_event fields (importance/write_score/tier/strength/...), the
tier-aware HEAD view (ephemeral hidden but never deleted), and revert restoring the
target's tier/importance.
"""

from __future__ import annotations

import asyncpg


async def test_add_defaults_to_durable_tier(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    assert ev.tier == "durable"
    assert ev.importance == 5
    assert ev.write_score == 1.0
    assert ev.strength == 1.0
    assert ev.recall_count == 0

    row = await db.fetchrow(
        "SELECT tier, importance, write_score, strength, recall_count, last_used "
        "FROM memory_event WHERE event_id=$1",
        ev.event_id,
    )
    assert row["tier"] == "durable"
    assert row["importance"] == 5
    assert row["last_used"] is not None


async def test_ephemeral_is_hidden_from_head_but_not_deleted(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user",
        "weather",
        "72F sunny",
        provenance="agent_inference",
        importance=2,
        write_score=0.2,
        tier="ephemeral",
        reason="below salience floor",
    )
    assert ev.tier == "ephemeral"

    # Ephemeral facts drop out of HEAD (memory_current) -> not in search / list.
    assert await store.list_current() == []
    assert await store.search("weather") == []

    # But the event is never deleted: blame still traces it.
    history = await store.blame(fact_id=ev.fact_id)
    assert [e.op for e in history] == ["ADD"]
    assert history[0].tier == "ephemeral"


async def test_invalidate_hides_fact_reversibly(store, db: asyncpg.Connection) -> None:
    e1 = await store.add("user", "location", "Austin", provenance="direct_user_statement")
    inv = await store.invalidate(e1.fact_id, reason="user moved away")
    assert inv.op == "INVALIDATE"
    assert inv.valid_to is not None
    assert inv.parent_event_id == e1.event_id

    # Hidden from HEAD and search; history fully preserved.
    assert await store.get(e1.fact_id) is None
    assert await store.search("Austin") == []
    assert [e.op for e in await store.blame(fact_id=e1.fact_id)] == ["ADD", "INVALIDATE"]

    # Reversible: revert to the original ADD restores the fact.
    await store.revert(e1.fact_id, e1.event_id)
    restored = await store.get(e1.fact_id)
    assert restored is not None and restored.object_text == "Austin"


async def test_revert_restores_target_tier_and_importance(store) -> None:
    e1 = await store.add(
        "user",
        "preferred_database",
        "PostgreSQL",
        provenance="direct_user_statement",
        importance=8,
    )
    await store.add(
        "user",
        "preferred_database",
        "MongoDB",
        provenance="agent_inference",
        importance=4,
    )
    e3 = await store.revert(e1.fact_id, e1.event_id)
    assert e3.op == "REVERT"
    assert e3.importance == 8  # restored from the target event, not defaulted
    assert e3.tier == e1.tier
