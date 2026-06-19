"""M1 schema tests (against the live disposable DB).

Acceptance (spec §12 M1): migrations apply cleanly; the view returns seeded facts.
"""

from __future__ import annotations

import shutil
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from mnemo import db as mdb
from mnemo.seed import seed

CORE_TABLES = [
    "memory_fact",
    "memory_event",
    "memory_commit",
    "fast_cache",
    "extraction_job",
    "schema_migrations",
]


async def test_connect_and_migrate_on_database_without_vector_extension(
    _disposable_test_db: str,
) -> None:
    """The public connection path must work before migrations install pgvector."""
    parts = urlsplit(_disposable_test_db)
    name = "mnemo_fresh_" + uuid4().hex[:12]
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = urlunsplit(parts._replace(path="/" + name))
        conn = await mdb.connect(dsn)
        try:
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')"
            )
            applied = await mdb.apply_migrations(conn)
            assert applied[0] == "0001_init.sql"
            assert await conn.fetchval("SELECT count(*) FROM memory_fact") == 0
            assert await mdb.apply_migrations(conn) == []
        finally:
            await conn.close()

        # Normal connections still decode vectors after the extension is installed.
        conn = await mdb.connect(dsn)
        try:
            assert await conn.fetchval("SELECT '[1,2,3]'::vector") == [1.0, 2.0, 3.0]
        finally:
            await conn.close()
    finally:
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def test_core_tables_exist(db: asyncpg.Connection) -> None:
    for table in CORE_TABLES:
        reg = await db.fetchval("SELECT to_regclass($1)", f"public.{table}")
        assert reg is not None, f"table {table} missing"


async def test_extensions_installed(db: asyncpg.Connection) -> None:
    names = {r["extname"] for r in await db.fetch("SELECT extname FROM pg_extension")}
    assert "vector" in names
    assert "pgcrypto" in names


async def test_memory_current_returns_seeded_facts(db: asyncpg.Connection) -> None:
    inserted = await seed(db)
    assert inserted == 3  # fresh transaction: all seed facts are new

    rows = await db.fetch(
        "SELECT subject, predicate, object_text, provenance, trust_level "
        "FROM memory_current ORDER BY predicate"
    )
    values = {(r["subject"], r["predicate"]): r["object_text"] for r in rows}
    assert values[("user", "preferred_database")] == "PostgreSQL"
    assert values[("user", "name")] == "Yugesh"

    # provenance/trust survive the round-trip through the view
    pg = next(r for r in rows if r["predicate"] == "preferred_database")
    assert pg["provenance"] == "direct_user_statement"
    assert pg["trust_level"] == "high"


async def test_seed_is_idempotent(db: asyncpg.Connection) -> None:
    assert await seed(db) == 3
    assert await seed(db) == 0  # second run inserts nothing
    count = await db.fetchval("SELECT count(*) FROM memory_current")
    assert count == 3


async def test_fact_state_as_of_reflects_seq(db: asyncpg.Connection) -> None:
    await seed(db)
    max_seq = await db.fetchval("SELECT max(seq) FROM memory_event")

    # As of the latest seq, all three facts are present.
    rows_now = await db.fetch("SELECT * FROM fact_state_as_of('default', 'default', $1)", max_seq)
    assert len(rows_now) == 3

    # As of seq 0 (before anything), no facts exist.
    rows_before = await db.fetch("SELECT * FROM fact_state_as_of('default', 'default', $1)", 0)
    assert len(rows_before) == 0


async def test_memory_entity_dropped_and_view_has_decay_columns(
    db: asyncpg.Connection,
) -> None:
    assert await db.fetchval("SELECT to_regclass('public.memory_entity')") is None
    cols = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='memory_current'"
        )
    }
    assert {"last_used", "valid_to", "reason"} <= cols


async def test_expired_valid_to_hides_fact_from_head(store, db: asyncpg.Connection) -> None:
    ev = await store.add("user", "location", "Austin", provenance="direct_user_statement")
    # World-validity ends through a new event; payload rows cannot be rewritten.
    await store.invalidate(ev.fact_id)
    assert (
        await db.fetchval("SELECT count(*) FROM memory_current WHERE fact_id=$1", ev.fact_id) == 0
    )


@pytest.mark.parametrize("predicate", ["preferred_database", "name", "preferred_language"])
async def test_each_seed_fact_has_exactly_one_live_event(
    db: asyncpg.Connection, predicate: str
) -> None:
    await seed(db)
    # The HEAD invariant: exactly one non-superseded event per active fact.
    live = await db.fetchval(
        """
        SELECT count(*) FROM memory_event e
        JOIN memory_fact f ON f.fact_id = e.fact_id
        WHERE f.predicate = $1 AND e.superseded_at IS NULL
        """,
        predicate,
    )
    assert live == 1


async def test_mutation_receipts_are_scoped_unique_and_immutable(store, db) -> None:
    event = await store.add("user", "preferred_database", "PostgreSQL")
    request_id = uuid4()
    insert = """
        INSERT INTO memory_mutation_receipt
            (namespace, user_id, agent_id, request_id, operation, request_payload,
             status, fact_id, event_id, result)
        VALUES ($1, 'default', 'default', $2, 'create', '{}', 'applied', $3, $4, '{}')
    """
    await db.execute(insert, "default", request_id, event.fact_id, event.event_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with db.transaction():
            await db.execute(insert, "default", request_id, event.fact_id, event.event_id)
    await db.execute(insert, "other", request_id, event.fact_id, event.event_id)
    for statement in (
        "UPDATE memory_mutation_receipt SET result='{}'",
        "DELETE FROM memory_mutation_receipt",
    ):
        with pytest.raises(asyncpg.CheckViolationError, match="append-only") as error:
            async with db.transaction():
                await db.execute(statement)
        assert error.value.sqlstate == "23514"
    assert await db.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 2


async def test_0010_applies_over_populated_0009(db, fake_embedder, tmp_path) -> None:
    from mnemo.core import MnemoStore

    for migration in sorted(mdb.MIGRATIONS_DIR.glob("*.sql")):
        if int(migration.name.split("_")[0]) <= 9:
            shutil.copy(migration, tmp_path / migration.name)
    schema = "mnemo_upgrade_" + uuid4().hex
    await db.execute(f'CREATE SCHEMA "{schema}"')
    await db.execute(f'SET LOCAL search_path TO "{schema}", public')
    # Prevent the public schema's migration bookkeeping from shadowing this one.
    await db.execute(
        "CREATE TABLE schema_migrations (filename text PRIMARY KEY, "
        "applied_at timestamptz NOT NULL DEFAULT now())"
    )
    assert len(await mdb.apply_migrations(db, tmp_path)) == 9
    legacy = MnemoStore(db, fake_embedder)
    await legacy.add("user", "preferred_database", "PostgreSQL")
    await legacy.add("user", "preferred_language", "Python")
    snapshot = "SELECT row_to_json(f)::text FROM memory_fact f ORDER BY fact_id"
    before = await db.fetch(snapshot)
    assert await mdb.apply_migrations(db) == ["0010_mutation_receipts.sql"]
    assert await db.fetch(snapshot) == before
    assert await db.fetchval("SELECT count(*) FROM memory_current") == 2
    assert await db.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 0
