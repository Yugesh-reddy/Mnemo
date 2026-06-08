"""C2: the quality gate's pure functions — verification, scoring, tiering.

CLAUDE.md test #3: negations + hypotheticals rejected (no false memories);
borderline facts demoted, not dropped (recall protected).
"""

from __future__ import annotations

import pytest

from mnemo.config import Settings
from mnemo.quality import HeuristicVerifier, is_transient, specificity, tier_for, write_score

S = Settings(_env_file=None)
V = HeuristicVerifier()


# ---- verification (Layer 1 pre-filter) -----------------------------------


def test_negation_rejected() -> None:
    v = V.verify("MongoDB", "I don't use MongoDB, never liked it.")
    assert not v.accepted
    assert v.label == "contradiction"


def test_hypothetical_rejected() -> None:
    v = V.verify("graph database", "What if I switched to a graph database someday?")
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
    v = V.verify(obj, src)
    assert v.accepted
    assert v.label == "entailment"


def test_negation_of_a_different_object_is_not_rejected() -> None:
    # "I don't work weekends" must not poison an unrelated fact from the same turn.
    v = V.verify("Austin", "I don't work weekends anymore, I moved to Austin.")
    assert v.accepted


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
