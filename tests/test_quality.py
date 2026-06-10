"""C2: the quality gate's pure functions — verification, scoring, tiering.

CLAUDE.md test #3: negations + hypotheticals rejected (no false memories);
borderline facts demoted, not dropped (recall protected).
"""

from __future__ import annotations

import pytest

from mnemo.config import Settings
from mnemo.models import ExtractedFact
from mnemo.quality import (
    HeuristicVerifier,
    LLMVerifier,
    is_transient,
    specificity,
    tier_for,
    write_score,
)

S = Settings(_env_file=None)
V = HeuristicVerifier()


# ---- verification (Layer 1 pre-filter) -----------------------------------


def test_negation_rejected() -> None:
    v = V.verify(
        _candidate("user", "preferred_database", "MongoDB"), "I don't use MongoDB, never liked it."
    )
    assert not v.accepted
    assert v.label == "contradiction"


def test_hypothetical_rejected() -> None:
    v = V.verify(
        _candidate("user", "preferred_database", "graph database"),
        "What if I switched to a graph database someday?",
    )
    assert not v.accepted
    assert v.label == "neutral"


@pytest.mark.parametrize(
    "obj, src",
    [
        ("PostgreSQL", "I use PostgreSQL for my main project."),
        ("Priya", "My team lead is Priya and we ship on Fridays."),
    ],
)
def test_plain_assertion_accepted(obj: str, src: str) -> None:
    v = V.verify(
        _candidate("user", "team_lead" if obj == "Priya" else "preferred_database", obj), src
    )
    assert v.accepted
    assert v.label == "entailment"


def test_negation_of_a_different_object_is_not_rejected() -> None:
    # "I don't work weekends" must not poison an unrelated fact from the same turn.
    v = V.verify(
        _candidate("user", "location", "Austin"),
        "I don't work weekends anymore, I moved to Austin.",
    )
    assert v.accepted


def _candidate(subject: str, predicate: str, value: str) -> ExtractedFact:
    return ExtractedFact(subject=subject, predicate=predicate, object=value)


def test_full_candidate_rejects_invented_object() -> None:
    verdict = V.verify(_candidate("user", "preferred_database", "MongoDB"), "I live in Austin.")
    assert not verdict.accepted
    assert verdict.label == "neutral"
    assert verdict.backend == "heuristic"
    assert verdict.probability == 1.0


def test_full_candidate_rejects_wrong_relation_and_subject() -> None:
    assert not V.verify(_candidate("user", "team_lead", "Austin"), "I live in Austin.").accepted
    assert not V.verify(_candidate("Priya", "location", "Austin"), "I live in Austin.").accepted


def test_multiple_negations_reject_matching_denial() -> None:
    verdict = V.verify(
        _candidate("user", "preferred_database", "MongoDB"),
        "I don't use Redis and I never use MongoDB.",
    )
    assert not verdict.accepted
    assert verdict.label == "contradiction"
    assert verdict.evidence == "I never use MongoDB."


def test_unrelated_hypothetical_does_not_poison_factual_clause() -> None:
    verdict = V.verify(
        _candidate("user", "location", "Austin"),
        "What if I used MongoDB someday? I live in Austin.",
    )
    assert verdict.accepted


@pytest.mark.parametrize(
    "candidate, source",
    [
        (_candidate("user", "preferred_database", "PostgreSQL"), "I use Postgres."),
        (_candidate("user", "ship_day", "Friday"), "We ship on Fridays."),
        (_candidate("user", "timezone", "America/Chicago"), "My timezone is US Central."),
    ],
)
def test_full_candidate_aliases(candidate: ExtractedFact, source: str) -> None:
    assert V.verify(candidate, source).accepted


def test_unknown_relation_is_honestly_neutral() -> None:
    verdict = V.verify(_candidate("user", "favorite_orm", "SQLAlchemy"), "I use SQLAlchemy.")
    assert not verdict.accepted
    assert verdict.label == "neutral"
    assert "no rule" in verdict.reason


def test_relation_must_attach_to_candidate_object() -> None:
    language = _candidate("user", "preferred_language", "Rust")
    assert not V.verify(language, "I use PostgreSQL and want to learn Rust.").accepted
    name = _candidate("user", "name", "Austin")
    assert not V.verify(name, "I am in Austin.").accepted


def test_llm_acceptance_is_derived_from_label_and_threshold() -> None:
    verifier = LLMVerifier(backend="ollama", model="test", base_url="http://unused", max_retries=0)
    verifier._request = lambda _: {  # type: ignore[method-assign]
        "label": "entailment",
        "probability": 0.98,
        "reason": "model confidence",
    }
    verdict = verifier.verify(_candidate("user", "location", "Austin"), "I live in Austin")
    assert verdict.label == "entailment"
    assert not verdict.accepted
    verifier.close()


def test_llm_rejects_unexpected_self_reported_acceptance() -> None:
    verifier = LLMVerifier(backend="ollama", model="test", base_url="http://unused", max_retries=0)
    verifier._request = lambda _: {  # type: ignore[method-assign]
        "accepted": True,
        "label": "entailment",
        "probability": 1.0,
        "reason": "trust me",
    }
    verdict = verifier.verify(_candidate("user", "location", "Austin"), "unrelated")
    assert not verdict.accepted
    assert verdict.label == "neutral"
    verifier.close()


# ---- scoring + tiering (Layer 2) ------------------------------------------


def test_specificity_vocab_vs_not() -> None:
    assert specificity("preferred_database", S.predicate_vocab) == 1.0
    assert specificity("weather", S.predicate_vocab) == 0.2


def test_is_transient() -> None:
    assert is_transient("Right now I'm waiting for CI to finish.")
    assert not is_transient("My timezone is US Central.")


def test_write_score_high_importance_in_vocab_novel_is_durable() -> None:
    score = write_score(
        importance=8, spec=1.0, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    assert score >= S.durable_cutoff
    assert tier_for(score, S) == "durable"


def test_junk_scores_below_floor_and_is_dropped() -> None:
    # weather: importance 2, out-of-vocab, transient turn, assistant-sourced
    score = write_score(
        importance=2, spec=0.2, novelty=1.0, from_assistant=True, transient=True, settings=S
    )
    assert score < S.ephemeral_floor
    assert tier_for(score, S) is None


def test_borderline_is_demoted_to_session_not_dropped() -> None:
    # mid importance, out-of-vocab, novel -> between floor and cutoff
    score = write_score(
        importance=5, spec=0.2, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    assert S.ephemeral_floor <= score < S.durable_cutoff
    assert tier_for(score, S) == "session"


def test_duplicate_novelty_zero_drags_score_down() -> None:
    fresh = write_score(
        importance=7, spec=1.0, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    dupe = write_score(
        importance=7, spec=1.0, novelty=0.0, from_assistant=False, transient=False, settings=S
    )
    assert dupe < fresh


def test_object_only_verification_is_not_an_acceptance_path() -> None:
    with pytest.raises(TypeError, match="subject, predicate and object"):
        V.verify("Austin", "I live in Austin.")


def test_cross_encoder_label_mapping_and_bounded_fallback(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from mnemo.quality import CrossEncoderVerifier, Verdict

    class Encoder:
        def __init__(self, model):
            self.model = SimpleNamespace(
                config=SimpleNamespace(id2label={0: "neutral", 1: "entailment", 2: "contradiction"})
            )

        def predict(self, pairs, **kwargs):
            assert pairs[0][1] == "I live in Austin."
            return [[0.2, 0.7, 0.1]]

    class Fallback:
        calls = 0

        def verify(self, candidate, source):
            self.calls += 1
            return Verdict(
                accepted=True,
                label="entailment",
                probability=1,
                reason="explicit statement",
                backend="stub",
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=Encoder))
    fallback = Fallback()
    verifier = CrossEncoderVerifier("stub", fallback=fallback)
    assert verifier.verify(_candidate("user", "location", "Austin"), "I live in Austin.").accepted
    assert fallback.calls == 1


def test_nli_hypothesis_preserves_preference_and_subject():
    from mnemo.quality import _hypothesis

    assert _hypothesis(_candidate("Priya", "preferred_database", "MongoDB")) == (
        "Priya prefers MongoDB."
    )
    assert _hypothesis(_candidate("user", "uses_database", "MongoDB")) == "I use MongoDB."
