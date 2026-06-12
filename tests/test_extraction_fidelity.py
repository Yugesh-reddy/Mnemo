"""Development regressions for extraction representation and semantic verification."""

import sys
from types import SimpleNamespace

import pytest

from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact
from mnemo.quality import CrossEncoderVerifier, Verdict, _hypothesis


def candidate(predicate, value):
    return ExtractedFact(subject="user", predicate=predicate, object=value, importance=8)


@pytest.mark.parametrize("value", ["not Eastern Time", "not_bare_metal", {"not": "Payments"}])
async def test_negative_value_never_overwrites_positive_identity(store, db, value):
    class Extractor:
        def extract(self, text, role="user"):
            return [candidate("timezone", value)]

    class AcceptEverything:
        def verify(self, candidate, source):
            return Verdict(
                accepted=True,
                label="entailment",
                probability=1,
                backend="stub",
                reason="even a confident model must not replace identity",
            )

    first = await store.add("user", "timezone", "Pacific Time")
    await store.observe("denial", "I do not use Eastern Time as my timezone.", "s")
    await ExtractionWorker(db, store.embedder, Extractor(), AcceptEverything()).process_one()
    current = await store.get(first.fact_id)
    assert current.value == "Pacific Time"
    assert await db.fetchval("SELECT count(*) FROM memory_event") == 1
    assert "negative value" in await db.fetchval("SELECT reason FROM quality_decision")


def test_hypothesis_keeps_database_type_and_preference():
    assert "database" in _hypothesis(candidate("uses_database", "Neovim"))
    assert "database" in _hypothesis(candidate("preferred_database", "USD"))
    assert "preferred" in _hypothesis(candidate("preferred_database", "MongoDB"))


def test_confident_denial_requires_fallback_or_fails_closed(monkeypatch):
    class Encoder:
        def __init__(self, model):
            self.model = SimpleNamespace(
                config=SimpleNamespace(id2label={0: "neutral", 1: "entailment", 2: "contradiction"})
            )

        def predict(self, pairs, **kwargs):
            return [[0.001, 0.9933, 0.0057]]

    class Fallback:
        calls = 0

        def verify(self, candidate, source):
            self.calls += 1
            return Verdict(
                accepted=False,
                label="contradiction",
                probability=1,
                backend="stub",
                reason="source denies emergency contact",
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=Encoder))
    source = "I do not use Alex Chen; please don't infer that as my emergency contact."
    fact = candidate("emergency_contact", "Alex Chen")
    assert not CrossEncoderVerifier("stub").verify(fact, source).accepted
    fallback = Fallback()
    assert not CrossEncoderVerifier("stub", fallback=fallback).verify(fact, source).accepted
    assert fallback.calls == 1
    # A denial about a different object must not block a clear factual clause.
    assert (
        CrossEncoderVerifier("stub")
        .verify(candidate("location", "Austin"), "I live in Austin. I do not use Redis.")
        .accepted
    )


def test_heuristic_does_not_confuse_use_preference_and_type():
    from mnemo.quality import HeuristicVerifier

    verifier = HeuristicVerifier()
    assert not verifier.verify(
        candidate("preferred_database", "Postgres"), "I use Postgres."
    ).accepted
    assert not verifier.verify(
        candidate("uses_database", "Neovim"), "I use Neovim as my editor."
    ).accepted
    assert verifier.verify(candidate("uses_database", "Postgres"), "I use Postgres.").accepted


def test_smoke_label_revision_preserves_original_source_and_legacy_labels():
    from mnemo.eval_data import load_smoke_dataset

    old = load_smoke_dataset(version="1.0.0")
    new = load_smoke_dataset()
    assert [(t.turn_id, t.text) for t in old.turns] == [(t.turn_id, t.text) for t in new.turns]
    assert old.turns[0].labels[-1].predicate == "preferred_database"
    assert new.turns[0].labels[-1].predicate == "uses_database"
    assert new.turns[10].labels[0].predicate == "primary_language"
