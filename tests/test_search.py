"""C4: hybrid retrieval — FTS stemming, composite rerank, auto-reinforcement."""

from __future__ import annotations

from mnemo.sqlite import Connection


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


async def test_search_reinforces_returned_facts(store, db: Connection) -> None:
    ev = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    await store.search("name")
    row = await db.fetchrow("SELECT recall_count FROM memory_event WHERE event_id=?1", ev.event_id)
    assert row["recall_count"] == 1


class FixedEmbedder:
    """Returns the vector registered for a text, so scores can be computed by hand."""

    dim = 3

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def embed(self, text: str) -> list[float]:
        return self.vectors[text]


async def test_score_is_the_documented_formula(db: Connection) -> None:
    from mnemo.core import MnemoStore

    memory, question = [1.0, 0.0, 0.0], [0.6, 0.8, 0.0]
    embedder = FixedEmbedder({"deploy target fly.io": memory, "zzz": question})
    store = MnemoStore(db, embedder)
    await store.add("deploy", "target", "fly.io", importance=7)
    [hit] = await store.search("zzz", reinforce=False)
    s = store.settings
    cosine = 0.6  # memory . question, both unit length
    assert cosine >= s.search_floor
    expected = s.search_w_rel * cosine + s.search_w_rec * 1.0 + s.search_w_imp * 0.7
    assert abs(hit.score - expected) < 1e-4


async def test_word_matches_need_no_vector_and_floor_hides_the_rest(db: Connection) -> None:
    from mnemo.core import MnemoStore

    embedder = FixedEmbedder(
        {"deploy target fly.io": [1.0, 0.0, 0.0], "target": [0.0, 1.0, 0.0], "other": [0, 0, 1.0]}
    )
    store = MnemoStore(db, embedder)
    event = await store.add("deploy", "target", "fly.io")
    await db.execute("DELETE FROM memory_embedding WHERE seq=?1", event.seq)
    assert [f.fact_id for f in await store.search("target", reinforce=False)] == [event.fact_id]
    assert await store.search("other", reinforce=False) == []


async def test_percent_and_underscore_in_a_query_are_literal(store) -> None:
    await store.add("build", "flags", "-O2")
    await store.add("ci", "coverage", "95% required")
    assert await store.search("%", reinforce=False) != []
    assert [f.predicate for f in await store.search("5%", reinforce=False)] == ["coverage"]
    assert await store.search("_", reinforce=False) == []
    assert await store.search('"unbalanced AND (', reinforce=False) == []


async def test_search_stays_in_its_scope(db: Connection, fake_embedder) -> None:
    from mnemo.core import MnemoStore

    mine = MnemoStore(db, fake_embedder, namespace="project:a")
    theirs = MnemoStore(db, fake_embedder, namespace="project:b", user_id="someone")
    await mine.add("tests", "command", "make test")
    await theirs.add("tests", "command", "npm test")
    assert [f.value for f in await mine.search("tests command")] == ["make test"]
    assert [f.value for f in await theirs.search("tests command")] == ["npm test"]


async def test_reindex_restores_vectors_and_the_word_index(db: Connection, fake_embedder) -> None:
    from mnemo.core import MnemoStore, reindex

    store = MnemoStore(db, fake_embedder)
    first = await store.add("deploy", "target", "fly.io")
    await store.add("deploy", "target", "rendering")
    await db.execute("DELETE FROM memory_embedding")
    await db.execute("DELETE FROM memory_search_text")
    # Only the stemmed word index can match "renders" to "rendering".
    assert await store.search("renders", reinforce=False) == []
    assert await reindex(db, fake_embedder) == {"events": 2, "embedded": 2}
    assert await db.fetchval("SELECT count(*) FROM memory_embedding") == 2
    [hit] = await store.search("renders", reinforce=False)
    assert hit.fact_id == first.fact_id
    # A second run finds nothing missing.
    assert await reindex(db, fake_embedder) == {"events": 2, "embedded": 0}


async def test_search_over_5000_memories_in_a_scope_stays_fast(db: Connection) -> None:
    import random
    import time
    from uuid import uuid4

    from mnemo.core import MnemoStore
    from mnemo.embedder import HashEmbedder, model_id, vector_blob

    embedder = HashEmbedder(384)
    rng = random.Random(7)
    async with db.transaction():
        for i in range(5000):
            fact_id, event_id = uuid4(), uuid4()
            await db.execute(
                "INSERT INTO memory_fact (fact_id, subject, predicate, fact_key) "
                "VALUES (?1, ?2, 'note', ?3)",
                fact_id,
                f"topic {i}",
                f"topic {i}|note",
            )
            seq = await db.fetchval(
                "INSERT INTO memory_event (event_id, fact_id, op, object_text, provenance) "
                "VALUES (?1, ?2, 'ADD', ?3, 'agent_inference') RETURNING seq",
                event_id,
                fact_id,
                f"value {i}",
            )
            await db.execute(
                "INSERT INTO memory_embedding VALUES (?1, ?2, 384, ?3)",
                seq,
                model_id(embedder),
                vector_blob([rng.uniform(-1, 1) for _ in range(384)]),
            )
            await db.execute(
                "UPDATE memory_fact SET current_event_id=?1 WHERE fact_id=?2", event_id, fact_id
            )
    store = MnemoStore(db, embedder)
    await store.search("warm up", reinforce=False)  # first query pays for loading pages
    timings = []
    queries = ("which database", "value 42", "deploy target", "topic", "x", "tests", "a b")
    for query in queries:
        started = time.perf_counter()
        await store.search(query, reinforce=False)
        timings.append(time.perf_counter() - started)
    timings.sort()
    print(f"search over 5,000 memories: {[round(t * 1000, 1) for t in timings]} ms")
    assert timings[len(timings) // 2] < 0.05, timings
