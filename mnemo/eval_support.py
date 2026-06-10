"""Dependency-free support objects for deterministic Mnemo evaluations."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mnemo.models import ExtractedFact


class DeterministicEmbedder:
    """Stable local embedder for regression/evaluation runs.

    This deliberately measures pipeline behavior rather than embedding quality.
    Real-backend runs should inject their production embedder instead.
    """

    backend = "deterministic-sha256"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        raw = b""
        counter = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        values = [
            (int.from_bytes(raw[i * 4 : i * 4 + 4], "big") / 2**31) - 1.0 for i in range(self.dim)
        ]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / norm for value in values]


@dataclass(frozen=True)
class AtomicLabel:
    """An explicit atomic candidate and its evaluation disposition."""

    predicate: str
    value: str
    disposition: str  # truth | must_keep | forbidden | candidate
    subject: str = "user"
    importance: int = 7

    def __post_init__(self) -> None:
        if self.disposition not in {"truth", "must_keep", "forbidden", "candidate"}:
            raise ValueError(f"unsupported atomic-label disposition: {self.disposition}")

    @property
    def pair(self) -> tuple[str, str]:
        return self.predicate, self.value

    def candidate(self, *, assertion_type: str = "direct_user_statement") -> ExtractedFact:
        return ExtractedFact(
            subject=self.subject,
            predicate=self.predicate,
            object=self.value,
            confidence=0.95,
            importance=self.importance,
            assertion_type=assertion_type,
        )


@dataclass(frozen=True)
class EvalTurn:
    turn_id: str
    role: str
    text: str
    session_id: str
    timestamp: datetime | None = None
    source_id: str | None = None
    labels: tuple[AtomicLabel, ...] = ()


@dataclass(frozen=True)
class EvalDataset:
    name: str
    version: str
    split: str
    turns: tuple[EvalTurn, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def truth(self) -> set[tuple[str, str]]:
        latest = {}
        for turn in self.turns:
            for label in turn.labels:
                if label.disposition in {"truth", "must_keep"}:
                    latest[(label.subject, label.predicate)] = label.pair
        return set(latest.values())

    @property
    def must_keep(self) -> set[tuple[str, str]]:
        keys = {
            (label.subject, label.predicate)
            for turn in self.turns
            for label in turn.labels
            if label.disposition == "must_keep"
        }
        latest = {}
        for turn in self.turns:
            for label in turn.labels:
                if (
                    label.disposition in {"truth", "must_keep"}
                    and (label.subject, label.predicate) in keys
                ):
                    latest[(label.subject, label.predicate)] = label.pair
        return set(latest.values())

    @property
    def forbidden(self) -> set[tuple[str, str]]:
        return {
            label.pair
            for turn in self.turns
            for label in turn.labels
            if label.disposition == "forbidden"
        }

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(f"{self.name}\0{self.version}\0{self.split}\n".encode())
        for turn in self.turns:
            digest.update(
                f"{turn.turn_id}\0{turn.role}\0{turn.session_id}\0{turn.timestamp}\0"
                f"{turn.source_id}\0{turn.text}\n".encode()
            )
            for label in turn.labels:
                digest.update(
                    f"{label.subject}\0{label.predicate}\0{label.value}\0"
                    f"{label.disposition}\0{label.importance}\n".encode()
                )
        return digest.hexdigest()


class LabeledReplayExtractor:
    """Replays the exact same labeled candidates into both comparison arms."""

    backend = "explicit-atomic-label-replay"

    def __init__(self, turns: Iterable[EvalTurn]) -> None:
        self._by_source = {(turn.role, turn.text): turn.labels for turn in turns}

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        assertion = "direct_user_statement" if role == "user" else "agent_inference"
        return [
            label.candidate(assertion_type=assertion)
            for label in self._by_source.get((role, text), ())
        ]


def component_fingerprint(component: Any) -> str:
    """Return a stable descriptive fingerprint without inventing model metadata."""

    cls = component.__class__
    identity = f"{cls.__module__}.{cls.__qualname__}"
    observed = {
        key: getattr(component, key)
        for key in ("backend", "model", "revision", "version", "dim")
        if hasattr(component, key)
    }
    return hashlib.sha256(f"{identity}:{sorted(observed.items())}".encode()).hexdigest()
