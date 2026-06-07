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

from pydantic import BaseModel

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

    @property
    def value(self) -> Any:
        return _value_of(self.object_text, self.object_number, self.object_json)

    @classmethod
    def from_row(cls, row: Row) -> Event:
        d = dict(row)
        return cls(**{k: d.get(k) for k in cls.model_fields})


class Fact(BaseModel):
    """A fact at HEAD — stable identity plus its current believed value."""

    fact_id: UUID
    namespace: str
    user_id: str
    agent_id: str
    session_id: str | None = None
    subject: str
    predicate: str
    kind: str
    event_id: UUID
    object_text: str | None = None
    object_number: Decimal | None = None
    object_json: Any | None = None
    provenance: str
    confidence: float
    trust_level: str
    valid_from: datetime | None = None
    recorded_at: datetime | None = None
    score: float | None = None

    @property
    def value(self) -> Any:
        return _value_of(self.object_text, self.object_number, self.object_json)

    @classmethod
    def from_row(cls, row: Row, *, score: float | None = None) -> Fact:
        d = dict(row)
        d["score"] = score
        return cls(**{k: d.get(k) for k in cls.model_fields})


class Commit(BaseModel):
    """A named pointer into the event sequence (high-water seq)."""

    commit_id: UUID
    namespace: str
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
