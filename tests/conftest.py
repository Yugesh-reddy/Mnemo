"""Shared test fixtures.

Two principles for the suite:
- **Hermetic by default.** Embeddings use a deterministic FakeEmbedder — no model,
  no network — so the canonical lifecycle/diff tests are fast and free.
- **DB tests skip cleanly** when Postgres isn't up, so `make test` is green on a
  bare checkout. From M1 the `db` fixture applies migrations to a disposable DB.
"""

from __future__ import annotations

import hashlib

import asyncpg
import pytest

from mnemo.config import get_settings


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


@pytest.fixture
async def db() -> asyncpg.Connection:
    """A connection to the disposable test DB; skips if Postgres isn't reachable.

    M1 extends this to create the DB and apply migrations per test session.
    """
    dsn = get_settings().test_dsn
    try:
        conn = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres test DB not available ({exc!s}); run `make up` first.")
    try:
        yield conn
    finally:
        await conn.close()
