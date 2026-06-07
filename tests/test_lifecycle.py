"""CANONICAL test — the proof of Mnemo's core claim (CLAUDE.md, spec §12 M2).

ADD(Postgres) -> UPDATE(Mongo) -> BLAME -> REVERT(Postgres):
  - memory_current = Postgres after the revert
  - exactly 3 events in the log
  - history is immutable: the ADD/UPDATE event *payloads* never change; only the
    HEAD/supersession bookkeeping moves (golden rule #5: supersession is a flag).
"""

from __future__ import annotations

import asyncpg

# Columns that record *what was believed* — these must never change once written.
PAYLOAD_COLS = [
    "event_id",
    "seq",
    "fact_id",
    "op",
    "object_text",
    "object_number",
    "object_json",
    "provenance",
    "actor",
    "confidence",
    "trust_level",
    "source_span",
    "valid_from",
    "recorded_at",
    "parent_event_id",
]


async def test_fact_lifecycle_add_update_blame_revert(store, db: asyncpg.Connection) -> None:
    # --- ADD(Postgres): a direct, high-trust user statement ---
    e1 = await store.add(
        "user",
        "preferred_database",
        "PostgreSQL",
        provenance="direct_user_statement",
        actor="user-1",
    )
    assert e1.op == "ADD"
    assert e1.trust_level == "high"

    # --- UPDATE(Mongo): the agent mis-infers a switch (low trust) ---
    e2 = await store.add(
        "user",
        "preferred_database",
        "MongoDB",
        provenance="agent_inference",
        actor="agent-1",
        confidence=0.4,
    )
    assert e2.op == "UPDATE"
    assert e2.fact_id == e1.fact_id
    assert e2.trust_level == "low"

    # --- BLAME shows the lineage that introduced each belief ---
    history = await store.blame(fact_id=e1.fact_id)
    assert [e.op for e in history] == ["ADD", "UPDATE"]
    assert history[0].provenance == "direct_user_statement"
    assert history[1].provenance == "agent_inference"

    # Snapshot the two historical events right before the revert.
    snap_e1 = dict(await db.fetchrow("SELECT * FROM memory_event WHERE event_id=$1", e1.event_id))
    snap_e2 = dict(await db.fetchrow("SELECT * FROM memory_event WHERE event_id=$1", e2.event_id))

    # --- REVERT to the original Postgres belief ---
    e3 = await store.revert(e1.fact_id, e1.event_id)
    assert e3.op == "REVERT"
    assert e3.object_text == "PostgreSQL"
    assert e3.provenance == "human_review"
    assert e3.trust_level == "high"
    assert e3.parent_event_id == e1.event_id

    # memory_current == Postgres, pointing at the revert event
    cur = await db.fetchrow(
        "SELECT object_text, event_id FROM memory_current WHERE fact_id=$1", e1.fact_id
    )
    assert cur["object_text"] == "PostgreSQL"
    assert cur["event_id"] == e3.event_id

    # exactly 3 events in the log
    log = await store.log(fact_id=e1.fact_id)
    assert len(log) == 3
    assert sorted(e.op for e in log) == ["ADD", "REVERT", "UPDATE"]

    # --- history is immutable ---
    # e1 is entirely untouched by the revert.
    after_e1 = dict(await db.fetchrow("SELECT * FROM memory_event WHERE event_id=$1", e1.event_id))
    assert after_e1 == snap_e1

    # e2's *payload* is unchanged; only its supersession flags moved.
    after_e2 = dict(await db.fetchrow("SELECT * FROM memory_event WHERE event_id=$1", e2.event_id))
    assert {k: after_e2[k] for k in PAYLOAD_COLS} == {k: snap_e2[k] for k in PAYLOAD_COLS}
    assert after_e2["superseded_by"] == e3.event_id
    assert after_e2["superseded_at"] is not None

    # nothing was deleted — still exactly 3 rows for the fact.
    total = await db.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=$1", e1.fact_id)
    assert total == 3
