"""M2 diff/commit tests: time-travel across commits emits +/~ entries."""

from __future__ import annotations

import asyncpg


async def test_commit_captures_high_water_seq(store, db: asyncpg.Connection) -> None:
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    c = await store.commit("after-pg")
    max_seq = await db.fetchval("SELECT max(seq) FROM memory_event")
    assert c.at_seq == max_seq
    assert c.label == "after-pg"


async def test_commit_chains_parent(store) -> None:
    c0 = await store.commit("c0")
    c1 = await store.commit("c1")
    assert c1.parent_commit_id == c0.commit_id
    assert c0.parent_commit_id is None


async def test_diff_emits_added_then_changed(store) -> None:
    c0 = await store.commit("empty")
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    c1 = await store.commit("pg")
    await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")
    c2 = await store.commit("mongo")

    # c0 -> c1: the fact appeared.
    d01 = await store.diff(c0.commit_id, c1.commit_id)
    added = {(e.predicate, e.change) for e in d01.entries}
    assert ("preferred_database", "added") in added

    # c1 -> c2: the value changed PostgreSQL -> MongoDB.
    d12 = await store.diff(c1.commit_id, c2.commit_id)
    entry = next(e for e in d12.entries if e.predicate == "preferred_database")
    assert entry.change == "changed"
    assert entry.old == "PostgreSQL"
    assert entry.new == "MongoDB"


async def test_diff_after_revert_shows_value_restored(store) -> None:
    await store.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    c_pg = await store.commit("pg")
    e2 = await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")
    await store.revert(e2.fact_id, (await store.blame(fact_id=e2.fact_id))[0].event_id)
    c_back = await store.commit("reverted")

    # Net change pg -> reverted is nothing (back to PostgreSQL).
    d = await store.diff(c_pg.commit_id, c_back.commit_id)
    assert all(e.predicate != "preferred_database" for e in d.entries)
