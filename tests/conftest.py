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
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

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
def _disposable_test_db() -> str:
    """Drop + recreate the test DB and apply all migrations, once per session.

    Runs its own event loop so it composes with function-scoped async fixtures.
    Skips the whole DB-backed suite if Postgres isn't reachable.
    """

    async def _setup() -> str:
        dsn = get_settings().test_dsn
        admin_dsn, db_name = _split_dsn(dsn)
        try:
            admin = await asyncpg.connect(admin_dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.skip(f"Postgres not available ({exc!s}); run `make up` first.")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(dsn)
        try:
            await apply_migrations(conn)
        finally:
            await conn.close()
        return dsn

    return asyncio.run(_setup())


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
