"""CANONICAL two-tier handshake test (CLAUDE.md, spec §8).

After observe(), search() returns the fact *before* extraction completes (immediate
recall from fast_cache); after the worker reconciles, the fact appears **exactly
once** (semantic), never twice (raw + semantic). Double-counting is the bug to prevent.
"""

from __future__ import annotations

import json

import asyncpg
import httpx
import pytest

from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact

SESSION = "sess-1"


class StubExtractor:
    """Deterministic extractor (no LLM): returns fixed facts, or detects 'postgres'."""

    def __init__(self, facts: list[ExtractedFact] | None = None) -> None:
        self._facts = facts

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        if self._facts is not None:
            return list(self._facts)
        out: list[ExtractedFact] = []
        if "postgres" in text.lower():
            out.append(
                ExtractedFact(
                    subject="user",
                    predicate="preferred_database",
                    object="PostgreSQL",
                    confidence=0.97,
                    assertion_type="direct_user_statement",
                )
            )
        return out


async def test_immediate_recall_then_exactly_once_after_reconcile(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    worker = ExtractionWorker(db, store.embedder, StubExtractor())

    await store.observe("t1", "I use Postgres for my project.", SESSION, role="user")

    # Before extraction: the cached turn is immediately recallable (no stale read).
    before = await store.search("Postgres", session_id=SESSION)
    assert len(before) == 1
    assert before[0].source == "fast_cache"
    assert before[0].raw_text and "Postgres" in before[0].raw_text
    assert await db.fetchval("SELECT count(*) FROM memory_current") == 0

    # Worker: extract -> reconcile -> handshake.
    assert await worker.process_one() is True

    # After reconcile: exactly one result, now the semantic fact (never raw+semantic).
    after = await store.search("Postgres", session_id=SESSION)
    assert len(after) == 1
    assert after[0].source == "semantic"
    assert after[0].object_text == "PostgreSQL"

    # Handshake fired and the job is done.
    assert await db.fetchval(
        "SELECT reconciled FROM fast_cache WHERE turn_id='t1' AND session_id=$1", SESSION
    )
    assert await db.fetchval("SELECT status FROM extraction_job WHERE turn_id='t1'") == "done"


async def test_no_double_count_without_session_filter(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    worker = ExtractionWorker(db, store.embedder, StubExtractor())
    await store.observe("t1", "I use Postgres.", SESSION)
    await worker.process_one()

    # A plain (sessionless) search sees the single semantic fact.
    results = await store.search("Postgres")
    assert len(results) == 1
    assert results[0].source == "semantic"


async def test_worker_drops_low_confidence(store: MnemoStore, db: asyncpg.Connection) -> None:
    weak = [
        ExtractedFact(
            subject="user",
            predicate="preferred_database",
            object="PostgreSQL",
            confidence=0.30,  # below the 0.5 floor
            assertion_type="agent_inference",
        )
    ]
    worker = ExtractionWorker(db, store.embedder, StubExtractor(weak))
    await store.observe("t1", "maybe postgres?", SESSION)
    await worker.process_one()

    assert await db.fetchval("SELECT count(*) FROM memory_current") == 0  # dropped
    # The turn is still reconciled (processed), just with nothing to store.
    assert await db.fetchval("SELECT reconciled FROM fast_cache WHERE turn_id='t1'")


async def test_worker_empty_queue_returns_false(store: MnemoStore, db: asyncpg.Connection) -> None:
    worker = ExtractionWorker(db, store.embedder, StubExtractor())
    assert await worker.process_one() is False


async def test_extracted_provenance_and_trust(store: MnemoStore, db: asyncpg.Connection) -> None:
    worker = ExtractionWorker(db, store.embedder, StubExtractor())
    await store.observe("t1", "I use Postgres.", SESSION)
    await worker.process_one()

    row = await db.fetchrow(
        "SELECT mc.provenance, mc.trust_level, mc.actor, e.source_span "
        "FROM memory_current mc JOIN memory_event e ON e.event_id = mc.event_id "
        "WHERE mc.predicate='preferred_database'"
    )
    assert row["provenance"] == "direct_user_statement"
    assert row["trust_level"] == "high"
    assert row["actor"] == "extractor"
    span = row["source_span"]
    assert json.loads(span) == {"turn_ids": ["t1"]}


# ---- live extraction (real instruct model) ------------------------------


def _llama_present(prefix: str = "llama3.2") -> bool:
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return any(prefix in m.get("name", "") for m in resp.json().get("models", []))
    except Exception:
        return False


@pytest.mark.skipif(not _llama_present(), reason="llama3.2 instruct model not available")
async def test_live_extraction_reconciles_a_database_fact(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    from mnemo.extraction import OllamaExtractor

    worker = ExtractionWorker(db, store.embedder, OllamaExtractor())
    await store.observe("t1", "I use Postgres for my project.", SESSION, role="user")
    assert await worker.process_one() is True

    facts = await store.search("database", session_id=SESSION)
    assert facts, "live extractor produced no fact for a clear database statement"
    blob = " ".join((f.object_text or "") for f in facts).lower()
    assert "postg" in blob
