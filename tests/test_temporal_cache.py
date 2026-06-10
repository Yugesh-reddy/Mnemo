"""Temporal visibility and comparable scores across the two retrieval tiers."""

from datetime import UTC, datetime, timedelta

import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore


async def test_session_restriction_and_expiry(db, fake_embedder):
    store = MnemoStore(
        db, fake_embedder, settings=Settings(_env_file=None, session_ttl_seconds=0.02)
    )
    first = await store.add("user", "task", "debugging", tier="session", session_id="a")
    assert await store.search("debugging", session_id="a")
    assert not await store.search("debugging", session_id="b")
    assert not await store.search("debugging")
    # now() is fixed inside this transaction; a separate as-of timestamp exercises TTL.
    later = first.expires_at + timedelta(seconds=1)
    assert not await store.search("debugging", session_id="a", as_of=later)


async def test_future_validity_and_aware_timestamp_contract(store):
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    event = await store.add("user", "project", "Juniper", valid_from=tomorrow)
    assert await store.get(event.fact_id) is None
    assert not await store.search("Juniper")
    assert await store.search("Juniper", valid_at=tomorrow + timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.search("Juniper", as_of=datetime(2026, 1, 1))


async def test_reasserting_a_future_value_now_creates_a_visible_revision(store):
    first = await store.add(
        "user", "project", "Juniper", valid_from=datetime.now(UTC) + timedelta(days=1)
    )
    current = await store.add("user", "project", "Juniper")
    assert current.event_id != first.event_id
    assert (await store.get(first.fact_id)).value == "Juniper"


async def test_historical_revision_after_correction_and_invalidation(
    worker_connections, fake_embedder
):
    conn, _ = worker_connections
    store = MnemoStore(conn, fake_embedder)
    first = await store.add("user", "location", "Austin")
    before = await conn.fetchval("SELECT clock_timestamp()")
    await store.add("user", "location", "Portland")
    after = await conn.fetchval("SELECT clock_timestamp()")
    await store.invalidate(first.fact_id)
    assert not await store.search("location")
    assert [f.value for f in await store.search("location", as_of=before)] == ["Austin"]
    assert [f.value for f in await store.search("location", as_of=after)] == ["Portland"]
    assert await conn.fetchval("SELECT sum(recall_count) FROM memory_event") == 0


async def test_k_one_raw_cache_is_ranked_before_weaker_semantic(store):
    await store.add("user", "note", "Postgres", importance=1)
    await store.observe("raw", "Postgres", "session")
    results = await store.search("Postgres", k=1, session_id="session")
    assert len(results) == 1 and results[0].source == "fast_cache"


async def test_session_revert_renews_ttl_and_preserves_session(store, db):
    first = await store.add("user", "task", "debug", tier="session", session_id="session")
    await store.archive_if_head(first.fact_id, first.event_id, actor="test", reason="archive")
    restored = await store.revert(first.fact_id, first.event_id)
    assert restored.session_id == "session"
    assert restored.expires_at >= first.expires_at
    assert await store.search("debug", session_id="session")
    assert not await store.search("debug", session_id="other")


async def test_archival_is_removal_in_diff(store):
    first = await store.add("user", "note", "remember")
    before = await store.commit()
    await store.archive_if_head(first.fact_id, first.event_id, actor="test", reason="archive")
    after = await store.commit()
    diff = await store.diff(before, after)
    assert len(diff.entries) == 1 and diff.entries[0].change == "removed"


async def test_recalled_head_cannot_be_archived_from_stale_retention(store):
    first = await store.add("user", "note", "remember")
    await store.reinforce(first.fact_id)
    result = await store.archive_if_head(
        first.fact_id,
        first.event_id,
        actor="test",
        reason="stale",
        expected_last_used=first.last_used - timedelta(seconds=1),
    )
    assert result is None


async def test_legacy_session_expiry_is_a_read_overlay(store, db):
    fact_id = await store._insert_fact("user", "task", "user|task", "triple", "legacy")
    old = datetime.now(UTC) - timedelta(days=2)
    event = await db.fetchrow(
        "INSERT INTO memory_event(fact_id,op,object_text,provenance,tier,valid_from,recorded_at) "
        "VALUES($1,'ADD','legacy task','agent_inference','session',$2,$2) RETURNING event_id,seq",
        fact_id,
        old,
    )
    await store._set_head(fact_id, event["event_id"])
    assert await store.get(fact_id) is None
    assert not await store.search("legacy task", session_id="legacy")
    historical = await store.search(
        "legacy task", session_id="legacy", as_of=old + timedelta(hours=1)
    )
    assert len(historical) == 1
    snapshot = await db.fetchrow(
        "SELECT * FROM fact_snapshot_as_of('default','default','default',$1)", event["seq"]
    )
    assert snapshot["expires_at"] == old + timedelta(hours=24)
    assert snapshot["session_id"] == "legacy"
    # The compatibility rule does not backfill or mutate the original event.
    assert (
        await db.fetchval(
            "SELECT expires_at FROM memory_event WHERE event_id=$1", event["event_id"]
        )
        is None
    )
