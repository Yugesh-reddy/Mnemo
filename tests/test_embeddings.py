"""M3: similarity-routing thresholds + live Ollama checks.

The routing thresholds (update_sim 0.90, dedup_sim 0.92) are exercised with a
*planted* embedder that returns vectors at exact, known cosines — so the boundaries
are deterministic and hermetic. Two live tests verify the real Ollama embedder and,
crucially, that the demo's Postgres→Mongo change isn't suppressed as a "restatement".
"""

from __future__ import annotations

import math

import asyncpg
import httpx
import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore

DIM = 768


def _planted(*coords: float) -> list[float]:
    """A unit 768-vector with the given leading components (rest zero)."""
    v = list(coords) + [0.0] * (DIM - len(coords))
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _at_cosine(c: float, *, axis: int = 0) -> list[float]:
    """Unit vector whose cosine with the ``axis`` basis vector is exactly ``c``."""
    coords = [0.0] * (axis + 2)
    coords[axis] = c
    coords[axis + 1] = math.sqrt(max(0.0, 1.0 - c * c))
    return _planted(*coords)


class PlantedEmbedder:
    """Returns pre-planted vectors per exact text; far-from-everything otherwise."""

    dim = DIM

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    def embed(self, text: str) -> list[float]:
        if text in self.mapping:
            return self.mapping[text]
        v = [0.0] * DIM
        v[-1] = 1.0  # orthogonal to all planted (low-index) vectors
        return v


def _store(db: asyncpg.Connection, mapping: dict[str, list[float]]) -> MnemoStore:
    # _env_file=None => deterministic default thresholds regardless of any local .env
    return MnemoStore(db, PlantedEmbedder(mapping), settings=Settings(_env_file=None))


# ---- update_sim: same key, restatement vs real change ------------------


async def test_same_key_high_cosine_restatement_is_noop(db: asyncpg.Connection) -> None:
    base = _planted(1.0, 0.0)
    mapping = {
        "user preferred_database PostgreSQL": base,
        "user preferred_database Postgres": _at_cosine(0.95),  # >= update_sim (0.90)
        "user preferred_database MongoDB": _at_cosine(0.30),  # < update_sim
    }
    store = _store(db, mapping)

    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    # "Postgres" differs textually but is semantically near-identical -> no churn.
    e2 = await store.add(
        "user", "preferred_database", "Postgres", provenance="direct_user_statement"
    )
    assert e2.event_id == e1.event_id
    assert await db.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=$1", e1.fact_id) == 1

    # "MongoDB" is a genuinely different value -> UPDATE.
    e3 = await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")
    assert e3.op == "UPDATE"
    assert e3.fact_id == e1.fact_id


# ---- dedup_sim: new key, entity resolution ------------------------------


async def test_new_key_high_cosine_resolves_to_existing_fact(db: asyncpg.Connection) -> None:
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user database_system Postgres": _at_cosine(0.95, axis=2),  # >= dedup_sim (0.92)
    }
    store = _store(db, mapping)

    e1 = await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    assert e1.op == "ADD"

    # Different fact_key, but semantically the same entity -> UPDATE the existing fact.
    e2 = await store.add("user", "database_system", "Postgres", provenance="agent_inference")
    assert e2.op == "UPDATE"
    assert e2.fact_id == e1.fact_id
    assert await db.fetchval("SELECT count(*) FROM memory_fact") == 1


async def test_new_key_low_cosine_creates_separate_fact(db: asyncpg.Connection) -> None:
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user favorite_color blue": _at_cosine(0.10, axis=2),  # < dedup_sim
    }
    store = _store(db, mapping)

    await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    e2 = await store.add("user", "favorite_color", "blue", provenance="direct_user_statement")
    assert e2.op == "ADD"
    assert await db.fetchval("SELECT count(*) FROM memory_fact") == 2


async def test_search_ranks_by_vector_similarity(db: asyncpg.Connection) -> None:
    # Two unrelated facts; a query vector close to one of them returns it first.
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user favorite_color blue": _at_cosine(0.0, axis=4),
        "what database": _at_cosine(0.97, axis=2),  # close to the db_engine fact
    }
    store = _store(db, mapping)
    await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    await store.add("user", "favorite_color", "blue", provenance="direct_user_statement")

    results = await store.search("what database")
    assert results
    assert results[0].predicate == "db_engine"
    # score is a composite (relevance + recency + importance), so assert ordering,
    # not the raw cosine magnitude.
    assert results[0].score is not None
    assert results[0].score == max(f.score for f in results)


# ---- live Ollama --------------------------------------------------------


def _ollama_up() -> bool:
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


@pytest.mark.skipif(not _ollama_up(), reason="Ollama server not running")
def test_ollama_embedder_live_returns_expected_dim() -> None:
    from mnemo.embedder import OllamaEmbedder

    emb = OllamaEmbedder()
    vec = emb.embed("the user prefers PostgreSQL")
    assert len(vec) == DIM
    assert any(x != 0.0 for x in vec)


@pytest.mark.skipif(not _ollama_up(), reason="Ollama server not running")
def test_demo_change_is_not_suppressed_as_restatement() -> None:
    # Guards the §10 demo: if cos(Postgres, Mongo) were >= update_sim, the agent's
    # mis-inferred switch to MongoDB would be silently dropped as a restatement.
    from mnemo.embedder import OllamaEmbedder

    emb = OllamaEmbedder()
    pg = emb.embed("user preferred_database PostgreSQL")
    mongo = emb.embed("user preferred_database MongoDB")
    cos = _cosine(pg, mongo)
    assert (
        cos < Settings(_env_file=None).update_sim
    ), f"cos(Postgres, Mongo)={cos:.3f} >= update_sim — demo UPDATE would be suppressed"
