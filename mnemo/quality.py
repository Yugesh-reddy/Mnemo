"""The quality gate — Layers 1–2 of the write path (spec §4).

Pure functions + a pluggable Verifier. The heuristic verifier is the free regex
pre-filter; an NLI entailment backend can replace it without touching the gate
(same .verify() shape). All weights/cutoffs come from Settings — tune ONLY
against `make eval`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from mnemo.config import Settings

NEGATION = re.compile(r"\b(don'?t|do not|never|not|no longer|stopped using)\b", re.I)
HYPOTHETICAL = re.compile(
    r"\b(what if|someday|if i|would i|maybe i'?ll|might|thinking about|who knows)\b", re.I
)
TRANSIENT = re.compile(
    r"\b(today|right now|just|currently|at the moment|this morning|waiting for)\b", re.I
)


class Verdict(BaseModel):
    accepted: bool
    label: str  # entailment | contradiction | neutral
    reason: str


class HeuristicVerifier:
    """Free regex stand-in for NLI entailment (spec §4 Layers 1-2).

    contradiction: the candidate object appears inside a negated source turn.
    neutral: the source turn is hypothetical, not an assertion.
    entailment: everything else (the NLI/LLM backend refines this later).
    """

    def verify(self, object_text: str, source_text: str) -> Verdict:
        src = source_text.lower()
        obj = object_text.lower()
        if HYPOTHETICAL.search(src):
            return Verdict(accepted=False, label="neutral", reason="hypothetical, not an assertion")
        negation = NEGATION.search(src)
        if negation and obj in src:
            obj_start = src.find(obj)
            between = src[negation.end() : obj_start]
            # Negation scope ends at a clause boundary: "I don't work weekends,
            # I moved to Austin" must not poison "Austin".
            in_scope = obj_start > negation.start() and not re.search(
                r"[,.;!?]|\b(and|but|so)\b", between
            )
            if in_scope:
                return Verdict(
                    accepted=False,
                    label="contradiction",
                    reason="refused to assert a denied fact",
                )
        return Verdict(accepted=True, label="entailment", reason="asserted by source turn")


def specificity(predicate: str, vocab: list[str]) -> float:
    return 1.0 if predicate in vocab else 0.2


def is_transient(source_text: str) -> bool:
    return bool(TRANSIENT.search(source_text))


def write_score(
    *,
    importance: int,
    spec: float,
    novelty: float,
    from_assistant: bool,
    transient: bool,
    settings: Settings,
) -> float:
    """spec §4 Layer 2: w_imp·(imp/10) + w_spec·spec + w_nov·nov − assistant − transient."""
    score = settings.w_imp * (importance / 10.0) + settings.w_spec * spec + settings.w_nov * novelty
    if from_assistant:
        score -= settings.w_src
    if transient:
        score -= settings.transient_penalty
    return max(0.0, score)


def tier_for(score: float, settings: Settings) -> str | None:
    """durable >= cutoff; session >= floor (demote, don't drop); None = true noise."""
    if score >= settings.durable_cutoff:
        return "durable"
    if score >= settings.ephemeral_floor:
        return "session"
    return None
