"""Core operations — the heart of Mnemo (spec §5).

Async ``MnemoStore`` bound to a single asyncpg connection. Each mutating op is a
transaction. The append-only invariant is sacred: state changes are *new* events;
we only ever flip supersession flags and move the HEAD pointer, never rewrite a
payload.

M2 scope: identity/dedup is by ``fact_key`` (canonical subject|predicate). Embedding
similarity routing and vector search arrive in M3 — embeddings are stored NULL here.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from mnemo.config import Settings, get_settings
from mnemo.models import Commit, Diff, DiffEntry, Event, Fact

# Predicate synonyms collapsed to a canonical key (spec §5 step 1). Small + in-code.
PREDICATE_ALIASES: dict[str, str] = {
    "favorite_db": "preferred_database",
    "favourite_db": "preferred_database",
    "favorite_database": "preferred_database",
    "favourite_database": "preferred_database",
    "preferred_db": "preferred_database",
    "favorite_language": "preferred_language",
    "favourite_language": "preferred_language",
    "preferred_lang": "preferred_language",
}

# Every column needed to hydrate an Event, in one place.
_EVENT_COLS = """
    event_id, seq, fact_id, op, object_text, object_number, object_json,
    provenance, actor, confidence, trust_level, source_span,
    valid_from, valid_to, recorded_at, superseded_at, superseded_by, parent_event_id
"""


def _collapse(text: str) -> str:
    return " ".join(text.strip().lower().split())


def canonicalize(subject: str, predicate: str) -> str:
    """Stable identity key for a fact: ``"<subject>|<predicate>"``, alias-normalized."""
    subj = _collapse(subject)
    pred = _collapse(predicate)
    pred = PREDICATE_ALIASES.get(pred, pred)
    return f"{subj}|{pred}"


def derive_trust(provenance: str, confidence: float) -> str:
    """Map provenance (+ confidence) to a trust level (spec §7)."""
    if provenance in ("direct_user_statement", "human_review"):
        return "high"
    if provenance in ("tool_output", "document"):
        return "medium"
    return "low"  # agent_inference (already the floor; low confidence can't go lower)


def _encode_object(value: Any) -> tuple[str | None, Decimal | None, str | None]:
    """Split a Python value into (object_text, object_number, object_json)."""
    if value is None:
        return None, None, None
    if isinstance(value, str):
        return value, None, None
    if isinstance(value, bool):
        return str(value), None, None
    if isinstance(value, (int, float, Decimal)):
        return None, Decimal(str(value)), None
    if isinstance(value, (dict, list)):
        return None, None, json.dumps(value, sort_keys=True)
    return str(value), None, None


def _same_object(event: Event, value: Any) -> bool:
    """True if ``value`` is effectively equal to the event's stored object."""
    text, number, js = _encode_object(value)
    if text is not None:
        return event.object_text == text
    if number is not None:
        return event.object_number == number
    if js is not None:
        existing = event.object_json
        if isinstance(existing, str):
            existing = json.loads(existing)
        return json.dumps(existing, sort_keys=True) == js
    return event.object_text is None and event.object_number is None and event.object_json is None


class MnemoStore:
    """Async core. One instance wraps one connection (callers manage pools/txns)."""

    def __init__(
        self,
        conn: asyncpg.Connection,
        embedder: Any,
        *,
        namespace: str = "default",
        user_id: str = "default",
        agent_id: str = "default",
        settings: Settings | None = None,
    ) -> None:
        self.conn = conn
        self.embedder = embedder  # stored for M3 (vector routing + search)
        self.namespace = namespace
        self.user_id = user_id
        self.agent_id = agent_id
        self.settings = settings or get_settings()

    # ---- internal helpers -----------------------------------------------

    async def _get_event(self, event_id: UUID | None) -> Event | None:
        if event_id is None:
            return None
        row = await self.conn.fetchrow(
            f"SELECT {_EVENT_COLS} FROM memory_event WHERE event_id=$1", event_id
        )
        return Event.from_row(row) if row else None

    async def _insert_event(
        self,
        fact_id: UUID,
        op: str,
        value: Any,
        *,
        provenance: str,
        actor: str | None,
        confidence: float,
        trust_level: str,
        source_span: Any | None,
        valid_from: Any | None,
        parent_event_id: UUID | None,
    ) -> Event:
        object_text, object_number, object_json = _encode_object(value)
        row = await self.conn.fetchrow(
            f"""
            INSERT INTO memory_event
                (fact_id, op, object_text, object_number, object_json,
                 provenance, actor, confidence, trust_level, source_span,
                 valid_from, parent_event_id)
            VALUES ($1, $2::mem_op, $3, $4, $5::jsonb,
                    $6::mem_provenance, $7, $8, $9::mem_trust, $10::jsonb,
                    COALESCE($11, now()), $12)
            RETURNING {_EVENT_COLS}
            """,
            fact_id,
            op,
            object_text,
            object_number,
            object_json,
            provenance,
            actor,
            confidence,
            trust_level,
            json.dumps(source_span) if source_span is not None else None,
            valid_from,
            parent_event_id,
        )
        return Event.from_row(row)

    async def _set_head(self, fact_id: UUID, event_id: UUID) -> None:
        await self.conn.execute(
            "UPDATE memory_fact SET current_event_id=$1 WHERE fact_id=$2", event_id, fact_id
        )

    async def _supersede(self, old_event_id: UUID, new_event_id: UUID) -> None:
        await self.conn.execute(
            "UPDATE memory_event SET superseded_at=now(), superseded_by=$2 WHERE event_id=$1",
            old_event_id,
            new_event_id,
        )

    async def _fact_id_for_key(self, fact_key: str) -> UUID | None:
        return await self.conn.fetchval(
            """
            SELECT fact_id FROM memory_fact
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND fact_key=$4
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            fact_key,
        )

    # ---- public ops -----------------------------------------------------

    async def add(
        self,
        subject: str,
        predicate: str,
        object: Any,
        *,
        kind: str = "triple",
        provenance: str = "agent_inference",
        actor: str | None = None,
        confidence: float = 1.0,
        source_span: Any | None = None,
        valid_from: Any | None = None,
        session_id: str | None = None,
    ) -> Event:
        """Insert a fact value: ADD a new fact, UPDATE an existing one, or no-op."""
        fact_key = canonicalize(subject, predicate)
        trust = derive_trust(provenance, confidence)

        async with self.conn.transaction():
            fact = await self.conn.fetchrow(
                """
                SELECT fact_id, current_event_id FROM memory_fact
                WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND fact_key=$4
                """,
                self.namespace,
                self.user_id,
                self.agent_id,
                fact_key,
            )

            if fact is None:
                fact_id = await self.conn.fetchval(
                    """
                    INSERT INTO memory_fact
                        (namespace, user_id, agent_id, session_id,
                         subject, predicate, fact_key, kind)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::mem_kind)
                    RETURNING fact_id
                    """,
                    self.namespace,
                    self.user_id,
                    self.agent_id,
                    session_id,
                    subject,
                    predicate,
                    fact_key,
                    kind,
                )
                event = await self._insert_event(
                    fact_id,
                    "ADD",
                    object,
                    provenance=provenance,
                    actor=actor,
                    confidence=confidence,
                    trust_level=trust,
                    source_span=source_span,
                    valid_from=valid_from,
                    parent_event_id=None,
                )
                await self._set_head(fact_id, event.event_id)
                return event

            fact_id = fact["fact_id"]
            current = await self._get_event(fact["current_event_id"])
            if current is not None and _same_object(current, object):
                return current  # no-op: same belief already at HEAD

            event = await self._insert_event(
                fact_id,
                "UPDATE",
                object,
                provenance=provenance,
                actor=actor,
                confidence=confidence,
                trust_level=trust,
                source_span=source_span,
                valid_from=valid_from,
                parent_event_id=fact["current_event_id"],
            )
            if fact["current_event_id"] is not None:
                await self._supersede(fact["current_event_id"], event.event_id)
            await self._set_head(fact_id, event.event_id)
            return event

    async def search(
        self,
        query: str,
        *,
        k: int = 8,
        as_of: Any | None = None,
        include_superseded: bool = False,
        session_id: str | None = None,
    ) -> list[Fact]:
        """Keyword search over HEAD (memory_current). Vector + fast-cache merge: M3/M4."""
        like = f"%{query}%"
        rows = await self.conn.fetch(
            """
            SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                   kind, event_id, object_text, object_number, object_json,
                   provenance, confidence, trust_level, valid_from, recorded_at
            FROM memory_current
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
              AND (subject ILIKE $4 OR predicate ILIKE $4 OR object_text ILIKE $4)
            ORDER BY recorded_at DESC
            LIMIT $5
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            like,
            k,
        )
        return [Fact.from_row(r) for r in rows]

    async def blame(
        self,
        *,
        fact_id: UUID | None = None,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[Event]:
        """Full ordered event history for a fact (git blame for memory)."""
        fid = await self._resolve_fact_id(fact_id, subject, predicate)
        if fid is None:
            return []
        rows = await self.conn.fetch(
            f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id=$1 ORDER BY seq", fid
        )
        return [Event.from_row(r) for r in rows]

    async def _resolve_fact_id(
        self, fact_id: UUID | None, subject: str | None, predicate: str | None
    ) -> UUID | None:
        if fact_id is not None:
            return fact_id
        if subject is not None and predicate is not None:
            return await self._fact_id_for_key(canonicalize(subject, predicate))
        raise ValueError("provide fact_id or both subject and predicate")

    async def revert(
        self, fact_id: UUID, to_event_id: UUID, *, actor: str = "human_review"
    ) -> Event:
        """Roll a fact back to a prior event's value via a new REVERT event."""
        async with self.conn.transaction():
            target = await self._get_event(to_event_id)
            if target is None:
                raise ValueError(f"event {to_event_id} not found")
            fact = await self.conn.fetchrow(
                "SELECT current_event_id FROM memory_fact WHERE fact_id=$1", fact_id
            )
            if fact is None:
                raise ValueError(f"fact {fact_id} not found")

            object_json = target.object_json
            if object_json is not None and not isinstance(object_json, str):
                object_json = json.dumps(object_json, sort_keys=True)

            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json,
                     provenance, actor, confidence, trust_level, parent_event_id)
                VALUES ($1, 'REVERT', $2, $3, $4::jsonb,
                        'human_review', $5, 1.0, 'high', $6)
                RETURNING {_EVENT_COLS}
                """,
                fact_id,
                target.object_text,
                target.object_number,
                object_json,
                actor,
                to_event_id,
            )
            event = Event.from_row(row)

            current_id = fact["current_event_id"]
            if current_id is not None and current_id != event.event_id:
                await self._supersede(current_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            return event

    async def log(self, *, fact_id: UUID | None = None, limit: int = 50) -> list[Event]:
        """Event history for a fact (newest first), or the recent global timeline."""
        if fact_id is not None:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id=$1 "
                f"ORDER BY seq DESC LIMIT $2",
                fact_id,
                limit,
            )
        else:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event ORDER BY seq DESC LIMIT $1", limit
            )
        return [Event.from_row(r) for r in rows]

    async def commit(self, label: str | None = None) -> Commit:
        """Capture the current high-water seq as a named, chained commit."""
        async with self.conn.transaction():
            at_seq = await self.conn.fetchval("SELECT COALESCE(max(seq), 0) FROM memory_event")
            parent = await self.conn.fetchval(
                "SELECT commit_id FROM memory_commit WHERE namespace=$1 "
                "ORDER BY at_seq DESC, created_at DESC LIMIT 1",
                self.namespace,
            )
            row = await self.conn.fetchrow(
                """
                INSERT INTO memory_commit (namespace, parent_commit_id, label, at_seq, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING commit_id, namespace, parent_commit_id, label, at_seq,
                          created_by, created_at
                """,
                self.namespace,
                parent,
                label,
                at_seq,
                self.user_id,
            )
            return Commit.from_row(row)

    async def diff(self, commit_a: Commit | UUID, commit_b: Commit | UUID) -> Diff:
        """Diff fact state between two commits, emitting added / removed / changed."""
        ca = commit_a.commit_id if isinstance(commit_a, Commit) else commit_a
        cb = commit_b.commit_id if isinstance(commit_b, Commit) else commit_b

        seq_a = await self.conn.fetchval("SELECT at_seq FROM memory_commit WHERE commit_id=$1", ca)
        seq_b = await self.conn.fetchval("SELECT at_seq FROM memory_commit WHERE commit_id=$1", cb)
        if seq_a is None or seq_b is None:
            raise ValueError("unknown commit id")

        rows_a = await self.conn.fetch(
            "SELECT * FROM fact_state_as_of($1, $2, $3)", self.namespace, self.user_id, seq_a
        )
        rows_b = await self.conn.fetch(
            "SELECT * FROM fact_state_as_of($1, $2, $3)", self.namespace, self.user_id, seq_b
        )
        a = {r["fact_id"]: r for r in rows_a}
        b = {r["fact_id"]: r for r in rows_b}

        entries: list[DiffEntry] = []
        for fid in set(a) | set(b):
            ra, rb = a.get(fid), b.get(fid)
            if ra is not None and rb is None:
                entries.append(_entry(ra, "removed", old=_row_value(ra), new=None))
            elif rb is not None and ra is None:
                entries.append(_entry(rb, "added", old=None, new=_row_value(rb)))
            elif _row_value(ra) != _row_value(rb):
                entries.append(_entry(rb, "changed", old=_row_value(ra), new=_row_value(rb)))

        entries.sort(key=lambda e: (e.predicate, e.subject))
        return Diff(commit_a=ca, commit_b=cb, seq_a=seq_a, seq_b=seq_b, entries=entries)


def _row_value(row: Any) -> Any:
    if row["object_text"] is not None:
        return row["object_text"]
    if row["object_number"] is not None:
        return row["object_number"]
    return row["object_json"]


def _entry(row: Any, change: str, *, old: Any, new: Any) -> DiffEntry:
    return DiffEntry(
        fact_id=row["fact_id"],
        subject=row["subject"],
        predicate=row["predicate"],
        change=change,
        old=old,
        new=new,
    )
