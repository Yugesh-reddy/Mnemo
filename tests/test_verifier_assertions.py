"""Development regressions for relation fidelity across NLI and its fallback."""

import json
import sys
from types import SimpleNamespace

import pytest

from mnemo.models import ExtractedFact
from mnemo.quality import CrossEncoderVerifier, LLMVerifier, Verdict, _hypothesis


def test_unknown_verb_assertion_uses_structured_fallback_without_an_invented_hypothesis(
    monkeypatch,
):
    fact = ExtractedFact(subject="user", predicate="attended_workshop", object="fermentation")
    source = "I recently attended a workshop on fermentation at a local food co-op."

    class Encoder:
        def __init__(self, model):
            self.model = SimpleNamespace(
                config=SimpleNamespace(id2label={0: "neutral", 1: "entailment", 2: "contradiction"})
            )

        def predict(self, pairs, **kwargs):
            raise AssertionError("An unknown verb must not become a possessive identity")

    class Fallback:
        def verify(self, candidate, evidence):
            assert candidate == fact
            assert evidence == source
            return Verdict(
                accepted=True,
                label="entailment",
                probability=1,
                backend="stub",
                reason="The user explicitly attended the workshop.",
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=Encoder))
    assert _hypothesis(fact) is None
    assert CrossEncoderVerifier("stub", fallback=Fallback()).verify(fact, source).accepted
    assert not CrossEncoderVerifier("stub").verify(fact, source).accepted


def test_fallback_verifies_only_the_assertion_not_extractor_claimed_authority():
    fact = ExtractedFact(
        subject="user",
        predicate="presented_poster",
        object="consumer purchasing decisions",
        assertion_type="direct_user_statement",
        confidence=1,
        importance=10,
    )
    source = "I recently presented a poster on consumer purchasing decisions."
    verifier = LLMVerifier(backend="ollama", model="stub", base_url="http://unused")

    def reply(prompt):
        payload = json.loads(prompt[prompt.index('{"source"') :])
        assert payload == {
            "source": source,
            "assertion": {
                "subject": "user",
                "predicate": "presented_poster",
                "object": "consumer purchasing decisions",
            },
        }
        return {"label": "entailment", "probability": 1, "reason": "Explicit past action"}

    verifier._request = reply
    try:
        assert verifier.verify(fact, source).accepted
    finally:
        verifier.close()


def test_confident_job_role_does_not_reverse_a_manager_relationship(monkeypatch):
    class Encoder:
        def __init__(self, model):
            self.model = SimpleNamespace(
                config=SimpleNamespace(id2label={0: "neutral", 1: "entailment", 2: "contradiction"})
            )

        def predict(self, pairs, **kwargs):
            return [[0.003, 0.9927, 0.0043]]

    class Fallback:
        def verify(self, candidate, evidence):
            return Verdict(
                accepted=False,
                label="neutral",
                probability=1,
                backend="stub",
                reason="Elena manages the user; the user is not asserted to manage Elena.",
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=Encoder))
    fact = ExtractedFact(subject="user", predicate="role", object="manager of Elena Ruiz")
    source = "My manager is Elena Ruiz."
    assert not CrossEncoderVerifier("stub", fallback=Fallback()).verify(fact, source).accepted
    assert not CrossEncoderVerifier("stub").verify(fact, source).accepted


@pytest.mark.parametrize(
    ("predicate", "source", "accepted"),
    [
        ("intends_to_cook", "I'm thinking of making kimchi fried rice.", False),
        ("planned_meal", "I'm considering kimchi fried rice for dinner.", False),
        ("considered_meal", "I'm considering kimchi fried rice for dinner.", True),
        (
            "planned_meal",
            "I'm considering lasagna. I will make kimchi fried rice for dinner.",
            True,
        ),
        (
            "planned_meal",
            "I was thinking of kimchi fried rice, but I will make kimchi fried rice tonight.",
            True,
        ),
        (
            "planned_meal",
            "I was considering kimchi fried rice. I've decided to make it tonight.",
            True,
        ),
    ],
)
def test_consideration_cannot_be_strengthened_into_a_definite_plan(predicate, source, accepted):
    verifier = LLMVerifier(backend="ollama", model="stub", base_url="http://unused")
    verifier._request = lambda _: {
        "label": "entailment",
        "probability": 1,
        "reason": "Confident model answer",
    }
    fact = ExtractedFact(subject="user", predicate=predicate, object="kimchi fried rice")
    try:
        assert verifier.verify(fact, source).accepted is accepted
    finally:
        verifier.close()


@pytest.mark.parametrize(
    ("predicate", "value", "source", "accepted"),
    [
        ("uses_database", "Linear", "Our team tracks issues in Linear.", False),
        ("editor", "fish", "My interactive shell is fish.", False),
        ("shell", "Neovim", "I use Neovim as my text editor.", False),
        ("uses_database", "QuasarStore", "My production database is QuasarStore.", True),
        ("uses_database", "MongoDB", "I use MongoDB.", True),
        ("uses_database", ["MongoDB", "Redis"], "I use MongoDB and Redis.", True),
        (
            "uses_database",
            ["QuasarStore", "AsterLake"],
            "Our databases are QuasarStore and AsterLake.",
            True,
        ),
        ("editor", "Emacs", "Emacs is both my editor and interactive shell.", True),
        ("role", "Reliability Platform team", "I belong to the Reliability Platform team.", False),
    ],
)
def test_typed_relations_require_type_evidence(predicate, value, source, accepted):
    verifier = LLMVerifier(backend="ollama", model="stub", base_url="http://unused")
    verifier._request = lambda _: {
        "label": "entailment",
        "probability": 1,
        "reason": "Confident model answer",
    }
    fact = ExtractedFact(subject="user", predicate=predicate, object=value)
    try:
        assert verifier.verify(fact, source).accepted is accepted
    finally:
        verifier.close()
