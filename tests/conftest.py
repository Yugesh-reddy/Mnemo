"""Shared test fixtures.

Two principles for the suite:
- **Hermetic by default.** Embeddings use a deterministic FakeEmbedder — no model,
  no network — so the canonical lifecycle/diff tests are fast and free.
- **Disposable DB.** The test database is dropped + recreated and migrated fresh at
  the start of each session, so it always reflects the current migration files. Each
  test runs inside a transaction that is rolled back, for isolation. If Postgres
  isn't reachable, DB tests skip cleanly so `make test` is green on a bare checkout.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from mnemo.config import get_settings
from mnemo.db import apply_migrations


class FakeEmbedder:
    """Deterministic, dependency-free embedder for tests.

    Maps text -> a stable L2-normalized vector of length ``dim``. Identical text
    yields an identical vector (cosine == 1.0); unrelated text yields a near-zero
    cosine. Embedding-similarity *routing* (M3) is tested with hand-built vectors,
    not with this — here we only need determinism and the right dimensionality.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        raw = b""
        i = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{i}:{text}".encode()).digest()
            i += 1
        vals = [
            (int.from_bytes(raw[j * 4 : j * 4 + 4], "big") / 2**31) - 1.0 for j in range(self.dim)
        ]
        norm = sum(v * v for v in vals) ** 0.5 or 1.0
        return [v / norm for v in vals]


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
    """Drop + recreate the test DB and apply all migrations, once per session.

    Runs its own event loop so it composes with function-scoped async fixtures.
    Skips the whole DB-backed suite if Postgres isn't reachable.
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
                "TRUNCATE memory_event, memory_fact, memory_commit, "
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
