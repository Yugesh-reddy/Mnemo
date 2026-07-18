"""Core operations — the heart of Mnemo (spec §5).

Async ``MnemoStore`` bound to one connection to the store file. Each mutating op is
a transaction, and every write transaction holds the file's single write lock. The
append-only invariant is sacred: state changes are *new* events; we only ever flip
supersession flags and move the HEAD pointer, never rewrite a payload.

Identity is by ``fact_key`` (canonical subject|predicate) plus an identity mode.
Legacy calls use the ``attribute`` identity: one HEAD per key. Member/occurrence
identities carry a nonzero ``identity_ref`` so several facts can share one key.
Exact values and narrow identity aliases no-op; changed values append revisions
regardless of cosine. ``search`` is hybrid word + vector over HEAD: every memory in
the scope is scored exactly.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from mnemo.config import Settings, get_settings
from mnemo.embedder import model_id, vector_blob
from mnemo.models import Commit, Diff, DiffEntry, Event, Fact
from mnemo.sqlite import Connection, Record

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

IdentityMode = Literal["attribute", "member", "occurrence"]
ATTRIBUTE_REF = UUID(int=0)


def identity_key(identity_mode: str, identity_ref: UUID | None) -> tuple[str, UUID]:
    """Validate an identity request; attributes store the all-zero reference."""
    if identity_mode == "attribute":
        if identity_ref is not None:
            raise ValueError("attribute identities take no identity_ref")
        return identity_mode, ATTRIBUTE_REF
    if identity_mode in ("member", "occurrence"):
        if not isinstance(identity_ref, UUID) or identity_ref == ATTRIBUTE_REF:
            raise ValueError(f"{identity_mode} identities require a nonzero identity_ref")
        return identity_mode, identity_ref
    raise ValueError("identity_mode must be 'attribute', 'member' or 'occurrence'")


# Every column needed to hydrate an Event, in one place.
_EVENT_COLS = """
    event_id, seq, fact_id, op, object_text, object_number, object_json,
    provenance, actor, confidence, trust_level, source_span,
    session_id, valid_from, valid_to, expires_at, recorded_at,
    superseded_at, superseded_by, parent_event_id,
    importance, write_score, tier, reason, strength, recall_count, last_used
"""


# Columns of an event joined to its fact, shaped like a memory_current row.
_LEGACY_EXPIRY = (
    "COALESCE(e.expires_at, CASE WHEN e.tier = 'session' THEN "
    "strftime('%Y-%m-%dT%H:%M:%S', e.recorded_at, '+24 hours') || substr(e.recorded_at, 20) END)"
)
_SEARCH_COLS = f"""
    e.fact_id, f.namespace, f.user_id, f.agent_id,
    COALESCE(e.session_id, f.session_id) AS session_id, f.subject, f.predicate, f.fact_key,
    f.kind, e.seq, e.event_id, e.object_text, e.object_number, e.object_json, e.provenance,
    e.actor, e.confidence, e.trust_level, e.source_span, e.valid_from, e.valid_to,
    {_LEGACY_EXPIRY} AS expires_at, e.recorded_at, e.importance, e.write_score, e.tier,
    e.strength, e.recall_count, e.last_used, f.identity_mode, f.identity_ref
"""
_CURRENT_COLS = """
    fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
    kind, event_id, object_text, object_number, object_json,
    provenance, confidence, trust_level, valid_from, recorded_at,
    importance, write_score, tier, strength, recall_count,
    source_span, valid_to, expires_at, fact_key, identity_mode, identity_ref
"""

# Postgres's English stop words: a question made only of these matches no words.
STOP_WORDS = frozenset("""
    i me my myself we our ours ourselves you your yours yourself yourselves he him his
    himself she her hers herself it its itself they them their theirs themselves what
    which who whom this that these those am is are was were be been being have has had
    having do does did doing a an the and but if or because as until while of at by for
    with about against between into through during before after above below to from up
    down in out on off over under again further then once here there when where why how
    all any both each few more most other some such no nor not only own same so than too
    very s t can will just don should now
    """.split())


def fts_query(text: str) -> str | None:
    """Every non-stop word, ANDed, each quoted so FTS5 syntax in the text is literal.

    The porter tokenizer stems both sides, as Postgres plainto_tsquery did."""
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOP_WORDS]
    return " AND ".join(f'"{w}"' for w in dict.fromkeys(words)) or None


def like_pattern(text: str) -> str:
    """A substring pattern in which ``%`` and ``_`` are literal (escape ``\\``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
    """Async core. One instance wraps one connection (callers own its lifetime)."""

    def __init__(
        self,
        conn: Connection,
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

    @property
    def _scope(self) -> tuple[str, str, str]:
        return self.namespace, self.user_id, self.agent_id

    # ---- internal helpers -----------------------------------------------

    async def _get_event(self, event_id: UUID | None) -> Event | None:
        if event_id is None:
            return None
        row = await self.conn.fetchrow(
            f"SELECT {_EVENT_COLS} FROM memory_event WHERE event_id=?1", event_id
        )
        return Event.from_row(row) if row else None

    async def _locked_fact(self, fact_id: UUID) -> Record | None:
        """The scoped fact row. Callers hold the write lock (BEGIN IMMEDIATE)."""
        return await self.conn.fetchrow(
            """
            SELECT fact_id, current_event_id, status FROM memory_fact
            WHERE fact_id=?1 AND namespace=?2 AND user_id=?3 AND agent_id=?4
            """,
            fact_id,
            *self._scope,
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
                (event_id, fact_id, op, object_text, object_number, object_json,
                 provenance, actor, confidence, trust_level, source_span,
                 session_id, valid_from, expires_at, parent_event_id,
                 importance, write_score, tier, reason)
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11,
                    ?12, COALESCE(?13, now()), ?14, ?15, ?16, ?17, ?18, ?19)
            RETURNING {_EVENT_COLS}
            """,
            uuid4(),
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
            session_id,
            valid_from,
            expires_at,
            parent_event_id,
            importance,
            write_score,
            tier,
            reason,
        )
        event = Event.from_row(row)
        if embedding is not None:
            await self._store_vector(event.seq, embedding)
        return event

    async def _store_vector(self, seq: int, embedding: list[float]) -> None:
        await self.conn.execute(
            "INSERT INTO memory_embedding (seq, model, dim, vector) VALUES (?1, ?2, ?3, ?4)",
            seq,
            model_id(self.embedder),
            len(embedding),
            vector_blob(embedding),
        )

    async def _copy_vector(self, from_event_id: UUID, to_seq: int) -> None:
        """A copied revision keeps the vector of the text it copies."""
        await self.conn.execute(
            """
            INSERT INTO memory_embedding (seq, model, dim, vector)
            SELECT ?1, m.model, m.dim, m.vector
            FROM memory_embedding m JOIN memory_event e ON e.seq = m.seq
            WHERE e.event_id = ?2
            """,
            to_seq,
            from_event_id,
        )

    async def _embed(self, text: str) -> list[float]:
        """Embed off the event loop (the backend call may hit the network)."""
        return await asyncio.to_thread(self.embedder.embed, text)

    async def _set_head(self, fact_id: UUID, event_id: UUID) -> None:
        await self.conn.execute(
            "UPDATE memory_fact SET current_event_id=?1 WHERE fact_id=?2", event_id, fact_id
        )

    async def _supersede(self, old_event_id: UUID, new_event_id: UUID) -> None:
        await self.conn.execute(
            "UPDATE memory_event SET superseded_at=now(), superseded_by=?2 WHERE event_id=?1",
            old_event_id,
            new_event_id,
        )

    async def _fact_by_key(self, fact_key: str, mode: str, ref: UUID) -> Record | None:
        return await self.conn.fetchrow(
            """
            SELECT fact_id, current_event_id, status FROM memory_fact
            WHERE namespace=?1 AND user_id=?2 AND agent_id=?3 AND fact_key=?4
              AND identity_mode=?5 AND identity_ref=?6
            """,
            *self._scope,
            fact_key,
            mode,
            ref,
        )

    async def _fact_id_for_key(
        self,
        fact_key: str,
        identity_mode: str = "attribute",
        identity_ref: UUID | None = None,
    ) -> UUID | None:
        mode, ref = identity_key(identity_mode, identity_ref)
        fact = await self._fact_by_key(fact_key, mode, ref)
        return None if fact is None else fact["fact_id"]

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
        identity_mode: IdentityMode = "attribute",
        identity_ref: UUID | None = None,
    ) -> Event:
        """Insert a fact value, routing to ADD / UPDATE / no-op (spec §5).

        Exact values and narrow aliases under the same identity are idempotent while
        visible. Changed values append UPDATE even with identical embeddings;
        unrelated identities create ADD. The default ``attribute`` identity keeps one
        HEAD per subject/predicate; each member/occurrence ``identity_ref`` is its
        own fact under the same key.
        """
        mode, ref = identity_key(identity_mode, identity_ref)
        fact_key = canonicalize(subject, predicate)
        trust = derive_trust(provenance, confidence)
        if tier == "session" and session_id is None:
            raise ValueError("session tier requires session_id")
        if valid_from is not None and (
            not isinstance(valid_from, datetime) or valid_from.utcoffset() is None
        ):
            raise ValueError("valid_from must be a timezone-aware datetime")
        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=self.settings.session_ttl_seconds)
            if tier == "session"
            else None
        )
        if embedding is None:
            embedding = await self._embed(f"{subject} {predicate} {object}")
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
            fact = await self._fact_by_key(fact_key, mode, ref)
            if fact is not None:
                return await self._apply_to_existing(fact, object, **emit)
            fact_id = await self._insert_fact(
                subject, predicate, fact_key, kind, session_id, identity_mode=mode, identity_ref=ref
            )
            if fact_id is None:
                # The same key appeared since the lookup: act as the second arrival.
                fact = await self._fact_by_key(fact_key, mode, ref)
                assert fact is not None  # the constraint fired, so the row must exist
                return await self._apply_to_existing(fact, object, **emit)
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

    async def _apply_to_existing(self, fact: Record, object: Any, **emit: Any) -> Event:
        """Route a value to an existing fact: no-op on a visible duplicate, else UPDATE."""
        current = await self._get_event(fact["current_event_id"])
        if current is not None:
            now = self.conn.now()
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
            "UPDATE memory_fact SET status='active' WHERE fact_id=?1", fact["fact_id"]
        )
        return await self._emit_update(fact["fact_id"], fact["current_event_id"], object, **emit)

    async def _insert_fact(
        self,
        subject: str,
        predicate: str,
        fact_key: str,
        kind: str,
        session_id: str | None,
        *,
        identity_mode: str = "attribute",
        identity_ref: UUID = ATTRIBUTE_REF,
    ) -> UUID | None:
        """Insert a brand-new fact. Returns ``None`` if the scoped identity already exists.

        Writers hold the file's write lock, so this cannot race; the savepoint keeps
        the caller's transaction usable if it ever does.
        """
        try:
            async with self.conn.transaction():  # savepoint
                return await self.conn.fetchval(
                    """
                    INSERT INTO memory_fact
                        (fact_id, namespace, user_id, agent_id, session_id,
                         subject, predicate, fact_key, kind, identity_mode, identity_ref)
                    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
                    RETURNING fact_id
                    """,
                    uuid4(),
                    *self._scope,
                    session_id,
                    subject,
                    predicate,
                    fact_key,
                    kind,
                    identity_mode,
                    identity_ref,
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: memory_fact" not in str(exc):
                raise
            return None

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
        """Hybrid search over HEAD (memory_current): word matches, then vector
        neighbours at/above ``search_floor``. ``session_id`` admits that session's
        session-tier facts. Every memory in the scope is scored (spec §14)."""
        if not 1 <= k <= 1000:
            raise ValueError("k must be 1..1000")
        for name, value in (("as_of", as_of), ("valid_at", valid_at)):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError(f"{name} must be a timezone-aware datetime")
        if include_superseded and as_of is None:
            as_of = datetime.now(tz=UTC)
        embedding = await self._embed(query)
        recorded_at = as_of
        if valid_at is not None and recorded_at is None:
            recorded_at = datetime.now(tz=UTC)
        valid_at = valid_at or recorded_at

        async with self.conn.transaction(readonly=not self.conn.is_in_transaction()):
            rows = await self._search_rows(
                query, embedding, k, session_id, recorded_at, valid_at, include_superseded
            )
            results = [Fact.from_row(r, score=float(r["score"])) for r in rows]

        should_reinforce = self.settings.search_reinforce if reinforce is None else reinforce
        if should_reinforce and recorded_at is None:
            for fact in results:
                if fact.source == "semantic":
                    await self.reinforce(fact.fact_id)
        return results

    async def _search_rows(
        self,
        query: str,
        embedding: list[float],
        limit: int,
        session_id: str | None,
        recorded_at: datetime | None,
        valid_at: datetime | None,
        include_superseded: bool,
    ) -> list[Record]:
        if recorded_at is None:
            source = (
                "SELECT * FROM memory_current WHERE namespace=?1 AND user_id=?2 AND agent_id=?3"
            )
        elif include_superseded:
            source = f"""
                SELECT {_SEARCH_COLS}
                FROM memory_event e JOIN memory_fact f ON f.fact_id = e.fact_id
                WHERE f.namespace=?1 AND f.user_id=?2 AND f.agent_id=?3
                  AND e.recorded_at <= ?14 AND e.valid_from <= ?15
                  AND (e.valid_to IS NULL OR e.valid_to > ?15)
                  AND (e.expires_at IS NULL OR e.expires_at > ?14)
                  AND e.op <> 'DELETE' AND e.tier IN ('durable', 'session')
            """
        else:
            # The latest event per fact that was recorded by then (fact_snapshot_at).
            source = f"""
                SELECT * FROM (
                  SELECT {_SEARCH_COLS}, e.op, ROW_NUMBER() OVER (
                    PARTITION BY e.fact_id ORDER BY e.recorded_at DESC, e.seq DESC) AS latest
                  FROM memory_event e JOIN memory_fact f ON f.fact_id = e.fact_id
                  WHERE f.namespace=?1 AND f.user_id=?2 AND f.agent_id=?3
                    AND e.recorded_at <= ?14
                ) s
                WHERE latest = 1 AND op <> 'DELETE' AND tier IN ('durable', 'session')
                  AND valid_from <= ?15 AND (valid_to IS NULL OR valid_to > ?15)
                  AND (expires_at IS NULL OR expires_at > ?14)
            """
        words = fts_query(query)
        fts_hit = (
            "eligible.seq IN (SELECT rowid FROM memory_search_text "
            "WHERE memory_search_text MATCH ?4)"
            if words
            else "0"
        )
        sql = f"""
            WITH source AS ({source}),
            eligible AS (
              SELECT * FROM source
              WHERE (tier='durable' OR (tier='session' AND session_id=?13))
                AND (?15 IS NULL OR valid_from <= ?15)
            ),
            scored AS (
              SELECT eligible.*,
                CASE WHEN m.vector IS NULL THEN 0
                  ELSE 1 - vec_distance_cosine(m.vector, ?5) END AS rel_vec,
                {fts_hit} AS fts_hit,
                (subject LIKE ?6 ESCAPE '\\' OR predicate LIKE ?6 ESCAPE '\\'
                 OR object_text LIKE ?6 ESCAPE '\\') AS kw_hit,
                pow(?9, (julianday(COALESCE(?14, now()))
                  - julianday(min(last_used, COALESCE(?14, now())))) * 24.0) AS recency
              FROM eligible
              LEFT JOIN memory_embedding m
                ON m.seq = eligible.seq AND m.model = ?16 AND m.dim = ?17
            )
            SELECT *, (?10 * max(rel_vec, CASE WHEN fts_hit THEN 0.85 ELSE 0 END,
                                 CASE WHEN kw_hit THEN 0.80 ELSE 0 END)
                       + ?11 * recency
                       + ?12 * (COALESCE(importance, 5) / 10.0)) AS score
            FROM scored WHERE fts_hit OR kw_hit OR rel_vec >= ?7
            ORDER BY score DESC, fact_id DESC LIMIT ?8
        """
        return await self.conn.fetch(
            sql,
            *self._scope,
            words,
            vector_blob(embedding),
            like_pattern(query),
            self.settings.search_floor,
            limit,
            self.settings.recency_gamma,
            self.settings.search_w_rel,
            self.settings.search_w_rec,
            self.settings.search_w_imp,
            session_id,
            recorded_at,
            valid_at,
            model_id(self.embedder),
            len(embedding),
        )

    async def list_current(self, *, limit: int = 200) -> list[Fact]:
        """All HEAD facts (most recently changed first) — for the UI list view."""
        rows = await self.conn.fetch(
            f"""
            SELECT {_CURRENT_COLS} FROM memory_current
            WHERE namespace=?1 AND user_id=?2 AND agent_id=?3
            ORDER BY recorded_at DESC, seq DESC
            LIMIT ?4
            """,
            *self._scope,
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
            WHERE namespace=?1 AND user_id=?2 AND agent_id=?3
            ORDER BY at_seq DESC, created_at DESC
            LIMIT ?4
            """,
            *self._scope,
            limit,
        )
        return [Commit.from_row(r) for r in rows]

    async def get(self, fact_id: UUID) -> Fact | None:
        """The HEAD fact for ``fact_id`` (current believed value), or None."""
        row = await self.conn.fetchrow(
            f"""
            SELECT {_CURRENT_COLS} FROM memory_current
            WHERE fact_id=?1 AND namespace=?2 AND user_id=?3 AND agent_id=?4
            """,
            fact_id,
            *self._scope,
        )
        return Fact.from_row(row) if row else None

    async def blame(
        self,
        *,
        fact_id: UUID | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        identity_mode: IdentityMode = "attribute",
        identity_ref: UUID | None = None,
    ) -> list[Event]:
        """Full ordered event history for a fact (git blame for memory).

        A subject/predicate lookup addresses the attribute identity unless a
        member/occurrence identity is given; fact_id lookups ignore identity.
        """
        fid = await self._resolve_fact_id(
            fact_id, subject, predicate, identity_mode=identity_mode, identity_ref=identity_ref
        )
        if fid is None:
            return []
        rows = await self.conn.fetch(
            f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id=?1 ORDER BY seq", fid
        )
        return [Event.from_row(r) for r in rows]

    async def _resolve_fact_id(
        self,
        fact_id: UUID | None,
        subject: str | None,
        predicate: str | None,
        *,
        identity_mode: str = "attribute",
        identity_ref: UUID | None = None,
    ) -> UUID | None:
        if fact_id is not None:
            fact = await self._locked_fact(fact_id)
            return None if fact is None else fact["fact_id"]
        if subject is not None and predicate is not None:
            return await self._fact_id_for_key(
                canonicalize(subject, predicate), identity_mode, identity_ref
            )
        raise ValueError("provide fact_id or both subject and predicate")

    async def revert(
        self, fact_id: UUID, to_event_id: UUID, *, actor: str = "human_review"
    ) -> Event:
        """Roll a fact back to a prior event's value via a new REVERT event."""
        async with self.conn.transaction():
            fact = await self._locked_fact(fact_id)
            if fact is None:
                raise ValueError(f"fact {fact_id} not found")
            target = await self.conn.fetchval(
                "SELECT 1 FROM memory_event WHERE event_id=?1 AND fact_id=?2", to_event_id, fact_id
            )
            if target is None:
                raise ValueError(f"event {to_event_id} does not belong to fact {fact_id}")

            event = await self._copy_as_revert(
                fact_id,
                to_event_id,
                provenance="human_review",
                trust_level="high",
                confidence=1.0,
                actor=actor,
                reason=f"revert to {str(to_event_id)[:8]}",
            )

            current_id = fact["current_event_id"]
            if current_id is not None and current_id != event.event_id:
                await self._supersede(current_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            # Revert is the undo for invalidate() too: reactivate the fact.
            await self.conn.execute(
                "UPDATE memory_fact SET status='active' WHERE fact_id=?1", fact_id
            )
            return event

    async def _copy_as_revert(
        self,
        fact_id: UUID,
        to_event_id: UUID,
        *,
        provenance: str | None,
        trust_level: str | None,
        actor: str | None,
        reason: str,
        confidence: float | None = None,
    ) -> Event:
        """Copy a scoped target into a new event inside the caller's transaction.

        None preserves source provenance, trust and confidence. Legacy human
        review explicitly overrides all three. HEAD movement belongs to the caller.
        """
        session_expiry = self.conn.now() + timedelta(seconds=self.settings.session_ttl_seconds)
        row = await self.conn.fetchrow(
            f"""
            INSERT INTO memory_event
                (event_id, fact_id, op, object_text, object_number, object_json,
                 provenance, actor, confidence, trust_level, source_span,
                 session_id, expires_at, valid_from, parent_event_id, importance, write_score,
                 tier, reason, strength, recall_count, last_used)
            SELECT ?9, fact_id, 'REVERT', object_text, object_number, object_json,
                   COALESCE(?3, provenance), ?4, COALESCE(?8, confidence),
                   COALESCE(?5, trust_level), source_span,
                   COALESCE(session_id, (SELECT session_id FROM memory_fact WHERE fact_id=?2)),
                   CASE WHEN tier='session' THEN ?6 END,
                   now(), event_id, importance, write_score, tier,
                   ?7, strength, recall_count, last_used
            FROM memory_event WHERE event_id=?1 AND fact_id=?2
            RETURNING {_EVENT_COLS}
            """,
            to_event_id,
            fact_id,
            provenance,
            actor,
            trust_level,
            session_expiry,
            reason,
            confidence,
            uuid4(),
        )
        if row is None:
            raise ValueError("restore target not found")
        event = Event.from_row(row)
        await self._copy_vector(to_event_id, event.seq)
        return event

    async def reinforce(self, fact_id: UUID) -> None:
        """Recall reinforcement (MemoryBank): S += 1, t -> 0 on the live event.

        strength/recall_count/last_used are the documented mutable decay
        bookkeeping — never payload."""
        async with self.conn.transaction():
            fact = await self._locked_fact(fact_id)
            if fact is None:
                return
            await self.conn.execute(
                """
                UPDATE memory_event SET strength = strength + 1,
                       recall_count = recall_count + 1, last_used = now()
                WHERE event_id=?1
                """,
                fact["current_event_id"],
            )

    async def invalidate(
        self, fact_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> Event:
        """Mark a fact no longer true in the world (bitemporal): append an
        INVALIDATE event with valid_to=now(). Reversible via revert()."""
        async with self.conn.transaction():
            fact = await self._locked_fact(fact_id)
            if fact is None or fact["current_event_id"] is None:
                raise ValueError(f"fact {fact_id} not found")
            current_id = fact["current_event_id"]
            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (event_id, fact_id, op, object_text, object_number, object_json,
                     provenance, actor, confidence, trust_level, source_span,
                     session_id, expires_at, valid_from, valid_to, parent_event_id, importance,
                     write_score, tier, reason, strength, recall_count, last_used)
                SELECT ?4, fact_id, 'INVALIDATE', object_text, object_number, object_json,
                       provenance, ?2, confidence, trust_level, source_span,
                       session_id, expires_at, valid_from, now(), event_id, importance,
                       write_score, tier, ?3, strength, recall_count, last_used
                FROM memory_event WHERE event_id=?1
                RETURNING {_EVENT_COLS}
                """,
                current_id,
                actor,
                reason or "invalidated",
                uuid4(),
            )
            event = Event.from_row(row)
            await self._copy_vector(current_id, event.seq)
            await self._supersede(current_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            await self.conn.execute(
                "UPDATE memory_fact SET status='invalidated' WHERE fact_id=?1", fact_id
            )
            return event

    async def log(self, *, fact_id: UUID | None = None, limit: int = 50) -> list[Event]:
        """Event history for a fact (newest first), or the recent global timeline."""
        scoped = (
            "fact_id IN (SELECT fact_id FROM memory_fact "
            "WHERE namespace=?1 AND user_id=?2 AND agent_id=?3)"
        )
        if fact_id is not None:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE fact_id=?5 AND {scoped} "
                "ORDER BY seq DESC LIMIT ?4",
                *self._scope,
                limit,
                fact_id,
            )
        else:
            rows = await self.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE {scoped} "
                "ORDER BY seq DESC LIMIT ?4",
                *self._scope,
                limit,
            )
        return [Event.from_row(r) for r in rows]

    async def commit(self, label: str | None = None) -> Commit:
        """Capture the current high-water seq as a named, chained commit."""
        async with self.conn.transaction():
            at_seq = await self.conn.fetchval(
                """SELECT COALESCE(max(e.seq), 0) FROM memory_event e
                   JOIN memory_fact f ON f.fact_id = e.fact_id
                   WHERE f.namespace=?1 AND f.user_id=?2 AND f.agent_id=?3""",
                *self._scope,
            )
            parent = await self.conn.fetchval(
                "SELECT commit_id FROM memory_commit "
                "WHERE namespace=?1 AND user_id=?2 AND agent_id=?3 "
                "ORDER BY at_seq DESC, created_at DESC LIMIT 1",
                *self._scope,
            )
            row = await self.conn.fetchrow(
                """
                INSERT INTO memory_commit
                    (commit_id, namespace, user_id, agent_id, parent_commit_id, label, at_seq,
                     created_by)
                VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                RETURNING commit_id, namespace, user_id, agent_id, parent_commit_id, label, at_seq,
                          created_by, created_at
                """,
                uuid4(),
                *self._scope,
                parent,
                label,
                at_seq,
                self.user_id,
            )
            return Commit.from_row(row)

    async def _snapshot_as_of(self, seq: int) -> list[Record]:
        """Each fact's latest event at or before ``seq`` (fact_snapshot_as_of)."""
        return await self.conn.fetch(
            """
            SELECT * FROM (
              SELECT e.fact_id, f.subject, f.predicate, f.fact_key, e.op,
                     e.object_text, e.object_number, e.object_json, e.tier,
                     COALESCE(e.session_id, f.session_id) AS session_id,
                     e.valid_from, e.valid_to,
                     COALESCE(e.expires_at, CASE WHEN e.tier = 'session' THEN
                       strftime('%Y-%m-%dT%H:%M:%S', e.recorded_at, '+24 hours')
                       || substr(e.recorded_at, 20) END) AS expires_at,
                     e.recorded_at,
                     ROW_NUMBER() OVER (PARTITION BY e.fact_id ORDER BY e.seq DESC) AS latest
              FROM memory_event e JOIN memory_fact f ON f.fact_id = e.fact_id
              WHERE f.namespace=?1 AND f.user_id=?2 AND f.agent_id=?3 AND e.seq <= ?4
            ) WHERE latest = 1
            """,
            *self._scope,
            seq,
        )

    async def diff(self, commit_a: Commit | UUID, commit_b: Commit | UUID) -> Diff:
        """Diff fact state between two commits, emitting added / removed / changed."""
        ca = commit_a.commit_id if isinstance(commit_a, Commit) else commit_a
        cb = commit_b.commit_id if isinstance(commit_b, Commit) else commit_b

        async def point(commit_id: UUID) -> Record | None:
            return await self.conn.fetchrow(
                "SELECT at_seq, created_at FROM memory_commit WHERE commit_id=?1 "
                "AND namespace=?2 AND user_id=?3 AND agent_id=?4",
                commit_id,
                *self._scope,
            )

        pa, pb = await point(ca), await point(cb)
        if pa is None or pb is None:
            raise ValueError("unknown commit id")
        seq_a, seq_b = pa["at_seq"], pb["at_seq"]
        rows_a = await self._snapshot_as_of(seq_a)
        rows_b = await self._snapshot_as_of(seq_b)

        def visible(row: Any, at: datetime) -> bool:
            return (
                row["op"] in {"ADD", "UPDATE", "REVERT"}
                and row["tier"] != "ephemeral"
                and row["valid_from"] <= at
                and (row["valid_to"] is None or row["valid_to"] > at)
                and (row["expires_at"] is None or row["expires_at"] > at)
            )

        a = {r["fact_id"]: r for r in rows_a if visible(r, pa["created_at"])}
        b = {r["fact_id"]: r for r in rows_b if visible(r, pb["created_at"])}

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
