"""Verify the actual candidate query retains an index-usable distance ordering."""

import json
from pathlib import Path

from mnemo.core import MnemoStore


async def test_vector_candidate_plan_uses_hnsw(worker_connections, fake_embedder):
    conn, _ = worker_connections
    await conn.execute("""
        INSERT INTO memory_fact(subject,predicate,fact_key)
        SELECT 'person-'||i, 'note', 'person-'||i||'|note' FROM generate_series(1,5000) i
    """)
    tail = ",0" * (fake_embedder.dim - 2)
    await conn.execute(
        """
        INSERT INTO memory_event(fact_id,op,object_text,provenance,embedding)
        SELECT fact_id,'ADD','vector note','agent_inference',
          ('['||cos(row_number() OVER ())||','||sin(row_number() OVER ())||$1||']')::vector
        FROM memory_fact
    """,
        tail,
    )
    await conn.execute("""
        UPDATE memory_fact f SET current_event_id=e.event_id
        FROM memory_event e WHERE e.fact_id=f.fact_id
    """)
    await conn.execute("ANALYZE memory_event")
    await conn.execute("ANALYZE memory_fact")
    plans = []

    class ExplainConnection:
        async def fetch(self, query, *args):
            plan = await conn.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, *args)
            plans.append(json.loads(plan))
            return []

    store = MnemoStore(ExplainConnection(), fake_embedder)
    vector = [1.0] + [0.0] * (fake_embedder.dim - 1)
    from mnemo.db import to_vector_literal

    await store._search_semantic(
        "note", to_vector_literal(vector), "%note%", 64, None, None, None, False
    )
    # Save only when deliberately collecting local evidence; CI asserts the same plan.
    import os

    if os.environ.get("MNEMO_PLAN_OUTPUT"):
        Path(os.environ["MNEMO_PLAN_OUTPUT"]).write_text(json.dumps(plans, indent=2) + "\n")
    assert "idx_event_embed" in json.dumps(plans), json.dumps(plans)


async def test_filtered_hnsw_continues_past_superseded_events(worker_connections, fake_embedder):
    conn, _ = worker_connections
    store = MnemoStore(conn, fake_embedder)
    target = await store.add("user", "value", "current")
    tail = ",0" * (fake_embedder.dim - 2)
    # Closest vectors all belong to historical revisions, not live HEADs.
    await conn.execute(
        "INSERT INTO memory_event(fact_id,op,object_text,provenance,embedding) "
        "SELECT $1,'UPDATE','old','agent_inference',"
        "('[1,'||(i/100000.0)||$2||']')::vector FROM generate_series(1,500) i",
        target.fact_id,
        tail,
    )
    current = await store.add(
        "user", "value", "live-neighbor", embedding=[0.8, 0.6] + [0.0] * (fake_embedder.dim - 2)
    )
    await conn.execute("ANALYZE memory_event")
    await conn.execute("ANALYZE memory_fact")

    class QueryVector:
        def embed(self, text):
            return [1.0] + [0.0] * (fake_embedder.dim - 1)

    # Force the ANN path for this adversarial small fixture. The larger plan test
    # checks that production-shaped SQL chooses HNSW without forcing the planner.
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan=off")
        await conn.execute("SET LOCAL hnsw.ef_search=10")
        found = await MnemoStore(conn, QueryVector()).search(
            "no lexical match", k=1, reinforce=False
        )
    assert [fact.event_id for fact in found] == [current.event_id]
