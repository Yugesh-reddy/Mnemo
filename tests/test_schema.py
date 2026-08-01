"""Schema tests against a real store file: migration, the HEAD view, and the guards
that make events and receipts append-only."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from uuid import uuid4

import pytest

from mnemo import db as mdb
from mnemo.seed import seed
from mnemo.sqlite import Connection, StoreUnavailable, connect

TABLES = {
    "memory_fact",
    "memory_event",
    "memory_commit",
    "memory_mutation_receipt",
    "memory_embedding",
    "memory_search_text",
    "schema_migrations",
}

# Every column of memory_event except the bookkeeping that may change.
PAYLOAD_COLUMNS = {
    "seq": 99999,
    "event_id": str(uuid4()),
    "op": "DELETE",
    "object_text": "changed",
    "object_number": "7",
    "object_json": '{"x": 1}',
    "provenance": "document",
    "actor": "someone-else",
    "confidence": 0.5,
    "trust_level": "low",
    "source_span": '{"doc": "x"}',
    "valid_from": "2000-01-01T00:00:00.000000Z",
    "valid_to": "2000-01-01T00:00:00.000000Z",
    "recorded_at": "2000-01-01T00:00:00.000000Z",
    "parent_event_id": None,  # set per test to another real event
    "commit_id": None,  # set per test to a real commit
    "importance": 9,
    "write_score": 0.1,
    "tier": "ephemeral",
    "reason": "rewritten",
    "session_id": "s",
    "expires_at": "2000-01-01T00:00:00.000000Z",
    "fact_id": None,  # set per test to another real fact
}
BOOKKEEPING = {"superseded_at", "superseded_by", "strength", "recall_count", "last_used"}


async def test_a_new_file_is_private_migrated_once_and_in_wal_mode(tmp_path) -> None:
    path = tmp_path / "home" / ".mnemo" / "mnemo.db"
    conn = await connect(path)
    try:
        assert await mdb.apply_migrations(conn) == mdb.migration_names()
        assert await mdb.apply_migrations(conn) == []
        await mdb.check_schema(conn)
        assert await conn.fetchval("PRAGMA journal_mode") == "wal"
        assert await conn.fetchval("PRAGMA foreign_keys") == 1
        assert await conn.fetchval("SELECT vec_version()")
    finally:
        await conn.close()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_an_existing_store_parent_directory_is_not_chmodded(tmp_path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    path = existing / "mnemo.db"
    conn = await connect(path)
    try:
        assert await mdb.apply_migrations(conn)
    finally:
        await conn.close()
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_check_schema_names_the_fix(tmp_path) -> None:
    conn = await connect(tmp_path / "empty.db")
    try:
        with pytest.raises(StoreUnavailable, match="mnemo install"):
            await mdb.check_schema(conn)
        await mdb.apply_migrations(conn)
        await conn.execute("INSERT INTO schema_migrations (filename) VALUES ('9999_future.sql')")
        with pytest.raises(StoreUnavailable, match="newer Mnemo"):
            await mdb.check_schema(conn)
    finally:
        await conn.close()
    with pytest.raises(StoreUnavailable, match="no Mnemo store"):
        await connect(tmp_path / "missing.db", create=False)


async def test_a_file_that_is_not_a_database_is_refused(tmp_path) -> None:
    path = tmp_path / "notes.db"
    path.write_text("not a database " * 100)
    with pytest.raises(StoreUnavailable, match="not a readable Mnemo store"):
        await connect(path)


async def test_tables_exist_and_no_pipeline_tables(db: Connection) -> None:
    names = {r["name"] for r in await db.fetch("SELECT name FROM sqlite_master")}
    assert TABLES <= names
    assert not names & {"fast_cache", "extraction_job", "quality_decision"}


async def test_memory_current_returns_seeded_facts(db: Connection) -> None:
    assert await seed(db) == 3
    rows = await db.fetch(
        "SELECT subject, predicate, object_text, provenance, trust_level "
        "FROM memory_current ORDER BY predicate"
    )
    values = {(r["subject"], r["predicate"]): r["object_text"] for r in rows}
    assert values[("user", "preferred_database")] == "PostgreSQL"
    assert values[("user", "name")] == "Yugesh"
    pg = next(r for r in rows if r["predicate"] == "preferred_database")
    assert (pg["provenance"], pg["trust_level"]) == ("direct_user_statement", "high")


async def test_seed_is_idempotent(db: Connection) -> None:
    assert await seed(db) == 3
    assert await seed(db) == 0
    assert await db.fetchval("SELECT count(*) FROM memory_current") == 3


async def test_snapshot_as_of_reflects_seq(store, db: Connection) -> None:
    await seed(db)
    max_seq = await db.fetchval("SELECT max(seq) FROM memory_event")
    assert len(await store._snapshot_as_of(max_seq)) == 3
    assert await store._snapshot_as_of(0) == []


async def test_view_has_the_decay_and_identity_columns(db: Connection) -> None:
    cols = {r["name"] for r in await db.fetch("PRAGMA table_info(memory_current)")}
    assert {"last_used", "valid_to", "reason", "seq", "identity_mode", "identity_ref"} <= cols


async def test_expired_valid_to_hides_fact_from_head(store, db: Connection) -> None:
    ev = await store.add("user", "location", "Austin", provenance="direct_user_statement")
    await store.invalidate(ev.fact_id)
    assert (
        await db.fetchval("SELECT count(*) FROM memory_current WHERE fact_id=?1", ev.fact_id) == 0
    )


@pytest.mark.parametrize("predicate", ["preferred_database", "name", "preferred_language"])
async def test_each_seed_fact_has_exactly_one_live_event(db: Connection, predicate: str) -> None:
    await seed(db)
    live = await db.fetchval(
        """
        SELECT count(*) FROM memory_event e
        JOIN memory_fact f ON f.fact_id = e.fact_id
        WHERE f.predicate = ?1 AND e.superseded_at IS NULL
        """,
        predicate,
    )
    assert live == 1


async def test_timestamps_are_stored_in_one_fixed_width_form(store, db: Connection) -> None:
    first = await store.add("user", "editor", "vim")
    await store.add("user", "editor", "neovim")
    await store.commit("checkpoint")
    form = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")
    rows = await db.fetch(
        "SELECT valid_from AS a, recorded_at AS b, last_used AS c, superseded_at AS d "
        "FROM memory_event"
    )
    raw = [v for row in rows for v in row.values() if v is not None]
    raw += [r["t"] for r in await db.fetch("SELECT created_at AS t FROM memory_fact")]
    raw += [r["t"] for r in await db.fetch("SELECT created_at AS t FROM memory_commit")]
    assert len(raw) >= 8 and all(form.fullmatch(v) for v in raw), raw
    # One transaction, one clock reading: superseding the first happened when its
    # replacement was recorded.
    old = await db.fetchval(
        "SELECT superseded_at AS t FROM memory_event WHERE event_id=?1", first.event_id
    )
    assert old == await db.fetchval("SELECT max(recorded_at) AS t FROM memory_event")


async def test_a_memory_is_visible_the_moment_it_is_written(store) -> None:
    for i in range(50):
        event = await store.add("user", f"fact {i}", f"value {i}")
        assert (await store.get(event.fact_id)).event_id == event.event_id


# ---- append-only guards ---------------------------------------------------------


@pytest.mark.parametrize("column", sorted(PAYLOAD_COLUMNS))
async def test_every_payload_column_is_immutable(store, db: Connection, column: str) -> None:
    event = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    other = await store.add("user", "city", "Austin")
    commit = await store.commit("c")
    value = {
        "parent_event_id": str(other.event_id),
        "commit_id": str(commit.commit_id),
        "fact_id": str(other.fact_id),
    }.get(column, PAYLOAD_COLUMNS[column])
    with pytest.raises(sqlite3.IntegrityError, match="payload is immutable"):
        async with db.transaction():
            await db.execute(
                f"UPDATE memory_event SET {column}=?1 WHERE event_id=?2", value, event.event_id
            )


async def test_bookkeeping_columns_may_change(store, db: Connection) -> None:
    first = await store.add("user", "name", "Sai")
    second = await store.add("user", "name", "Ari")
    columns = {r["name"] for r in await db.fetch("PRAGMA table_info(memory_event)")}
    assert columns == set(PAYLOAD_COLUMNS) | BOOKKEEPING
    await db.execute(
        "UPDATE memory_event SET strength=strength+1, recall_count=recall_count+1, "
        "last_used=now(), superseded_at=now(), superseded_by=?2 WHERE event_id=?1",
        first.event_id,
        second.event_id,
    )


async def test_events_cannot_be_deleted_or_replaced(store, db: Connection) -> None:
    event = await store.add("user", "name", "Sai")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        async with db.transaction():
            await db.execute("DELETE FROM memory_event WHERE event_id=?1", event.event_id)
    for conflict in ("seq", "event_id"):
        with pytest.raises(sqlite3.IntegrityError, match="payload is immutable"):
            async with db.transaction():
                await db.execute(
                    "INSERT OR REPLACE INTO memory_event "
                    "(seq, event_id, fact_id, op, object_text, provenance) "
                    "VALUES (?1, ?2, ?3, 'ADD', 'planted', 'agent_inference')",
                    event.seq if conflict == "seq" else None,
                    event.event_id if conflict == "event_id" else uuid4(),
                    event.fact_id,
                )
    assert (await store.get(event.fact_id)).value == "Sai"


async def test_mutation_receipts_are_scoped_unique_and_immutable(store, db) -> None:
    event = await store.add("user", "preferred_database", "PostgreSQL")
    request_id = uuid4()
    insert = """
        INSERT {mode} INTO memory_mutation_receipt
            (receipt_id, namespace, user_id, agent_id, request_id, operation,
             request_payload, status, fact_id, event_id, result)
        VALUES (?5, ?1, 'default', 'default', ?2, 'create', '{{}}', 'applied', ?3, ?4, '{{}}')
    """
    await db.execute(
        insert.format(mode=""), "default", request_id, event.fact_id, event.event_id, uuid4()
    )
    for mode in ("", "OR REPLACE"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            async with db.transaction():
                await db.execute(
                    insert.format(mode=mode),
                    "default",
                    request_id,
                    event.fact_id,
                    event.event_id,
                    uuid4(),
                )
    await db.execute(
        insert.format(mode=""), "other", request_id, event.fact_id, event.event_id, uuid4()
    )
    for statement in (
        "UPDATE memory_mutation_receipt SET result='{}'",
        "DELETE FROM memory_mutation_receipt",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            async with db.transaction():
                await db.execute(statement)
    assert await db.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 2


async def test_every_event_is_indexed_for_words(store, db: Connection) -> None:
    first = await store.add("deploy", "command", "make release")
    await store.add("deploy", "command", "make ship")
    rows = await db.fetch(
        "SELECT rowid FROM memory_search_text WHERE memory_search_text MATCH 'release'"
    )
    assert [r["rowid"] for r in rows] == [first.seq]
