"""Core operations — the heart of Mnemo (spec §5).

Async ``MnemoStore`` bound to a single asyncpg connection. Each mutating op is a
transaction. The append-only invariant is sacred: state changes are *new* events;
we only ever flip supersession flags and move the HEAD pointer, never rewrite a
payload.

Identity is by ``fact_key`` (canonical subject|predicate). Exact values and narrow
identity aliases no-op; changed values append revisions regardless of cosine. ``search``
is hybrid keyword + vector and merges un-reconciled fast_cache rows (§8) when given a
session. ``observe`` feeds the async extraction worker (mnemo/extraction.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
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
    "database_system": "db_engine",
    "preferred_lang": "preferred_language",
}

# Every column needed to hydrate an Event, in one place.
_EVENT_COLS = """
    event_id, seq, fact_id, op, object_text, object_number, object_json,
    provenance, actor, confidence, trust_level, source_span,
    session_id, valid_from, valid_to, expires_at, recorded_at,
    superseded_at, superseded_by, parent_event_id,
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
        if event.object_text == text:
            return True
        # Narrow spelling aliases, never embedding similarity, establish equivalence.
        database_names = {"postgres", "postgresql", "psql"}
        return (
            text.casefold() in database_names
            and (event.object_text or "").casefold() in database_names
        )
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

    async def _lock_scope(self) -> None:
        """Serialize writes and checkpoints within this store scope."""
        await self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"{self.namespace}\x1f{self.user_id}\x1f{self.agent_id}",
        )

    async def _locked_fact(self, fact_id: UUID) -> asyncpg.Record | None:
        return await self.conn.fetchrow(
            """
            SELECT fact_id, current_event_id, status FROM memory_fact
            WHERE fact_id=$1 AND namespace=$2 AND user_id=$3 AND agent_id=$4
            FOR UPDATE
            """,
            fact_id,
            self.namespace,
            self.user_id,
            self.agent_id,
        )

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
        session_id: str | None = None,
        expires_at: Any | None = None,
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
                 session_id, valid_from, expires_at, parent_event_id,
                 importance, write_score, tier, reason)
            VALUES ($1, $2::mem_op, $3, $4, $5::jsonb, $6::vector,
                    $7::mem_provenance, $8, $9, $10::mem_trust, $11::jsonb,
                    $12, COALESCE($13, now()), $14, $15,
                    $16, $17, $18::mem_tier, $19)
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
            session_id,
            valid_from,
            expires_at,
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

        Exact values and narrow aliases under the same key are idempotent while
        visible. Changed values append UPDATE even with identical embeddings;
        unrelated identities create ADD.
        """
        fact_key = canonicalize(subject, predicate)
        trust = derive_trust(provenance, confidence)
        if tier == "session" and session_id is None:
            raise ValueError("session tier requires session_id")
        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=self.settings.session_ttl_seconds)
            if tier == "session"
            else None
        )
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
            session_id=session_id,
            expires_at=expires_at,
            embedding=embedding,
            importance=importance,
            write_score=write_score,
            tier=tier,
            reason=reason,
        )

        async with self.conn.transaction():
            await self._lock_scope()
            fact = await self.conn.fetchrow(
                """
                SELECT fact_id, current_event_id, status FROM memory_fact
                WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND fact_key=$4
                FOR UPDATE
                """,
                self.namespace,
                self.user_id,
                self.agent_id,
                fact_key,
            )

            # --- existing fact under the same key ---
            if fact is not None:
                return await self._apply_to_existing(fact, object, vec, **emit)

            # --- brand-new fact ---
            fact_id = await self._insert_fact(subject, predicate, fact_key, kind, session_id)
            if fact_id is None:
                # Lost the UNIQUE race to a concurrent add: act as the second arrival.
                fact = await self.conn.fetchrow(
                    """
                    SELECT fact_id, current_event_id, status FROM memory_fact
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
        """Route a value to an existing fact: no-op on a visible duplicate, else UPDATE.

        Shared by the same-key path and the concurrent-insert recovery path (the latter
        wins the ``UNIQUE`` race when a parallel ``add`` inserted the fact first).
        """
        current = await self._get_event(fact["current_event_id"])
        if current is not None:
            now = datetime.now(tz=UTC)
            if (
                _same_object(current, object)
                and fact["status"] == "active"
                and current.tier != "ephemeral"
                and current.valid_from <= now
                and (current.valid_to is None or current.valid_to > now)
                and (current.expires_at is None or current.expires_at > now)
                and (current.tier != "session" or current.session_id == emit.get("session_id"))
                and (emit.get("valid_from") is None or emit["valid_from"] == current.valid_from)
            ):
                return current
        await self.conn.execute(
            "UPDATE memory_fact SET status='active' WHERE fact_id=$1", fact["fact_id"]
        )
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
        valid_at: datetime | None = None,
        reinforce: bool | None = None,
    ) -> list[Fact]:
        """Hybrid search over HEAD (memory_current): keyword matches first, then
        vector neighbours at/above ``search_floor``.

        When ``session_id`` is given, un-reconciled fast_cache rows for that session
        are merged in (immediate next-turn recall, spec §8). The ``reconciled`` flag
        is the dedup: a cache row is dropped the moment its semantic fact lands, so a
        belief is never double-counted (raw + semantic)."""
        if not 1 <= k <= 1000:
            raise ValueError("k must be 1..1000")
        for name, value in (("as_of", as_of), ("valid_at", valid_at)):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError(f"{name} must be a timezone-aware datetime")
        if include_superseded and as_of is None:
            as_of = datetime.now(tz=UTC)
        embedding = await self._embed(query)
        vec = to_vector_literal(embedding)
        like = f"%{query}%"
        candidate_limit = max(k, k * self.settings.search_candidate_multiplier)
        recorded_at = as_of
        if recorded_at is not None and not isinstance(recorded_at, datetime):
            raise TypeError("as_of must be a timezone-aware datetime")
        if valid_at is not None and not isinstance(valid_at, datetime):
            raise TypeError("valid_at must be a timezone-aware datetime")
        if valid_at is not None and recorded_at is None:
            recorded_at = datetime.now(tz=UTC)
        valid_at = valid_at or recorded_at

        async with self.conn.transaction(
            isolation=None if self.conn.is_in_transaction() else "repeatable_read"
        ):
            rows = await self._search_semantic(
                query,
                vec,
                like,
                candidate_limit,
                session_id,
                recorded_at,
                valid_at,
                include_superseded,
            )
            results = [Fact.from_row(r, score=float(r["score"])) for r in rows]
            if session_id is not None and recorded_at is None:
                cache = await self._search_fast_cache(query, vec, like, session_id, candidate_limit)
                semantic_caches = _source_cache_ids(results)
                results.extend(f for f in cache if str(f.fact_id) not in semantic_caches)

        results.sort(key=lambda fact: (fact.score or 0.0, str(fact.fact_id)), reverse=True)
        results = results[:k]
        should_reinforce = self.settings.search_reinforce if reinforce is None else reinforce
        if should_reinforce and recorded_at is None:
            for fact in results:
                if fact.source == "semantic":
                    await self.reinforce(fact.fact_id)
        return results

    async def _search_semantic(
        self,
        query: str,
        vec: str | None,
        like: str,
        limit: int,
        session_id: str | None,
        recorded_at: datetime | None,
        valid_at: datetime | None,
        include_superseded: bool,
    ) -> list[asyncpg.Record]:
        if recorded_at is None:
            source = (
                "SELECT * FROM memory_current WHERE namespace=$1 AND user_id=$2 AND agent_id=$3"
            )
        elif include_superseded:
            source = """
                SELECT f.fact_id, $1::text AS namespace, $2::text AS user_id,
                       $3::text AS agent_id, e.session_id, f.subject, f.predicate,
                       f.fact_key, f.kind, e.event_id, e.object_text, e.object_number,
                       e.object_json, e.embedding, e.provenance, e.actor, e.confidence,
                       e.trust_level, e.source_span, e.valid_from, e.valid_to,
                       e.expires_at, e.recorded_at, e.importance, e.write_score,
                       e.tier, e.strength, e.recall_count, e.last_used
                FROM memory_event e JOIN memory_fact f USING (fact_id)
                WHERE f.namespace=$1 AND f.user_id=$2 AND f.agent_id=$3
                  AND e.recorded_at <= $14 AND e.valid_from <= $15
                  AND (e.valid_to IS NULL OR e.valid_to > $15)
                  AND (e.expires_at IS NULL OR e.expires_at > $14)
                  AND e.op <> 'DELETE' AND e.tier IN ('durable', 'session')
            """
        else:
            source = """
                SELECT s.*, $1::text AS namespace, $2::text AS user_id, $3::text AS agent_id
                FROM fact_snapshot_at($1, $2, $3, $14, $15) s
            """
        vector_source = """
            SELECT * FROM eligible WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $5::vector LIMIT $8
        """
        if recorded_at is None:
            # Keep the distance scan on the indexed table. The correlated lookup
            # avoids multiplying the planner's independent HEAD/id selectivities.
            vector_source = """
              SELECT * FROM eligible WHERE event_id IN (
                SELECT e.event_id FROM memory_event e
                WHERE e.embedding IS NOT NULL
                  AND e.op IN ('ADD','UPDATE','REVERT')
                  AND e.valid_from <= now()
                  AND (e.valid_to IS NULL OR e.valid_to > now())
                  AND (e.expires_at IS NULL OR e.expires_at > now())
                  AND (e.tier='durable' OR (e.tier='session'
                    AND (e.session_id=$13 OR e.session_id IS NULL)))
                  AND EXISTS (
                    SELECT 1 FROM memory_fact f WHERE f.fact_id=e.fact_id
                      AND f.current_event_id=e.event_id AND f.status='active'
                      AND (e.tier='durable' OR COALESCE(e.session_id,f.session_id)=$13)
                      AND f.namespace=$1 AND f.user_id=$2 AND f.agent_id=$3 OFFSET 0
                  )
                ORDER BY e.embedding <=> $5::vector LIMIT $8
              )
            """
        sql = f"""
            WITH source AS NOT MATERIALIZED ({source}),
            eligible AS NOT MATERIALIZED (
              SELECT * FROM source
              WHERE (tier='durable' OR (tier='session' AND session_id=$13::text))
                AND ($15::timestamptz IS NULL OR valid_from <= $15)
            ),
            vector_candidates AS MATERIALIZED ({vector_source}),
            lexical_candidates AS (
              SELECT * FROM eligible
              WHERE to_tsvector('english', subject || ' ' || predicate || ' '
                    || coalesce(object_text, '')) @@ plainto_tsquery('english', $4)
                 OR subject ILIKE $6 OR predicate ILIKE $6 OR object_text ILIKE $6
              ORDER BY recorded_at DESC LIMIT $8
            ),
            candidates AS (
              SELECT * FROM vector_candidates UNION ALL
              SELECT * FROM lexical_candidates
              WHERE event_id NOT IN (SELECT event_id FROM vector_candidates)
            ), scored AS (
              SELECT *, COALESCE(1 - (embedding <=> $5::vector), 0) AS rel_vec,
                to_tsvector('english', subject || ' ' || predicate || ' '
                  || coalesce(object_text, '')) @@ plainto_tsquery('english', $4) AS fts_hit,
                (subject ILIKE $6 OR predicate ILIKE $6 OR object_text ILIKE $6) AS kw_hit,
                power($9, EXTRACT(EPOCH FROM (COALESCE($14::timestamptz, now())
                  - LEAST(last_used, COALESCE($14::timestamptz,
                       now()))))/3600.0)
                  AS recency
              FROM candidates
            )
            SELECT *, ($10 * GREATEST(rel_vec, CASE WHEN fts_hit THEN 0.85 ELSE 0 END,
                                      CASE WHEN kw_hit THEN 0.80 ELSE 0 END)
                       + $11 * recency
                       + $12 * (COALESCE(importance, 5) / 10.0)) AS score
            FROM scored WHERE fts_hit OR kw_hit OR rel_vec >= $7
            ORDER BY score DESC LIMIT $8
        """
        return await self.conn.fetch(
            sql,
            self.namespace,
            self.user_id,
            self.agent_id,
            query,
            vec,
            like,
            self.settings.search_floor,
            limit,
            self.settings.recency_gamma,
            self.settings.search_w_rel,
            self.settings.search_w_rec,
            self.settings.search_w_imp,
            session_id,
            recorded_at,
            valid_at,
        )

    async def _search_fast_cache(
        self, query: str, vec: str | None, like: str, session_id: str, k: int
    ) -> list[Fact]:
        rows = await self.conn.fetch(
            """WITH candidates AS (
              SELECT *, COALESCE(1-(embedding <=> $5::vector),0) AS cosine,
                to_tsvector('english', raw_text) @@ plainto_tsquery('english',$6) AS lexical,
                raw_text ILIKE $7 AS keyword
              FROM fast_cache WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
                AND session_id=$4 AND role <> 'assistant' AND NOT reconciled
                AND created_at > now()-make_interval(secs => $8)
            ) SELECT *, $9*GREATEST(cosine, CASE WHEN lexical THEN 0.85 ELSE 0 END,
                CASE WHEN keyword THEN 0.8 ELSE 0 END)
                + $10*power($11, GREATEST(0,EXTRACT(EPOCH FROM (now()-created_at))/3600))
                + $12*0.5 AS score FROM candidates
              WHERE lexical OR keyword OR cosine >= $13
              ORDER BY score DESC, cache_id LIMIT $14""",
            self.namespace,
            self.user_id,
            self.agent_id,
            session_id,
            vec,
            query,
            like,
            self.settings.session_ttl_seconds,
            self.settings.search_w_rel,
            self.settings.search_w_rec,
            self.settings.recency_gamma,
            self.settings.search_w_imp,
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
                tier="session",
                score=float(r["score"]),
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
        if role not in {"user", "assistant", "tool", "document"}:
            raise ValueError("role must be user, assistant, tool, or document")
        if not session_id or not turn_id:
            raise ValueError("session_id and turn_id are required")
        ingest_key = hashlib.sha256(f"{role}\0{turn_id}\0{text}".encode()).hexdigest()
        args = (self.namespace, self.user_id, self.agent_id, session_id, ingest_key)
        duplicate = await self.conn.fetchval(
            "SELECT 1 FROM fast_cache WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 "
            "AND session_id=$4 AND ingest_key=$5",
            *args,
        )
        if duplicate or await self._latest_observation_matches(session_id, role, text):
            return
        embedding = await self._embed(text) if role != "assistant" else None
        async with self.conn.transaction():
            await self._lock_scope()
            if await self._latest_observation_matches(session_id, role, text):
                return
            cache_id = await self.conn.fetchval(
                """INSERT INTO fast_cache
                    (namespace, user_id, agent_id, session_id, ingest_key,
                     turn_id, raw_text, embedding, role, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9,clock_timestamp())
                   ON CONFLICT (namespace,user_id,agent_id,session_id,ingest_key)
                     WHERE ingest_key IS NOT NULL DO NOTHING RETURNING cache_id""",
                *args,
                turn_id,
                text,
                to_vector_literal(embedding),
                role,
            )
            if cache_id is None:
                return
            await self.conn.execute(
                """INSERT INTO extraction_job
                    (namespace, user_id, agent_id, session_id, turn_id, payload,
                     cache_id, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,clock_timestamp())""",
                self.namespace,
                self.user_id,
                self.agent_id,
                session_id,
                turn_id,
                json.dumps({"text": text, "role": role}),
                cache_id,
            )

    async def _latest_observation_matches(self, session_id: str, role: str, text: str) -> bool:
        row = await self.conn.fetchrow(
            "SELECT role, raw_text FROM fast_cache WHERE namespace=$1 AND user_id=$2 "
            "AND agent_id=$3 AND session_id=$4 ORDER BY created_at DESC LIMIT 1",
            self.namespace,
            self.user_id,
            self.agent_id,
            session_id,
        )
        return row is not None and row["role"] == role and row["raw_text"] == text

    async def list_current(self, *, limit: int = 200) -> list[Fact]:
        """All HEAD facts (most recently changed first) — for the UI list view."""
        rows = await self.conn.fetch(
            """
            SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                   kind, event_id, object_text, object_number, object_json,
                   provenance, confidence, trust_level, valid_from, recorded_at,
                   importance, write_score, tier, strength, recall_count,
                   source_span, valid_to, expires_at, fact_key
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
            SELECT commit_id, namespace, user_id, agent_id, parent_commit_id, label,
                   at_seq, created_by, created_at
            FROM memory_commit
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
            ORDER BY at_seq DESC, created_at DESC
            LIMIT $4
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            limit,
        )
        return [Commit.from_row(r) for r in rows]

    async def get(self, fact_id: UUID) -> Fact | None:
        """The HEAD fact for ``fact_id`` (current believed value), or None."""
        row = await self.conn.fetchrow(
            """
            SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                   kind, event_id, object_text, object_number, object_json,
                   provenance, confidence, trust_level, valid_from, recorded_at,
                   importance, write_score, tier, strength, recall_count,
                   source_span, valid_to, expires_at, fact_key
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
            return await self.conn.fetchval(
                """
                SELECT fact_id FROM memory_fact
                WHERE fact_id=$1 AND namespace=$2 AND user_id=$3 AND agent_id=$4
                """,
                fact_id,
                self.namespace,
                self.user_id,
                self.agent_id,
            )
        if subject is not None and predicate is not None:
            return await self._fact_id_for_key(canonicalize(subject, predicate))
        raise ValueError("provide fact_id or both subject and predicate")

    async def revert(
        self, fact_id: UUID, to_event_id: UUID, *, actor: str = "human_review"
    ) -> Event:
        """Roll a fact back to a prior event's value via a new REVERT event."""
        async with self.conn.transaction():
            await self._lock_scope()
            fact = await self._locked_fact(fact_id)
            if fact is None:
                raise ValueError(f"fact {fact_id} not found")
            target = await self.conn.fetchrow(
                "SELECT * FROM memory_event WHERE event_id=$1 AND fact_id=$2", to_event_id, fact_id
            )
            if target is None:
                raise ValueError(f"event {to_event_id} does not belong to fact {fact_id}")

            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json, embedding,
                     provenance, actor, confidence, trust_level, source_span,
                     session_id, expires_at, valid_from, parent_event_id, importance, write_score,
                       tier,
                     reason, strength, recall_count, last_used)
                SELECT fact_id, 'REVERT', object_text, object_number, object_json, embedding,
                       'human_review', $3, 1.0, 'high', source_span,
                       COALESCE(session_id, $5),
                       CASE WHEN tier='session' THEN
                           clock_timestamp()+make_interval(secs => $6) END,
                       now(), event_id, importance, write_score, tier,
                       $4, strength, recall_count, last_used
                FROM memory_event WHERE event_id=$1 AND fact_id=$2
                RETURNING {_EVENT_COLS}
                """,
                to_event_id,
                fact_id,
                actor,
                f"revert to {str(to_event_id)[:8]}",
                await self.conn.fetchval(
                    "SELECT session_id FROM memory_fact WHERE fact_id=$1", fact_id
                ),
                self.settings.session_ttl_seconds,
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
        async with self.conn.transaction():
            await self._lock_scope()
            fact = await self._locked_fact(fact_id)
            if fact is None:
                return
            await self.conn.execute(
                """
                UPDATE memory_event SET strength = strength + 1,
                       recall_count = recall_count + 1, last_used = now()
                WHERE event_id=$1
                """,
                fact["current_event_id"],
            )

    async def invalidate(
        self, fact_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> Event:
        """Mark a fact no longer true in the world (bitemporal): append an
        INVALIDATE event with valid_to=now(). Reversible via revert()."""
        async with self.conn.transaction():
            await self._lock_scope()
            fact = await self._locked_fact(fact_id)
            if fact is None or fact["current_event_id"] is None:
                raise ValueError(f"fact {fact_id} not found")
            current = await self._get_event(fact["current_event_id"])

            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json, embedding,
                     provenance, actor, confidence, trust_level, source_span,
                     session_id, expires_at, valid_from, valid_to, parent_event_id, importance,
                       write_score,
                     tier, reason, strength, recall_count, last_used)
                SELECT fact_id, 'INVALIDATE', object_text, object_number, object_json, embedding,
                       provenance, $2, confidence, trust_level, source_span,
                       session_id, expires_at, valid_from, now(), event_id, importance, write_score,
                       tier, $3, strength, recall_count, last_used
                FROM memory_event WHERE event_id=$1
                RETURNING {_EVENT_COLS}
                """,
                current.event_id,
                actor,
                reason or "invalidated",
            )
            event = Event.from_row(row)
            await self._supersede(current.event_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            await self.conn.execute(
                "UPDATE memory_fact SET status='invalidated' WHERE fact_id=$1", fact_id
            )
            return event

    async def archive_if_head(
        self,
        fact_id: UUID,
        expected_event_id: UUID,
        *,
        actor: str,
        reason: str,
        expected_last_used: datetime | None = None,
    ) -> Event | None:
        """Append an archival event only if ``expected_event_id`` is still HEAD."""
        async with self.conn.transaction():
            await self._lock_scope()
            fact = await self._locked_fact(fact_id)
            if fact is None or fact["current_event_id"] != expected_event_id:
                return None
            if expected_last_used is not None:
                last_used = await self.conn.fetchval(
                    "SELECT last_used FROM memory_event WHERE event_id=$1", expected_event_id
                )
                if last_used != expected_last_used:
                    return None  # a recall after selection invalidates the decay decision
            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json, embedding,
                     provenance, actor, confidence, trust_level, source_span,
                     session_id, expires_at, valid_from, valid_to, parent_event_id, importance,
                       write_score,
                     tier, reason, strength, recall_count, last_used)
                SELECT fact_id, 'UPDATE', object_text, object_number, object_json, embedding,
                       provenance, $2, confidence, trust_level, source_span,
                       session_id, expires_at, valid_from, valid_to, event_id, importance,
                       write_score,
                       'ephemeral', $3, strength, recall_count, last_used
                FROM memory_event WHERE event_id=$1
                RETURNING {_EVENT_COLS}
                """,
                expected_event_id,
                actor,
                reason,
            )
            event = Event.from_row(row)
            await self._supersede(expected_event_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            return event

    async def log(self, *, fact_id: UUID | None = None, limit: int = 50) -> list[Event]:
        """Event history for a fact (newest first), or the recent global timeline."""
        if fact_id is not None:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id=$1 "
                f"AND fact_id IN (SELECT fact_id FROM memory_fact WHERE namespace=$2 "
                f"AND user_id=$3 AND agent_id=$4) "
                f"ORDER BY seq DESC LIMIT $5",
                fact_id,
                self.namespace,
                self.user_id,
                self.agent_id,
                limit,
            )
        else:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id IN "
                f"(SELECT fact_id FROM memory_fact WHERE namespace=$1 AND user_id=$2 "
                f"AND agent_id=$3) "
                f"ORDER BY seq DESC LIMIT $4",
                self.namespace,
                self.user_id,
                self.agent_id,
                limit,
            )
        return [Event.from_row(r) for r in rows]

    async def commit(self, label: str | None = None) -> Commit:
        """Capture the current high-water seq as a named, chained commit."""
        async with self.conn.transaction():
            await self._lock_scope()
            at_seq = await self.conn.fetchval(
                """SELECT COALESCE(max(e.seq), 0) FROM memory_event e
                   JOIN memory_fact f USING (fact_id)
                   WHERE f.namespace=$1 AND f.user_id=$2 AND f.agent_id=$3""",
                self.namespace,
                self.user_id,
                self.agent_id,
            )
            parent = await self.conn.fetchval(
                "SELECT commit_id FROM memory_commit "
                "WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 "
                "ORDER BY at_seq DESC, created_at DESC LIMIT 1",
                self.namespace,
                self.user_id,
                self.agent_id,
            )
            row = await self.conn.fetchrow(
                """
                INSERT INTO memory_commit
                    (namespace, user_id, agent_id, parent_commit_id, label, at_seq, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING commit_id, namespace, user_id, agent_id, parent_commit_id, label, at_seq,
                          created_by, created_at
                """,
                self.namespace,
                self.user_id,
                self.agent_id,
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

        seq_a = await self.conn.fetchval(
            "SELECT at_seq FROM memory_commit WHERE commit_id=$1 AND namespace=$2 "
            "AND user_id=$3 AND agent_id=$4",
            ca,
            self.namespace,
            self.user_id,
            self.agent_id,
        )
        seq_b = await self.conn.fetchval(
            "SELECT at_seq FROM memory_commit WHERE commit_id=$1 AND namespace=$2 "
            "AND user_id=$3 AND agent_id=$4",
            cb,
            self.namespace,
            self.user_id,
            self.agent_id,
        )
        if seq_a is None or seq_b is None:
            raise ValueError("unknown commit id")

        rows_a = await self.conn.fetch(
            "SELECT * FROM fact_snapshot_as_of($1, $2, $3, $4)",
            self.namespace,
            self.user_id,
            self.agent_id,
            seq_a,
        )
        rows_b = await self.conn.fetch(
            "SELECT * FROM fact_snapshot_as_of($1, $2, $3, $4)",
            self.namespace,
            self.user_id,
            self.agent_id,
            seq_b,
        )
        at_a = await self.conn.fetchval(
            "SELECT created_at FROM memory_commit WHERE commit_id=$1", ca
        )
        at_b = await self.conn.fetchval(
            "SELECT created_at FROM memory_commit WHERE commit_id=$1", cb
        )

        def visible(row: Any, at: datetime) -> bool:
            return (
                row["op"] in {"ADD", "UPDATE", "REVERT"}
                and row["tier"] != "ephemeral"
                and row["valid_from"] <= at
                and (row["valid_to"] is None or row["valid_to"] > at)
                and (row["expires_at"] is None or row["expires_at"] > at)
            )

        a = {r["fact_id"]: r for r in rows_a if visible(r, at_a)}
        b = {r["fact_id"]: r for r in rows_b if visible(r, at_b)}

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


def _source_cache_ids(facts: list[Fact]) -> set[str]:
    ids: set[str] = set()
    for fact in facts:
        span = fact.source_span
        if isinstance(span, str):
            span = json.loads(span)
        if isinstance(span, dict):
            ids.update(span.get("cache_ids", []))
    return ids
