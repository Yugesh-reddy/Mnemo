"""Pydantic models for the public surface: Event, Fact, Commit, Diff.

These are thin views over rows — the SQL is the source of truth. ``from_row``
mappers keep the SQL ↔ model boundary in one place. The raw ``embedding`` vector is
deliberately not surfaced here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# asyncpg Record is mapping-like; dict(row) gives us .get for optional columns.
Row = Any


def _value_of(object_text: str | None, object_number: Decimal | None, object_json: Any) -> Any:
    """The single 'value' of a fact, whichever object_* column carries it."""
    if object_text is not None:
        return object_text
    if object_number is not None:
        return object_number
    return object_json


class Event(BaseModel):
    """One immutable entry in the append-only log."""

    event_id: UUID
    seq: int
    fact_id: UUID
    op: str
    object_text: str | None = None
    object_number: Decimal | None = None
    object_json: Any | None = None
    provenance: str
    actor: str | None = None
    confidence: float
    trust_level: str
    source_span: Any | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime
    superseded_at: datetime | None = None
    superseded_by: UUID | None = None
    parent_event_id: UUID | None = None
    # --- council quality / decay fields (0003) ---
    importance: int | None = None
    write_score: float | None = None
    tier: str = "durable"
    reason: str | None = None
    strength: float = 1.0
    recall_count: int = 0
    last_used: datetime | None = None

    @property
    def value(self) -> Any:
        return _value_of(self.object_text, self.object_number, self.object_json)

    @classmethod
    def from_row(cls, row: Row) -> Event:
        d = dict(row)
        return cls(**{k: d[k] for k in cls.model_fields if k in d})


class Fact(BaseModel):
    """A fact at HEAD — its current believed value.

    A search result is either a reconciled ``semantic`` fact or an un-reconciled
    ``fast_cache`` hit (raw turn text awaiting extraction); ``source`` says which.
    """

    fact_id: UUID
    namespace: str = "default"
    user_id: str = "default"
    agent_id: str = "default"
    session_id: str | None = None
    subject: str = ""
    predicate: str = ""
    kind: str = "triple"
    event_id: UUID | None = None
    object_text: str | None = None
    object_number: Decimal | None = None
    object_json: Any | None = None
    provenance: str = "agent_inference"
    confidence: float = 1.0
    trust_level: str = "medium"
    valid_from: datetime | None = None
    recorded_at: datetime | None = None
    score: float | None = None
    source: str = "semantic"
    raw_text: str | None = None
    importance: int | None = None
    write_score: float | None = None
    tier: str = "durable"
    strength: float = 1.0
    recall_count: int = 0

    @property
    def value(self) -> Any:
        if self.source == "fast_cache":
            return self.raw_text
        return _value_of(self.object_text, self.object_number, self.object_json)

    @classmethod
    def from_row(cls, row: Row, *, score: float | None = None) -> Fact:
        d = dict(row)
        data = {k: d[k] for k in cls.model_fields if k in d}
        if score is not None:
            data["score"] = score
        return cls(**data)


class Commit(BaseModel):
    """A named pointer into the event sequence (high-water seq)."""

    commit_id: UUID
    namespace: str
    user_id: str = "default"
    agent_id: str = "default"
    parent_commit_id: UUID | None = None
    label: str | None = None
    at_seq: int
    created_by: str | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Row) -> Commit:
        d = dict(row)
        return cls(**{k: d.get(k) for k in cls.model_fields})


class DiffEntry(BaseModel):
    """One +/~/- change between two points in time."""

    fact_id: UUID
    subject: str
    predicate: str
    change: str  # 'added' | 'removed' | 'changed'
    old: Any | None = None
    new: Any | None = None


class Diff(BaseModel):
    """The set of changes between two commits."""

    commit_a: UUID
    commit_b: UUID
    seq_a: int
    seq_b: int
    entries: list[DiffEntry]


class ExtractedFact(BaseModel):
    """A candidate fact from the (untrusted) extractor — validated before use."""

    subject: str
    predicate: str
    object: Any
    kind: str = "triple"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: int = Field(default=5, ge=1, le=10)
    assertion_type: str = "agent_inference"
