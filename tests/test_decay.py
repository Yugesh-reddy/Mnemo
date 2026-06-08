"""C4: Ebbinghaus decay + recall reinforcement (spec §4 Layer 5).

Archival is an appended event (tier=ephemeral), never an in-place tier mutation —
append-only stays sacred, and revert() un-archives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

from mnemo.config import Settings
from mnemo.decay import decay_sweep, retention

S = Settings(_env_file=None)
NOW = datetime(2026, 7, 5, tzinfo=UTC)


def test_retention_decays_over_time_and_importance_slows_it() -> None:
    fresh = retention(
        strength=1.0, importance=5, last_used=NOW, now=NOW, lambda_base=S.decay_lambda_base
    )
    month_old = retention(
        strength=1.0,
        importance=5,
        last_used=NOW - timedelta(days=30),
        now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    month_old_vital = retention(
        strength=1.0,
        importance=10,
        last_used=NOW - timedelta(days=30),
        now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    assert fresh == 1.0
    assert month_old < S.decay_archive_below  # unused mid-importance fact fades out
    assert month_old_vital > month_old  # importance slows decay


def test_strength_from_recall_slows_decay() -> None:
    weak = retention(
        strength=1.0,
        importance=5,
        last_used=NOW - timedelta(days=30),
        now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    reinforced = retention(
        strength=5.0,
        importance=5,
        last_used=NOW - timedelta(days=30),
        now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    assert reinforced > weak


async def test_sweep_archives_faded_fact_reversibly(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user", "old_project", "legacy-api", provenance="direct_user_statement", importance=3
    )
    await db.execute(  # simulate a month of disuse
        "UPDATE memory_event SET last_used = now() - interval '30 days' WHERE event_id=$1",
        ev.event_id,
    )
    archived = await decay_sweep(store)
    assert archived == 1

    # Gone from HEAD, but history shows the archival event and revert restores it.
    assert await store.get(ev.fact_id) is None
    history = await store.blame(fact_id=ev.fact_id)
    assert [e.op for e in history] == ["ADD", "UPDATE"]
    assert history[-1].tier == "ephemeral"
    assert "decay" in history[-1].reason
    await store.revert(ev.fact_id, ev.event_id)
    assert (await store.get(ev.fact_id)).object_text == "legacy-api"


async def test_reinforce_bumps_strength_and_recall(store, db: asyncpg.Connection) -> None:
    ev = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    await store.reinforce(ev.fact_id)
    row = await db.fetchrow(
        "SELECT strength, recall_count FROM memory_event WHERE event_id=$1", ev.event_id
    )
    assert row["strength"] == 2.0
    assert row["recall_count"] == 1
