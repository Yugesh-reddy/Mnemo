"""Queue leases and scope isolation exercised with actual independent connections."""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker, LeaseLost
from mnemo.models import ExtractedFact
from tests.test_twotier import StubExtractor


async def test_claim_scope_and_exact_cache_identity(worker_connections, fake_embedder):
    a, b = worker_connections
    scopes = [
        dict(namespace="one", user_id="alice", agent_id="a"),
        dict(namespace="one", user_id="bob", agent_id="a"),
        dict(namespace="one", user_id="alice", agent_id="b"),
    ]
    for scope in scopes:
        await MnemoStore(a, fake_embedder, **scope).observe(
            "same-turn", "I use Postgres.", "same-session"
        )
    worker = ExtractionWorker(b, fake_embedder, StubExtractor(), namespace="wrong")
    for _ in scopes:
        assert await worker.process_one()
    rows = await a.fetch(
        "SELECT namespace,user_id,agent_id,provenance,trust_level FROM memory_current"
    )
    assert {(r["namespace"], r["user_id"], r["agent_id"]) for r in rows} == {
        ("one", "alice", "a"),
        ("one", "bob", "a"),
        ("one", "alice", "b"),
    }
    assert all(r["provenance"] == "agent_inference" and r["trust_level"] == "low" for r in rows)
    assert await a.fetchval("SELECT count(*) FROM fast_cache WHERE reconciled") == 3


async def test_slow_extraction_renews_lease_and_cannot_be_reclaimed(
    worker_connections, fake_embedder
):
    a, b = worker_connections

    class Slow(StubExtractor):
        def extract(self, text, role="user"):
            time.sleep(0.8)
            return super().extract(text, role)

    settings = Settings(_env_file=None, job_lease_seconds=0.3)
    await MnemoStore(a, fake_embedder).observe("t", "I use Postgres.", "s")
    first = ExtractionWorker(a, fake_embedder, Slow(), settings=settings)
    second = ExtractionWorker(b, fake_embedder, StubExtractor(), settings=settings)
    task = asyncio.create_task(first.process_one())
    await asyncio.sleep(0.5)
    assert not await second.process_one()
    assert await task
    assert await b.fetchval("SELECT attempts FROM extraction_job") == 1
    assert await b.fetchval("SELECT count(*) FROM memory_event") == 1


async def test_lost_owner_cannot_commit_or_reconcile(worker_connections, fake_embedder):
    a, b = worker_connections
    await MnemoStore(a, fake_embedder).observe("t", "I use Postgres.", "s")
    worker = ExtractionWorker(a, fake_embedder, StubExtractor())
    job = await worker._claim()
    prepared = await worker._prepare("I use Postgres.", "user")
    await b.execute(
        "UPDATE extraction_job SET locked_by=$1 WHERE job_id=$2", uuid4(), job["job_id"]
    )
    with pytest.raises(LeaseLost):
        await worker._commit(job, "I use Postgres.", prepared)
    assert await b.fetchval("SELECT count(*) FROM memory_event") == 0
    assert not await b.fetchval("SELECT reconciled FROM fast_cache")
    assert await b.fetchval("SELECT count(*) FROM quality_decision") == 0


async def test_retry_exhaustion_and_assistant_exclusion(store, db):
    class Broken:
        def extract(self, text, role="user"):
            raise TimeoutError("private request content must not appear in diagnostics")

    settings = Settings(_env_file=None, job_retry_base_seconds=0, job_max_attempts=2)
    worker = ExtractionWorker(db, store.embedder, Broken(), settings=settings)
    await store.observe("user", "I use Postgres.", "s")
    assert await worker.process_one()
    assert await db.fetchval("SELECT status FROM extraction_job") == "pending"
    assert await worker.process_one()
    assert await db.fetchval("SELECT status FROM extraction_job") == "failed"
    assert await db.fetchval("SELECT last_error FROM extraction_job") == "TimeoutError"
    await store.observe("assistant", "I use Postgres.", "s", role="assistant")
    assert await worker.process_one()  # does not call Broken.extract
    assert (
        await db.fetchval("SELECT status FROM extraction_job WHERE turn_id='assistant'") == "done"
    )
    assert await db.fetchval("SELECT count(*) FROM memory_event") == 0


async def test_provenance_spoof_and_full_assertion_rejection(store, db):
    malicious = [
        ExtractedFact(
            subject="user",
            predicate="preferred_database",
            object="MongoDB",
            importance=10,
            assertion_type="human_review",
        )
    ]
    worker = ExtractionWorker(db, store.embedder, StubExtractor(malicious))
    await store.observe("t", "I live in Austin.", "s")
    await worker.process_one()
    row = await db.fetchrow("SELECT * FROM quality_decision")
    assert row["outcome"] == "rejected"
    assert json.loads(row["verification"])["label"] == "neutral"
    assert await db.fetchval("SELECT count(*) FROM memory_event") == 0


async def test_runtime_drains_and_closes_pool(_disposable_test_db, fake_embedder, clean_memory):
    from mnemo.runtime import background_runtime

    settings = Settings(_env_file=None, dsn=_disposable_test_db, worker_poll_seconds=0.01)
    async with background_runtime(
        settings, embedder=fake_embedder, extractor=StubExtractor()
    ) as rt:
        async with rt["pool"].acquire() as conn:
            await MnemoStore(conn, fake_embedder).observe("runtime", "I use Postgres.", "runtime")
        async with asyncio.timeout(5):
            while True:
                async with rt["pool"].acquire() as conn:
                    if (
                        await conn.fetchval(
                            "SELECT status FROM extraction_job WHERE turn_id='runtime'"
                        )
                        == "done"
                    ):
                        break
                await asyncio.sleep(0.02)
    assert rt["pool"]._closed


async def test_repeat_after_intervening_correction_is_not_hash_deduplicated(store, db):
    await store.observe("t1", "I live in Austin.", "s")
    await store.observe("t2", "I live in Portland.", "s")
    await store.observe("t3", "I live in Austin.", "s")
    await store.observe("t3", "I live in Austin.", "s")  # retry is still idempotent
    assert await db.fetchval("SELECT count(*) FROM extraction_job") == 3


LEGACY_TIERING = dict(
    w_imp=0.4,
    w_spec=0.3,
    w_nov=0.3,
    ephemeral_floor=0.45,
    novelty_mode="cosine",
    transient_markers=[
        "today",
        "right now",
        "just",
        "currently",
        "at the moment",
        "this morning",
        "waiting for",
    ],
)


@pytest.mark.parametrize(
    ("legacy", "tier"), [(True, "session"), (False, "durable")], ids=["legacy", "lasting"]
)
async def test_transient_word_cannot_drop_an_important_verified_fact(store, db, legacy, tier):
    from mnemo.quality import Verdict

    class Constant:
        def embed(self, text):
            return [1.0] + [0.0] * (store.settings.embed_dim - 1)

    class Entailed:
        def verify(self, candidate, source):
            return Verdict(
                accepted=True,
                label="entailment",
                probability=1,
                reason="explicit allergy assertion",
                backend="test",
            )

    settings = Settings(_env_file=None, **(LEGACY_TIERING if legacy else {}))
    memory = MnemoStore(db, Constant(), settings=settings)
    await memory.add("user", "diet", "vegetarian")
    candidate = ExtractedFact(subject="user", predicate="allergy", object="peanuts", importance=9)
    await memory.observe("allergy", "Today I found out I am allergic to peanuts.", "s")
    worker = ExtractionWorker(
        db, Constant(), StubExtractor([candidate]), Entailed(), settings=settings
    )
    await worker.process_one()
    history = await memory.blame(subject="user", predicate="allergy")
    # Kept either way: legacy rescues a below-floor score into session; lasting
    # scores the important fact durable despite the transient word.
    assert len(history) == 1 and history[0].tier == tier
    if legacy:
        assert history[0].write_score < settings.ephemeral_floor


async def test_cancellation_requeues_and_late_model_return_cannot_write(
    worker_connections, fake_embedder
):
    a, b = worker_connections

    class Slow(StubExtractor):
        def extract(self, text, role="user"):
            time.sleep(0.3)
            return super().extract(text, role)

    settings = Settings(_env_file=None, job_retry_base_seconds=0)
    await MnemoStore(a, fake_embedder).observe("cancel", "I use Postgres.", "s")
    worker = ExtractionWorker(a, fake_embedder, Slow(), settings=settings)
    task = asyncio.create_task(worker.process_one())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await b.fetchval("SELECT status FROM extraction_job") == "pending"
    assert await ExtractionWorker(
        b, fake_embedder, StubExtractor(), settings=settings
    ).process_one()
    await asyncio.sleep(0.3)
    assert await b.fetchval("SELECT count(*) FROM memory_event") == 1
    assert await b.fetchval("SELECT attempts FROM extraction_job") == 2
