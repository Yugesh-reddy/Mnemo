"""Pydantic models for the public surface: Event, Fact, Commit, Diff.

These are thin views over rows — the SQL is the source of truth. ``from_row``
mappers keep the SQL ↔ model boundary in one place. The raw ``embedding`` vector is
deliberately not surfaced here.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
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
    session_id: str | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    expires_at: datetime | None = None
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
        for field in ("object_json", "source_span"):
            if isinstance(d.get(field), str):
                d[field] = json.loads(d[field])
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
    fact_key: str = ""
    kind: str = "triple"
    event_id: UUID | None = None
    object_text: str | None = None
    object_number: Decimal | None = None
    object_json: Any | None = None
    provenance: str = "agent_inference"
    confidence: float = 1.0
    trust_level: str = "medium"
    source_span: Any | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
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
        for field in ("object_json", "source_span"):
            if isinstance(d.get(field), str):
                d[field] = json.loads(d[field])
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
    evidence: str | None = Field(default=None, min_length=1)
    """Untrusted quote hint; the worker checks it against the original input."""


class MutationResult(BaseModel):
    status: Literal["applied", "no_change"]
    fact_id: UUID
    event_id: UUID
    previous_event_id: UUID | None = None
    restored_from_event_id: UUID | None = None
    value: str
    request_id: UUID
    replayed: bool = False


class CurrentValue(BaseModel):
    fact_id: UUID
    subject: str
    predicate: str
    value: str
    current_event_id: UUID
    provenance: str
    trust_level: str
    recorded_at: datetime


class HistoricalValue(BaseModel):
    fact_id: UUID
    event_id: UUID
    op: str
    value: str
    provenance: str
    trust_level: str
    actor: str | None
    recorded_at: datetime
    current_event_id: UUID | None
    restorable: bool


class HistoryEntry(BaseModel):
    event_id: UUID
    seq: int
    op: str
    value_preview: str
    value_truncated: bool
    provenance: str
    trust_level: str
    actor: str | None
    recorded_at: datetime
    restored_from_event_id: UUID | None = None


class HistoryPage(BaseModel):
    fact_id: UUID
    current_event_id: UUID | None
    entries: list[HistoryEntry]
    next_cursor: str | None


class SearchHit(BaseModel):
    fact_id: UUID
    event_id: UUID
    subject: str
    predicate: str
    value: str
    score: float
