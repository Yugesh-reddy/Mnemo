"""Core operations — the heart of Mnemo (spec §5).

Async ``MnemoStore`` bound to a single asyncpg connection. Each mutating op is a
transaction. The append-only invariant is sacred: state changes are *new* events;
we only ever flip supersession flags and move the HEAD pointer, never rewrite a
payload.

Identity is by ``fact_key`` (canonical subject|predicate); embedding cosine refines
routing — near-identical restatements (>= update_sim) no-op, and a new key that is
semantically very close to an existing fact (>= dedup_sim) resolves to it. ``search``
is hybrid keyword + vector and merges un-reconciled fast_cache rows (§8) when given a
session. ``observe`` feeds the async extraction worker (mnemo/extraction.py).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from mnemo.config import Settings, get_settings
from mnemo.db import to_vector_literal
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
    valid_from, valid_to, recorded_at, superseded_at, superseded_by, parent_event_id,
    importance, write_score, tier, reason, strength, recall_count, last_used
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
        self.embedder = embedder
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
        embedding: list[float] | None = None,
        importance: int | None = None,
        write_score: float | None = None,
        tier: str = "durable",
        reason: str | None = None,
    ) -> Event:
        object_text, object_number, object_json = _encode_object(value)
        row = await self.conn.fetchrow(
            f"""
            INSERT INTO memory_event
                (fact_id, op, object_text, object_number, object_json, embedding,
                 provenance, actor, confidence, trust_level, source_span,
                 valid_from, parent_event_id,
                 importance, write_score, tier, reason)
            VALUES ($1, $2::mem_op, $3, $4, $5::jsonb, $6::vector,
                    $7::mem_provenance, $8, $9, $10::mem_trust, $11::jsonb,
                    COALESCE($12, now()), $13,
                    $14, $15, $16::mem_tier, $17)
            RETURNING {_EVENT_COLS}
            """,
            fact_id,
            op,
            object_text,
            object_number,
            object_json,
            to_vector_literal(embedding),
            provenance,
            actor,
            confidence,
            trust_level,
            json.dumps(source_span) if source_span is not None else None,
            valid_from,
            parent_event_id,
            importance,
            write_score,
            tier,
            reason,
        )
        return Event.from_row(row)

    async def _embed(self, text: str) -> list[float]:
        """Embed off the event loop (the backend call may hit the network)."""
        return await asyncio.to_thread(self.embedder.embed, text)

    async def _cosine_to_event(self, vec_literal: str | None, event_id: UUID) -> float | None:
        """Cosine similarity between a query vector and an event's stored embedding."""
        if vec_literal is None:
            return None
        value = await self.conn.fetchval(
            "SELECT 1 - (embedding <=> $1::vector) FROM memory_event "
            "WHERE event_id=$2 AND embedding IS NOT NULL",
            vec_literal,
            event_id,
        )
        return float(value) if value is not None else None

    async def _max_cosine(self, vec_literal: str | None) -> float:
        """Best cosine between a candidate and any HEAD fact (0.0 on empty store)."""
        if vec_literal is None:
            return 0.0
        value = await self.conn.fetchval(
            """
            SELECT max(1 - (embedding <=> $4::vector)) FROM memory_current
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND embedding IS NOT NULL
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            vec_literal,
        )
        return float(value) if value is not None else 0.0

    async def _nearest_fact(self, vec_literal: str | None) -> tuple[UUID, UUID, float] | None:
        """Nearest HEAD fact by cosine, if at/above the dedup threshold (entity resolution)."""
        if vec_literal is None:
            return None
        row = await self.conn.fetchrow(
            """
            SELECT fact_id, event_id AS current_event_id,
                   1 - (embedding <=> $4::vector) AS cosine
            FROM memory_current
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND embedding IS NOT NULL
            ORDER BY embedding <=> $4::vector
            LIMIT 1
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            vec_literal,
        )
        if row is None or row["cosine"] is None:
            return None
        if float(row["cosine"]) >= self.settings.dedup_sim:
            return row["fact_id"], row["current_event_id"], float(row["cosine"])
        return None

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
        importance: int = 5,
        write_score: float = 1.0,
        tier: str = "durable",
        reason: str | None = None,
        embedding: list[float] | None = None,
    ) -> Event:
        """Insert a fact value, routing to ADD / UPDATE / no-op (spec §5).

        - Same ``fact_key``: no-op if the value is unchanged or a near-identical
          restatement (cosine >= update_sim); otherwise UPDATE.
        - New ``fact_key``: if a different fact is semantically very close
          (cosine >= dedup_sim), UPDATE *that* fact (entity resolution); else ADD.
        """
        fact_key = canonicalize(subject, predicate)
        trust = derive_trust(provenance, confidence)
        if embedding is None:
            embedding = await self._embed(f"{subject} {predicate} {object}")
        vec = to_vector_literal(embedding)
        emit = dict(
            provenance=provenance,
            actor=actor,
            confidence=confidence,
            trust_level=trust,
            source_span=source_span,
            valid_from=valid_from,
            embedding=embedding,
            importance=importance,
            write_score=write_score,
            tier=tier,
            reason=reason,
        )

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

            # --- existing fact under the same key ---
            if fact is not None:
                return await self._apply_to_existing(fact, object, vec, **emit)

            # --- new key: semantic dedup may resolve to a different fact ---
            match = await self._nearest_fact(vec)
            if match is not None:
                matched_fact_id, matched_event_id, _ = match
                current = await self._get_event(matched_event_id)
                if current is not None and _same_object(current, object):
                    return current
                return await self._emit_update(matched_fact_id, matched_event_id, object, **emit)

            # --- brand-new fact ---
            fact_id = await self._insert_fact(subject, predicate, fact_key, kind, session_id)
            if fact_id is None:
                # Lost the UNIQUE race to a concurrent add: act as the second arrival.
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
                assert fact is not None  # the constraint fired, so the row must exist
                return await self._apply_to_existing(fact, object, vec, **emit)
            event = await self._insert_event(fact_id, "ADD", object, parent_event_id=None, **emit)
            await self._set_head(fact_id, event.event_id)
            return event

    async def _emit_update(
        self, fact_id: UUID, current_event_id: UUID | None, object: Any, **emit: Any
    ) -> Event:
        event = await self._insert_event(
            fact_id, "UPDATE", object, parent_event_id=current_event_id, **emit
        )
        if current_event_id is not None:
            await self._supersede(current_event_id, event.event_id)
        await self._set_head(fact_id, event.event_id)
        return event

    async def _apply_to_existing(
        self, fact: asyncpg.Record, object: Any, vec: str | None, **emit: Any
    ) -> Event:
        """Route a value to an existing fact: no-op on (near-)duplicate, else UPDATE.

        Shared by the same-key path and the concurrent-insert recovery path (the latter
        wins the ``UNIQUE`` race when a parallel ``add`` inserted the fact first).
        """
        current = await self._get_event(fact["current_event_id"])
        if current is not None:
            if _same_object(current, object):
                return current  # exact no-op
            cosine = await self._cosine_to_event(vec, current.event_id)
            if cosine is not None and cosine >= self.settings.update_sim:
                return current  # near-identical restatement: don't churn
        return await self._emit_update(fact["fact_id"], fact["current_event_id"], object, **emit)

    async def _insert_fact(
        self, subject: str, predicate: str, fact_key: str, kind: str, session_id: str | None
    ) -> UUID | None:
        """Insert a brand-new fact. Returns ``None`` if a concurrent add won the race.

        Two parallel ``add`` calls for a new key can both miss the existence check,
        then collide on ``UNIQUE (namespace, user_id, agent_id, fact_key)``. The loser
        would otherwise surface as a raw ``UniqueViolationError``; instead we catch it
        *outside* the savepoint (asyncpg only rolls a savepoint back when the exception
        propagates out of the ``async with`` block — catching inside leaves the
        transaction aborted), so the enclosing ``add`` transaction stays usable and the
        caller can re-fetch and route through the existing-fact path.
        """
        try:
            async with self.conn.transaction():  # savepoint
                return await self.conn.fetchval(
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
        except asyncpg.UniqueViolationError:
            return None  # lost the race to a parallel add

    async def search(
        self,
        query: str,
        *,
        k: int = 8,
        as_of: Any | None = None,
        include_superseded: bool = False,
        session_id: str | None = None,
    ) -> list[Fact]:
        """Hybrid search over HEAD (memory_current): keyword matches first, then
        vector neighbours at/above ``search_floor``.

        When ``session_id`` is given, un-reconciled fast_cache rows for that session
        are merged in (immediate next-turn recall, spec §8). The ``reconciled`` flag
        is the dedup: a cache row is dropped the moment its semantic fact lands, so a
        belief is never double-counted (raw + semantic)."""
        embedding = await self._embed(query)
        vec = to_vector_literal(embedding)
        like = f"%{query}%"
        rows = await self.conn.fetch(
            """
            WITH scored AS (
              SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                     kind, event_id, object_text, object_number, object_json,
                     provenance, confidence, trust_level, valid_from, recorded_at,
                     importance, write_score, tier, strength, recall_count,
                     COALESCE(1 - (embedding <=> $5::vector), 0) AS rel_vec,
                     to_tsvector('english', subject || ' ' || predicate || ' '
                                 || coalesce(object_text, ''))
                         @@ plainto_tsquery('english', $4) AS fts_hit,
                     (subject ILIKE $6 OR predicate ILIKE $6 OR object_text ILIKE $6) AS kw_hit,
                     power($9, EXTRACT(EPOCH FROM (now() - last_used)) / 3600.0) AS recency
              FROM memory_current
              WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
            )
            SELECT *,
                   ($10 * GREATEST(rel_vec,
                                   CASE WHEN fts_hit THEN 0.85 ELSE 0 END,
                                   CASE WHEN kw_hit THEN 0.80 ELSE 0 END)
                    + $11 * recency
                    -- parens matter: int*int context would make PG infer $12 as
                    -- integer and asyncpg truncate the 0.3 weight to 0
                    + $12 * (COALESCE(importance, 5) / 10.0)) AS score
            FROM scored
            WHERE fts_hit OR kw_hit OR rel_vec >= $7
            ORDER BY score DESC
            LIMIT $8
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            query,
            vec,
            like,
            self.settings.search_floor,
            k,
            self.settings.recency_gamma,
            self.settings.search_w_rel,
            self.settings.search_w_rec,
            self.settings.search_w_imp,
        )
        results = [Fact.from_row(r, score=float(r["score"])) for r in rows]
        for fact in results:
            await self.reinforce(fact.fact_id)  # recall reinforcement (S+1, t->0)

        if session_id is not None:
            results.extend(await self._search_fast_cache(query, vec, like, session_id, k))
        return results[:k]

    async def _search_fast_cache(
        self, query: str, vec: str | None, like: str, session_id: str, k: int
    ) -> list[Fact]:
        rows = await self.conn.fetch(
            """
            SELECT cache_id, turn_id, raw_text,
                   CASE WHEN embedding IS NULL THEN NULL
                        ELSE 1 - (embedding <=> $4::vector) END AS score
            FROM fast_cache
            WHERE namespace=$1 AND user_id=$2 AND session_id=$3 AND reconciled = false
              AND (
                  raw_text ILIKE $5
                  OR (embedding IS NOT NULL AND 1 - (embedding <=> $4::vector) >= $6)
              )
            ORDER BY created_at DESC
            LIMIT $7
            """,
            self.namespace,
            self.user_id,
            session_id,
            vec,
            like,
            self.settings.search_floor,
            k,
        )
        return [
            Fact(
                fact_id=r["cache_id"],
                namespace=self.namespace,
                user_id=self.user_id,
                agent_id=self.agent_id,
                session_id=session_id,
                kind="raw",
                event_id=None,
                object_text=r["raw_text"],
                raw_text=r["raw_text"],
                provenance="fast_cache",
                trust_level="low",
                source="fast_cache",
                score=(float(r["score"]) if r["score"] is not None else None),
            )
            for r in rows
        ]

    async def observe(
        self, turn_id: str, text: str, session_id: str, *, role: str = "user"
    ) -> None:
        """Synchronously cache a turn and enqueue extraction (spec §8).

        Writes a fast_cache row (for immediate recall) and an extraction_job (for the
        async worker), in one transaction.
        """
        # Exact-duplicate turn for this session (Mem0-v3-style hash dedup): re-observing
        # identical text would re-embed, re-extract, and double-store the same belief
        # source. md5 comparison keeps the check index-friendly and cheap.
        duplicate = await self.conn.fetchval(
            """
            SELECT 1 FROM fast_cache
            WHERE namespace=$1 AND user_id=$2 AND session_id=$3
              AND md5(raw_text) = md5($4)
            LIMIT 1
            """,
            self.namespace,
            self.user_id,
            session_id,
            text,
        )
        if duplicate:
            return

        embedding = await self._embed(text)
        vec = to_vector_literal(embedding)
        async with self.conn.transaction():
            await self.conn.execute(
                """
                INSERT INTO fast_cache
                    (namespace, user_id, session_id, turn_id, raw_text, embedding)
                VALUES ($1, $2, $3, $4, $5, $6::vector)
                """,
                self.namespace,
                self.user_id,
                session_id,
                turn_id,
                text,
                vec,
            )
            await self.conn.execute(
                """
                INSERT INTO extraction_job
                    (namespace, user_id, agent_id, session_id, turn_id, payload)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                self.namespace,
                self.user_id,
                self.agent_id,
                session_id,
                turn_id,
                json.dumps({"text": text, "role": role}),
            )

    async def list_current(self, *, limit: int = 200) -> list[Fact]:
        """All HEAD facts (most recently changed first) — for the UI list view."""
        rows = await self.conn.fetch(
            """
            SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                   kind, event_id, object_text, object_number, object_json,
                   provenance, confidence, trust_level, valid_from, recorded_at
            FROM memory_current
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
            ORDER BY recorded_at DESC
            LIMIT $4
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            limit,
        )
        return [Fact.from_row(r) for r in rows]

    async def list_commits(self, *, limit: int = 100) -> list[Commit]:
        """Commits in this namespace (most recent first) — for the diff view."""
        rows = await self.conn.fetch(
            """
            SELECT commit_id, namespace, parent_commit_id, label, at_seq, created_by, created_at
            FROM memory_commit
            WHERE namespace=$1
            ORDER BY at_seq DESC, created_at DESC
            LIMIT $2
            """,
            self.namespace,
            limit,
        )
        return [Commit.from_row(r) for r in rows]

    async def get(self, fact_id: UUID) -> Fact | None:
        """The HEAD fact for ``fact_id`` (current believed value), or None."""
        row = await self.conn.fetchrow(
            """
            SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                   kind, event_id, object_text, object_number, object_json,
                   provenance, confidence, trust_level, valid_from, recorded_at
            FROM memory_current
            WHERE fact_id=$1 AND namespace=$2 AND user_id=$3 AND agent_id=$4
            """,
            fact_id,
            self.namespace,
            self.user_id,
            self.agent_id,
        )
        return Fact.from_row(row) if row else None

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
                     provenance, actor, confidence, trust_level, parent_event_id,
                     importance, write_score, tier, reason)
                VALUES ($1, 'REVERT', $2, $3, $4::jsonb,
                        'human_review', $5, 1.0, 'high', $6,
                        $7, 1.0, $8::mem_tier, $9)
                RETURNING {_EVENT_COLS}
                """,
                fact_id,
                target.object_text,
                target.object_number,
                object_json,
                actor,
                to_event_id,
                target.importance,
                target.tier,
                f"revert to {str(to_event_id)[:8]}",
            )
            event = Event.from_row(row)

            current_id = fact["current_event_id"]
            if current_id is not None and current_id != event.event_id:
                await self._supersede(current_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            # Revert is the undo for invalidate() too: reactivate the fact.
            await self.conn.execute(
                "UPDATE memory_fact SET status='active' WHERE fact_id=$1", fact_id
            )
            return event

    async def reinforce(self, fact_id: UUID) -> None:
        """Recall reinforcement (MemoryBank): S += 1, t -> 0 on the live event.

        strength/recall_count/last_used are the documented mutable decay
        bookkeeping — never payload."""
        await self.conn.execute(
            """
            UPDATE memory_event SET strength = strength + 1,
                   recall_count = recall_count + 1, last_used = now()
            WHERE event_id = (SELECT current_event_id FROM memory_fact WHERE fact_id=$1)
            """,
            fact_id,
        )

    async def invalidate(
        self, fact_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> Event:
        """Mark a fact no longer true in the world (bitemporal): append an
        INVALIDATE event with valid_to=now(). Reversible via revert()."""
        async with self.conn.transaction():
            fact = await self.conn.fetchrow(
                "SELECT current_event_id FROM memory_fact WHERE fact_id=$1", fact_id
            )
            if fact is None or fact["current_event_id"] is None:
                raise ValueError(f"fact {fact_id} not found")
            current = await self._get_event(fact["current_event_id"])

            object_json = current.object_json
            if object_json is not None and not isinstance(object_json, str):
                object_json = json.dumps(object_json, sort_keys=True)

            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json,
                     provenance, actor, confidence, trust_level, parent_event_id,
                     importance, tier, reason, valid_to)
                VALUES ($1, 'INVALIDATE', $2, $3, $4::jsonb,
                        $5::mem_provenance, $6, $7, $8::mem_trust, $9,
                        $10, $11::mem_tier, $12, now())
                RETURNING {_EVENT_COLS}
                """,
                fact_id,
                current.object_text,
                current.object_number,
                object_json,
                current.provenance,
                actor,
                current.confidence,
                current.trust_level,
                current.event_id,
                current.importance,
                current.tier,
                reason or "invalidated",
            )
            event = Event.from_row(row)
            await self._supersede(current.event_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            await self.conn.execute(
                "UPDATE memory_fact SET status='invalidated' WHERE fact_id=$1", fact_id
            )
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
