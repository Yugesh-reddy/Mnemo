"""Demo seed data.

Inserts a few facts directly via SQL — fact row + ADD event + HEAD pointer — so
``memory_current`` has demo data without requiring a model server. Idempotent:
re-running skips facts that already exist. Reusable by tests (call ``seed(conn)``
on any connection).
"""

from __future__ import annotations

import asyncio

import asyncpg

from mnemo.db import connect

# (subject, predicate, object_text, provenance, trust_level, confidence)
SEED_FACTS: list[tuple[str, str, str, str, str, float]] = [
    ("user", "preferred_database", "PostgreSQL", "direct_user_statement", "high", 1.0),
    ("user", "name", "Yugesh", "direct_user_statement", "high", 1.0),
    ("user", "preferred_language", "Python", "direct_user_statement", "high", 1.0),
]


def _fact_key(subject: str, predicate: str) -> str:
    """Minimal canonicalization for seed identity (full version lives in core @ M2)."""
    return f"{subject.strip().lower()}|{predicate.strip().lower()}"


async def add_seed_fact(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    obj: str,
    provenance: str,
    trust_level: str,
    confidence: float = 1.0,
) -> str | None:
    """Insert one fact (fact + ADD event + HEAD pointer).

    Returns the new event_id, or None if the fact already existed.
    """
    async with conn.transaction():
        fact_id = await conn.fetchval(
            """
            INSERT INTO memory_fact (subject, predicate, fact_key, kind)
            VALUES ($1, $2, $3, 'triple')
            ON CONFLICT (namespace, user_id, agent_id, fact_key) DO NOTHING
            RETURNING fact_id
            """,
            subject,
            predicate,
            _fact_key(subject, predicate),
        )
        if fact_id is None:
            return None  # already seeded

        event_id = await conn.fetchval(
            """
            INSERT INTO memory_event
                (fact_id, op, object_text, provenance, actor, confidence, trust_level)
            VALUES ($1, 'ADD', $2, $3::mem_provenance, 'seed', $4, $5::mem_trust)
            RETURNING event_id
            """,
            fact_id,
            obj,
            provenance,
            confidence,
            trust_level,
        )
        await conn.execute(
            "UPDATE memory_fact SET current_event_id = $1 WHERE fact_id = $2",
            event_id,
            fact_id,
        )
        return event_id


async def seed(conn: asyncpg.Connection) -> int:
    """Seed all demo facts. Returns the number newly inserted."""
    inserted = 0
    for subject, predicate, obj, provenance, trust, confidence in SEED_FACTS:
        event_id = await add_seed_fact(
            conn,
            subject=subject,
            predicate=predicate,
            obj=obj,
            provenance=provenance,
            trust_level=trust,
            confidence=confidence,
        )
        if event_id is not None:
            inserted += 1
    return inserted


async def _main() -> None:
    conn = await connect()
    try:
        n = await seed(conn)
        print(f"Seeded {n} new fact(s); {len(SEED_FACTS) - n} already present.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
