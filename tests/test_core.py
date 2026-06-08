"""M2 unit/behavior tests for the core ops (keyword-first; vectors arrive in M3)."""

from __future__ import annotations

import asyncpg
import pytest

from mnemo.core import canonicalize, derive_trust

# ---- pure helpers -------------------------------------------------------


@pytest.mark.parametrize(
    "subject, predicate, expected",
    [
        ("user", "preferred_database", "user|preferred_database"),
        ("  User ", "  Preferred   Database ", "user|preferred database"),
        ("user", "favorite_db", "user|preferred_database"),  # alias
        ("USER", "preferred_db", "user|preferred_database"),  # alias
    ],
)
def test_canonicalize(subject: str, predicate: str, expected: str) -> None:
    assert canonicalize(subject, predicate) == expected


@pytest.mark.parametrize(
    "provenance, confidence, expected",
    [
        ("direct_user_statement", 1.0, "high"),
        ("human_review", 1.0, "high"),
        ("tool_output", 1.0, "medium"),
        ("document", 1.0, "medium"),
        ("agent_inference", 1.0, "low"),
        ("agent_inference", 0.3, "low"),
    ],
)
def test_derive_trust(provenance: str, confidence: float, expected: str) -> None:
    assert derive_trust(provenance, confidence) == expected


# ---- add ----------------------------------------------------------------


async def test_add_creates_fact_and_add_event(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    assert ev.op == "ADD"
    assert ev.object_text == "PostgreSQL"
    assert ev.provenance == "direct_user_statement"
    assert ev.trust_level == "high"

    row = await db.fetchrow(
        "SELECT object_text FROM memory_current "
        "WHERE subject='user' AND predicate='preferred_database'"
    )
    assert row["object_text"] == "PostgreSQL"


async def test_add_same_key_different_value_updates(store, db: asyncpg.Connection) -> None:
    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    e2 = await store.add(
        "user", "preferred_database", "MongoDB", provenance="agent_inference", confidence=0.4
    )
    assert e2.op == "UPDATE"
    assert e2.fact_id == e1.fact_id
    assert e2.parent_event_id == e1.event_id
    assert e2.trust_level == "low"

    cur = await db.fetchrow(
        "SELECT object_text, event_id FROM memory_current WHERE fact_id=$1", e1.fact_id
    )
    assert cur["object_text"] == "MongoDB"
    assert cur["event_id"] == e2.event_id

    e1row = await db.fetchrow(
        "SELECT superseded_at, superseded_by FROM memory_event WHERE event_id=$1", e1.event_id
    )
    assert e1row["superseded_at"] is not None
    assert e1row["superseded_by"] == e2.event_id


async def test_add_same_value_is_noop(store, db: asyncpg.Connection) -> None:
    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    e2 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    n = await db.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=$1", e1.fact_id)
    assert n == 1
    assert e2.event_id == e1.event_id


async def test_alias_predicate_updates_same_fact(store) -> None:
    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    e2 = await store.add("user", "favorite_db", "MongoDB", provenance="agent_inference")
    assert e2.op == "UPDATE"
    assert e2.fact_id == e1.fact_id


async def test_add_recovers_from_concurrent_insert_race(store, db: asyncpg.Connection) -> None:
    # The UNIQUE fact_key race: two parallel adds for a brand-new key both miss the
    # existence check, then the loser's INSERT hits the constraint. _insert_fact must
    # catch the UniqueViolationError *outside* its savepoint (asyncpg only rolls a
    # savepoint back when the exception propagates out — catching inside poisons the
    # transaction), return None, and leave the outer transaction healthy so add() can
    # re-fetch and route through the existing-fact path.
    from mnemo.core import canonicalize

    fact_key = canonicalize("user", "favorite_color")

    # First arrival wins the race and creates the fact.
    first = await store._insert_fact("user", "favorite_color", fact_key, "triple", None)
    assert first is not None

    # Second arrival collides on the UNIQUE constraint -> None, no exception, and
    # (critically) the transaction is NOT left aborted.
    second = await store._insert_fact("user", "favorite_color", fact_key, "triple", None)
    assert second is None

    # Proof the transaction stayed usable: a normal query still runs, and add() now
    # sees the existing fact and routes to a write rather than erroring.
    assert await db.fetchval("SELECT count(*) FROM memory_fact WHERE fact_key=$1", fact_key) == 1
    upd = await store.add("user", "favorite_color", "red", provenance="direct_user_statement")
    assert upd.fact_id == first
    assert upd.op in ("ADD", "UPDATE")  # ADD: no prior event seeded; UPDATE otherwise

    # Exactly one fact row survived.
    assert await db.fetchval("SELECT count(*) FROM memory_fact WHERE fact_key=$1", fact_key) == 1


# ---- blame / search -----------------------------------------------------


async def test_blame_returns_full_history_in_order(store) -> None:
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    e2 = await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")

    events = await store.blame(subject="user", predicate="preferred_database")
    assert [e.op for e in events] == ["ADD", "UPDATE"]
    assert [e.object_text for e in events] == ["PostgreSQL", "MongoDB"]

    by_id = await store.blame(fact_id=e2.fact_id)
    assert len(by_id) == 2


async def test_search_keyword_matches_predicate_and_value(store) -> None:
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    await store.add("user", "preferred_language", "Python", provenance="direct_user_statement")

    by_pred = await store.search("database")
    assert "preferred_database" in {f.predicate for f in by_pred}

    by_value = await store.search("PostgreSQL")
    assert any(f.object_text == "PostgreSQL" for f in by_value)


async def test_search_returns_only_head_value(store) -> None:
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")

    # "PostgreSQL" is superseded; HEAD is MongoDB, so a keyword search for the old
    # value returns nothing (search reads memory_current, not history).
    assert await store.search("PostgreSQL") == []
    assert any(f.object_text == "MongoDB" for f in await store.search("MongoDB"))
