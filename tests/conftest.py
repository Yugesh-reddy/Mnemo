"""Shared test fixtures.

Two principles for the suite:
- **Hermetic by default.** Embeddings use a deterministic FakeEmbedder — no model,
  no network — so the canonical lifecycle/diff tests are fast and free.
- **Disposable DB.** Each invocation creates, migrates and later drops its own
  uniquely named database; it never drops the configured database. Each test uses
  a rolled-back transaction where possible. Unavailable Postgres fails CI and
  MNEMO_REQUIRE_DB=1 runs; optional local runs may skip database tests.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from mnemo.config import get_settings
from mnemo.db import apply_migrations
from mnemo.embedder import HashEmbedder as FakeEmbedder


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(get_settings().embed_dim)


def _split_dsn(dsn: str) -> tuple[str, str]:
    """Return (admin_dsn pointing at the 'postgres' maintenance db, test_db_name)."""
    parts = urlsplit(dsn)
    db_name = parts.path.lstrip("/")
    admin_dsn = urlunsplit(parts._replace(path="/postgres"))
    return admin_dsn, db_name


@pytest.fixture(scope="session")
def _disposable_test_db() -> Iterator[str]:
    """Create an invocation-owned test DB and apply migrations once per session.

    Runs its own event loop so it composes with function-scoped async fixtures.
    Fails required database runs, or skips optional local runs, if unavailable.
    """

    async def _setup() -> str:
        configured = get_settings().test_dsn
        admin_dsn, prefix = _split_dsn(configured)
        if not re.fullmatch(r"[a-zA-Z0-9_]+", prefix):
            raise ValueError(
                "test database name must contain only letters, numbers and underscores"
            )
        # Never drop a configured database. Each pytest invocation owns a new one.
        db_name = prefix[:35] + "_" + uuid4().hex[:12]
        dsn = urlunsplit(urlsplit(configured)._replace(path="/" + db_name))
        try:
            admin = await asyncpg.connect(admin_dsn, timeout=5)
        except (OSError, asyncpg.PostgresError) as exc:
            if os.environ.get("CI") or os.environ.get("MNEMO_REQUIRE_DB") == "1":
                pytest.fail(f"Required Postgres integration database unavailable: {exc!s}")
            pytest.skip(f"Postgres not available ({exc!s}); run `make up` first.")
        try:
            await admin.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(dsn)
        try:
            await apply_migrations(conn)
        finally:
            await conn.close()
        return dsn

    dsn = asyncio.run(_setup())
    try:
        yield dsn
    finally:

        async def cleanup() -> None:
            admin_dsn, db_name = _split_dsn(dsn)
            admin = await asyncpg.connect(admin_dsn, timeout=5)
            try:
                await admin.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
            finally:
                await admin.close()

        asyncio.run(cleanup())


@pytest.fixture
async def db(_disposable_test_db: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection to the migrated test DB, wrapped in a rolled-back transaction."""
    from mnemo.db import register_vector

    conn = await asyncpg.connect(_disposable_test_db)
    await register_vector(conn)  # so SELECT * can decode embedding columns
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def store(db: asyncpg.Connection, fake_embedder: FakeEmbedder):
    """An async MnemoStore bound to the rolled-back test connection."""
    from mnemo.core import MnemoStore  # lazy: core.py may not exist yet during M2 RED

    return MnemoStore(db, fake_embedder)


@pytest.fixture
def clean_memory(_disposable_test_db: str):
    """Teardown that truncates memory tables — for sync-SDK tests that commit for real."""
    yield

    async def _truncate() -> None:
        conn = await asyncpg.connect(_disposable_test_db)
        try:
            await conn.execute(
                "TRUNCATE memory_mutation_receipt, memory_event, memory_fact, memory_commit, "
                "fast_cache, extraction_job RESTART IDENTITY CASCADE"
            )
        finally:
            await conn.close()

    asyncio.run(_truncate())


@pytest.fixture
async def worker_connections(_disposable_test_db):
    from mnemo.db import drop_isolated_schema, prepare_isolated_schema

    schema = "mnemo_worker_" + uuid4().hex
    a = await prepare_isolated_schema(_disposable_test_db, schema)
    b = await asyncpg.connect(_disposable_test_db)
    await b.execute(f'SET search_path TO "{schema}", public')
    try:
        yield a, b
    finally:
        await b.close()
        await drop_isolated_schema(a, schema)
        await a.close()
