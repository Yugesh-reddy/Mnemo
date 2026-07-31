"""The near-duplicate warning on create (spec §35)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.direct import DirectMemory
from mnemo.embedder import build_embedder
from mnemo.models import SIMILAR_NOTE, MutationResult
from mnemo.sqlite import connect
from tests.test_retrieval_quality import _available

DATA = Path(__file__).parent / "data"
RETRIEVAL = json.loads((DATA / "retrieval_set.json").read_text())["memories"]
DUPLICATES = json.loads((DATA / "duplicate_set.json").read_text())["duplicates"]


class Words:
    """Bag-of-words vectors, so a reworded memory lands near the original."""

    model = "bag-of-words"
    dim = 64

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in text.lower().replace("-", " ").split():
            vector[sum(map(ord, word.rstrip("s"))) % self.dim] += 1.0
        return vector


def memory(db, fake_embedder, floor: float | None = 0.8, namespace: str = "default"):
    settings = Settings(backend="hash", embed_dim=Words.dim, duplicate_floor=floor)
    return DirectMemory(MnemoStore(db, Words(), settings=settings, namespace=namespace))


async def test_a_near_duplicate_is_reported_with_a_note(db, fake_embedder):
    d = memory(db, fake_embedder)
    first = await d.create("tests", "command", "make test-db", request_id=uuid4())
    assert first.similar == [] and "similar" not in first.model_dump(mode="json")
    request_id = uuid4()
    again = await d.create("test", "command", "make test-db", request_id=request_id)
    [similar] = again.similar
    assert similar.fact_id == first.fact_id and similar.current_event_id == first.event_id
    assert similar.value == "make test-db" and similar.similarity >= 0.8
    dumped = again.model_dump(mode="json")
    assert dumped["note"] == SIMILAR_NOTE and dumped["similar"][0]["subject"] == "tests"
    replay = await d.create("test", "command", "make test-db", request_id=request_id)
    assert replay.replayed and replay.similar == again.similar
    assert MutationResult.model_validate(dumped) == again


async def test_only_current_durable_memories_in_the_same_scope_count(db, fake_embedder):
    d = memory(db, fake_embedder)
    other = memory(db, fake_embedder, namespace="default@github.com/acme/other")
    await other.create("tests", "command", "make test-db", request_id=uuid4())
    gone = await d.create("tests", "command", "make test-db", request_id=uuid4())
    await d.retract(gone.fact_id, expected_event_id=gone.event_id, request_id=uuid4())
    assert (await d.create("test", "command", "make test-db", request_id=uuid4())).similar == []


async def test_no_floor_no_warning(db, fake_embedder):
    d = memory(db, fake_embedder, floor=None)
    await d.create("tests", "command", "make test-db", request_id=uuid4())
    assert (await d.create("test", "command", "make test-db", request_id=uuid4())).similar == []


def test_floors_are_set_only_for_measured_models():
    assert Settings(backend="local").duplicate_floor == 0.80
    assert Settings(backend="ollama").duplicate_floor == 0.75
    assert Settings(backend="hash").duplicate_floor is None
    assert Settings(backend="ollama", embed_model="mxbai-embed-large").duplicate_floor is None
    assert Settings(backend="local", duplicate_floor=0.7).duplicate_floor == 0.7


@pytest.mark.parametrize(("backend", "caught"), [("ollama", 20), ("local", 19)])
async def test_the_floor_flags_duplicates_and_not_neighbours(_disposable_test_db, backend, caught):
    """Live: the measured floor finds the rewordings of tests/data/duplicate_set.json
    and never links two of the 40 unrelated memories in retrieval_set.json."""
    if not _available(backend):
        pytest.skip(f"the {backend} embedding model is not available")
    settings = Settings(backend=backend, search_reinforce=False)
    db = await connect(_disposable_test_db, create=False)
    embedder = build_embedder(settings)
    try:
        d = DirectMemory(MnemoStore(db, embedder, settings=settings, namespace="dups"))
        ids = []
        for row in RETRIEVAL:
            created = await d.create(
                row["subject"], row["predicate"], row["value"], request_id=uuid4()
            )
            assert created.similar == [], (row, created.similar)
            ids.append(created.fact_id)
        found = 0
        for index, row in DUPLICATES:
            created = await d.create(
                row["subject"], row["predicate"], row["value"], request_id=uuid4()
            )
            flagged = [s.fact_id for s in created.similar]
            assert set(flagged) <= {ids[index]}, (row, created.similar)
            found += ids[index] in flagged
            await d.retract(created.fact_id, expected_event_id=created.event_id, request_id=uuid4())
        assert found >= caught, found
    finally:
        close = getattr(embedder, "close", None)
        if close:
            close()
        await db.close()
