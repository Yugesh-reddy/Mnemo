"""C4: hybrid retrieval — FTS stemming, composite rerank, auto-reinforcement."""

from __future__ import annotations

import asyncpg


async def test_fts_stemming_matches_inflected_query(store) -> None:
    # ILIKE '%languages%' can never match 'preferred_language Python';
    # english FTS stems languages -> languag and finds it.
    await store.add("user", "preferred_language", "Python", provenance="direct_user_statement")
    results = await store.search("what languages")
    assert any(f.predicate == "preferred_language" for f in results)


async def test_importance_boosts_rank_between_equal_matches(store) -> None:
    await store.add(
        "user",
        "project_alpha_note",
        "uses Postgres",
        provenance="direct_user_statement",
        importance=2,
    )
    await store.add(
        "user",
        "preferred_database",
        "PostgreSQL",
        provenance="direct_user_statement",
        importance=9,
    )
    results = await store.search("postgres")
    assert results[0].predicate == "preferred_database"
    assert results[0].score is not None


async def test_search_reinforces_returned_facts(store, db: asyncpg.Connection) -> None:
    ev = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    await store.search("name")
    row = await db.fetchrow("SELECT recall_count FROM memory_event WHERE event_id=$1", ev.event_id)
    assert row["recall_count"] == 1
