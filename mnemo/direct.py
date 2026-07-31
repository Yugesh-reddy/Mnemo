"""Guarded direct memory: exact values, expected revisions and durable receipts.

Scope comes from the bound store. Model calls happen before scope/row locks;
event, HEAD and receipt writes then succeed or roll back together. Legacy store
operations retain their existing semantics.

Every write names its source (who it came from), which sets provenance and trust.
A retraction hides a memory from reads by appending an INVALIDATE event; update and
revert bring it back, so nothing a retraction does is final.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from mnemo.core import _EVENT_COLS, MnemoStore, canonicalize, derive_trust
from mnemo.embedder import model_id, vector_blob
from mnemo.errors import ErrorCode, MnemoError
from mnemo.models import (
    CurrentValue,
    Event,
    HistoricalValue,
    HistoryEntry,
    HistoryPage,
    MutationResult,
    RetractedMemory,
    SearchHit,
    SimilarMemory,
)
from mnemo.sqlite import Record

SOURCES = {
    "agent": "agent_inference",  # the agent concluded it (low trust)
    "tool": "tool_output",  # command output, a file or a web page (medium)
    "user": "direct_user_statement",  # the user said it (high)
}
DEFAULT_SOURCE = "agent"
REVIEW_SOURCE = "review"  # the web UI's manual correction, never an agent's


# Event columns qualified for a join with memory_fact.
_HEAD_COLS = ", ".join("e." + column.strip() for column in _EVENT_COLS.split(","))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class DirectMemory:
    MAX_VALUE_BYTES = 8192
    PREVIEW_CHARS = 256

    def __init__(self, store: MnemoStore) -> None:
        self.store = store

    @property
    def _scope(self) -> tuple[str, str, str]:
        return self.store.namespace, self.store.user_id, self.store.agent_id

    @staticmethod
    def _uuid(value: UUID, name: str) -> None:
        if not isinstance(value, UUID):
            raise MnemoError(ErrorCode.INVALID_INPUT, f"{name} must be a UUID")

    @classmethod
    def _text(cls, value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise MnemoError(ErrorCode.INVALID_INPUT, f"{name} must be nonblank text without NUL")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise MnemoError(ErrorCode.INVALID_INPUT, f"{name} must be valid UTF-8") from exc
        if size > cls.MAX_VALUE_BYTES:
            raise MnemoError(
                ErrorCode.INVALID_INPUT, f"{name} exceeds {cls.MAX_VALUE_BYTES} UTF-8 bytes"
            )

    @staticmethod
    def _payload(actor: str | None, **values: Any) -> dict[str, Any]:
        if actor is not None and (not isinstance(actor, str) or "\x00" in actor):
            raise MnemoError(ErrorCode.INVALID_INPUT, "actor must be text")
        try:
            # Snapshot mutable caller metadata and reject non-JSON inputs before embedding.
            encoded = json.dumps({**values, "actor": actor}, sort_keys=True, allow_nan=False)
            return json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise MnemoError(
                ErrorCode.INVALID_INPUT, "Mutation metadata must be JSON-compatible"
            ) from exc

    @staticmethod
    def _provenance(source: str) -> str:
        if source not in SOURCES:
            raise MnemoError(ErrorCode.INVALID_INPUT, f"source must be one of {', '.join(SOURCES)}")
        return SOURCES[source]

    @staticmethod
    def _sourced(source: str) -> dict[str, str]:
        """The source as receipt payload. The default is omitted, so a retry of a
        request recorded before sources existed still matches its receipt."""
        return {} if source == DEFAULT_SOURCE else {"source": source}

    @staticmethod
    def _retracted(status: str, head: Event | None) -> bool:
        """A durable memory hidden by retract (or legacy invalidate)."""
        return bool(
            status == "invalidated"
            and head is not None
            and head.op == "INVALIDATE"
            and head.tier == "durable"
            and head.session_id is None
            and head.expires_at is None
        )

    @staticmethod
    def _archived(status: str, head: Event | None) -> bool:
        """A memory the removed decay job hid by copying HEAD into an ephemeral
        revision (stores upgraded from the extraction pipeline still hold them)."""
        return bool(
            status == "active"
            and head is not None
            and head.op in {"ADD", "UPDATE", "REVERT"}
            and head.tier == "ephemeral"
            and head.session_id is None
            and head.expires_at is None
            and head.valid_to is None
        )

    @classmethod
    def _withdrawn(cls, status: str, head: Event | None) -> bool:
        """Hidden from search but restorable: retracted or archived."""
        return cls._retracted(status, head) or cls._archived(status, head)

    @staticmethod
    def _eligible(event: Event | None) -> bool:
        return bool(
            event is not None
            and event.op in {"ADD", "UPDATE", "REVERT"}
            and event.tier == "durable"
            and event.session_id is None
            and event.valid_to is None
            and event.expires_at is None
        )

    async def _fact(self, fact_id: UUID) -> Record:
        row = await self.store.conn.fetchrow(
            "SELECT fact_id, subject, predicate, current_event_id, status FROM memory_fact "
            "WHERE fact_id=?1 AND namespace=?2 AND user_id=?3 AND agent_id=?4",
            fact_id,
            *self._scope,
        )
        if row is None:
            raise MnemoError(ErrorCode.NOT_FOUND, "Memory not found")
        return row

    async def _checked_head(self, fact_id: UUID, expected_event_id: UUID) -> tuple[Event, bool]:
        """The locked HEAD if it is still ``expected_event_id``, and whether it is
        withdrawn (retracted or archived), so update and revert restore it."""
        fact = await self.store._locked_fact(fact_id)
        if fact is None:
            raise MnemoError(ErrorCode.NOT_FOUND, "Memory not found")
        head = await self.store._get_event(fact["current_event_id"])
        retracted = self._withdrawn(fact["status"], head)
        if not retracted and (fact["status"] != "active" or not self._eligible(head)):
            raise MnemoError(
                ErrorCode.UNSUPPORTED_STATE, "Only active, durable memories can be changed"
            )
        assert head is not None
        if head.event_id != expected_event_id:
            raise MnemoError(
                ErrorCode.REVISION_CONFLICT,
                "Memory changed; read its current revision and reconsider",
                current_event_id=str(head.event_id),
            )
        return head, retracted

    async def _reactivate(self, fact_id: UUID) -> None:
        await self.store.conn.execute(
            "UPDATE memory_fact SET status='active' WHERE fact_id=?1", fact_id
        )

    async def _existing_key(self, fact_key: str) -> None:
        row = await self.store.conn.fetchrow(
            "SELECT fact_id, current_event_id, status FROM memory_fact "
            "WHERE namespace=?1 AND user_id=?2 AND agent_id=?3 AND fact_key=?4 "
            "AND identity_mode='attribute'",
            *self._scope,
            fact_key,
        )
        if row is not None:
            head = await self.store._get_event(row["current_event_id"])
            retracted = self._withdrawn(row["status"], head)
            state = "archived" if self._archived(row["status"], head) else "retracted"
            raise MnemoError(
                ErrorCode.ALREADY_EXISTS,
                "Memory already exists; use its ID to update it"
                + (f" (it is {state}; update or revert restores it)" if retracted else ""),
                fact_id=str(row["fact_id"]),
                current_event_id=str(row["current_event_id"]) if row["current_event_id"] else None,
                fact_key=fact_key,
                **({"retracted": True} if retracted else {}),
            )

    async def _mutate(
        self,
        operation: str,
        request_id: UUID,
        payload: dict[str, Any],
        *,
        actor: str | None,
        apply: Callable[[], Awaitable[MutationResult]],
    ) -> MutationResult:
        async with self.store.conn.transaction():
            replayed = await self._replay(operation, request_id, payload)
            if replayed is not None:
                return replayed
            result = await apply()
            await self._insert_receipt(request_id, operation, payload, actor, result)
            return result

    async def _replay(
        self, operation: str, request_id: UUID, payload: dict[str, Any]
    ) -> MutationResult | None:
        """The saved result of an identical earlier request, or None if there is none.
        Called before embedding too, so a retry replays without the model."""
        receipt = await self.store.conn.fetchrow(
            "SELECT operation, request_payload, result FROM memory_mutation_receipt "
            "WHERE namespace=?1 AND user_id=?2 AND agent_id=?3 AND request_id=?4",
            *self._scope,
            request_id,
        )
        if receipt is None:
            return None
        # Compared as canonical JSON text, so formatting (a receipt imported from
        # Postgres) doesn't matter but 1 and true still differ.
        matches = _canonical(json.loads(receipt["request_payload"])) == _canonical(payload)
        if receipt["operation"] != operation or not matches:
            raise MnemoError(
                ErrorCode.REQUEST_ID_REUSED,
                "request_id was used for a different mutation",
                request_id=str(request_id),
            )
        saved = receipt["result"]
        if isinstance(saved, str):
            saved = json.loads(saved)
        return MutationResult.model_validate(saved).model_copy(update={"replayed": True})

    async def _insert_receipt(
        self,
        request_id: UUID,
        operation: str,
        payload: dict[str, Any],
        actor: str | None,
        result: MutationResult,
    ) -> None:
        await self.store.conn.execute(
            """INSERT INTO memory_mutation_receipt
               (namespace, user_id, agent_id, request_id, operation, request_payload, status,
                fact_id, event_id, previous_event_id, restored_from_event_id, actor, result,
                receipt_id)
               VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)""",
            *self._scope,
            request_id,
            operation,
            json.dumps(payload, sort_keys=True),
            result.status,
            result.fact_id,
            result.event_id,
            result.previous_event_id,
            result.restored_from_event_id,
            actor,
            result.model_dump_json(),
            uuid4(),
        )

    async def create(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        request_id: UUID,
        actor: str | None = None,
        source_span: Any | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> MutationResult:
        self._uuid(request_id, "request_id")
        for name, text in (("subject", subject), ("predicate", predicate), ("value", value)):
            self._text(text, name)
        provenance = self._provenance(source)
        fact_key = canonicalize(subject, predicate)
        payload = self._payload(
            actor,
            subject=subject,
            predicate=predicate,
            value=value,
            source_span=source_span,
            **self._sourced(source),
        )
        if (replayed := await self._replay("create", request_id, payload)) is not None:
            return replayed
        embedding = await self.store._embed(f"{subject} {predicate} {value}")

        async def apply() -> MutationResult:
            await self._existing_key(fact_key)
            fact_id = await self.store._insert_fact(subject, predicate, fact_key, "triple", None)
            if fact_id is None:
                await self._existing_key(fact_key)
                raise MnemoError(ErrorCode.ALREADY_EXISTS, "Memory already exists")
            event = await self.store._insert_event(
                fact_id,
                "ADD",
                value,
                parent_event_id=None,
                provenance=provenance,
                trust_level=derive_trust(provenance, 1.0),
                confidence=1.0,
                actor=actor,
                source_span=payload["source_span"],
                valid_from=None,
                embedding=embedding,
                tier="durable",
                importance=5,
                write_score=1.0,
            )
            await self.store._set_head(fact_id, event.event_id)
            return MutationResult(
                status="applied",
                fact_id=fact_id,
                event_id=event.event_id,
                value=value,
                request_id=request_id,
                similar=await self._similar(fact_id, embedding),
            )

        return await self._mutate("create", request_id, payload, actor=actor, apply=apply)

    async def _similar(self, fact_id: UUID, embedding: list[float]) -> list[SimilarMemory]:
        """Current durable memories in this scope whose vector (from the configured
        model) is within the duplicate floor of a new memory's, closest first."""
        floor = self.store.settings.duplicate_floor
        if floor is None:
            return []
        rows = await self.store.conn.fetch(
            """
            SELECT * FROM (
              SELECT f.fact_id, f.subject, f.predicate, e.object_text, f.current_event_id,
                     1 - vec_distance_cosine(m.vector, ?5) AS similarity
              FROM memory_fact f
              JOIN memory_event e ON e.event_id = f.current_event_id
              JOIN memory_embedding m ON m.seq = e.seq AND m.model = ?6 AND m.dim = ?7
              WHERE f.namespace = ?1 AND f.user_id = ?2 AND f.agent_id = ?3
                AND f.fact_id <> ?4 AND f.status = 'active' AND e.tier = 'durable'
                AND e.op IN ('ADD', 'UPDATE', 'REVERT')
            ) WHERE similarity >= ?8
            ORDER BY similarity DESC LIMIT 3
            """,
            *self._scope,
            fact_id,
            vector_blob(embedding),
            model_id(self.store.embedder),
            len(embedding),
            floor,
        )
        return [
            SimilarMemory(
                fact_id=row["fact_id"],
                subject=row["subject"],
                predicate=row["predicate"],
                value=row["object_text"] or "",
                current_event_id=row["current_event_id"],
                similarity=round(row["similarity"], 3),
            )
            for row in rows
        ]

    async def update(
        self,
        fact_id: UUID,
        value: str,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None = None,
        source: str = DEFAULT_SOURCE,
    ) -> MutationResult:
        provenance = self._provenance(source)
        return await self._update(
            fact_id,
            value,
            expected_event_id=expected_event_id,
            request_id=request_id,
            actor=actor,
            source=source,
            provenance=provenance,
        )

    async def correct(
        self,
        fact_id: UUID,
        value: str,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str = "ui",
    ) -> MutationResult:
        """A person's correction from the review UI: a guarded update recorded as
        human review (high trust). Agents cannot reach it; their sources stop at 'user'."""
        return await self._update(
            fact_id,
            value,
            expected_event_id=expected_event_id,
            request_id=request_id,
            actor=actor,
            source=REVIEW_SOURCE,
            provenance="human_review",
        )

    async def _update(
        self,
        fact_id: UUID,
        value: str,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None,
        source: str,
        provenance: str,
    ) -> MutationResult:
        for name, identifier in (
            ("fact_id", fact_id),
            ("expected_event_id", expected_event_id),
            ("request_id", request_id),
        ):
            self._uuid(identifier, name)
        self._text(value, "value")
        payload = self._payload(
            actor,
            fact_id=str(fact_id),
            value=value,
            expected_event_id=str(expected_event_id),
            **self._sourced(source),
        )
        if (replayed := await self._replay("update", request_id, payload)) is not None:
            return replayed
        identity = await self._fact(fact_id)
        embedding = await self.store._embed(
            f"{identity['subject']} {identity['predicate']} {value}"
        )

        async def apply() -> MutationResult:
            head, retracted = await self._checked_head(fact_id, expected_event_id)
            if head.object_text == value and not retracted:
                return MutationResult(
                    status="no_change",
                    fact_id=fact_id,
                    event_id=head.event_id,
                    value=value,
                    request_id=request_id,
                )
            if retracted:
                await self._reactivate(fact_id)
            event = await self.store._emit_update(
                fact_id,
                head.event_id,
                value,
                provenance=provenance,
                trust_level=derive_trust(provenance, 1.0),
                confidence=1.0,
                actor=actor,
                source_span=None,
                valid_from=None,
                embedding=embedding,
                tier="durable",
                importance=5,
                write_score=1.0,
            )
            return MutationResult(
                status="applied",
                fact_id=fact_id,
                event_id=event.event_id,
                previous_event_id=head.event_id,
                value=value,
                request_id=request_id,
            )

        return await self._mutate("update", request_id, payload, actor=actor, apply=apply)

    async def revert(
        self,
        fact_id: UUID,
        to_event_id: UUID,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None = None,
    ) -> MutationResult:
        if to_event_id is None:
            raise MnemoError(
                ErrorCode.UNSUPPORTED_OPERATION, "Undoing a memory's creation is unsupported"
            )
        for name, identifier in (
            ("fact_id", fact_id),
            ("to_event_id", to_event_id),
            ("expected_event_id", expected_event_id),
            ("request_id", request_id),
        ):
            self._uuid(identifier, name)
        payload = self._payload(
            actor,
            fact_id=str(fact_id),
            to_event_id=str(to_event_id),
            expected_event_id=str(expected_event_id),
        )

        async def apply() -> MutationResult:
            head, retracted = await self._checked_head(fact_id, expected_event_id)
            row = await self.store.conn.fetchrow(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE event_id=?1 AND fact_id=?2",
                to_event_id,
                fact_id,
            )
            if row is None:
                raise MnemoError(
                    ErrorCode.INVALID_RESTORE_TARGET, "Restore target not found for this memory"
                )
            target = Event.from_row(row)
            if not self._eligible(target):
                raise MnemoError(
                    ErrorCode.UNSUPPORTED_STATE, "Restore target is not an unexpired durable value"
                )
            if target.event_id == head.event_id:
                return MutationResult(
                    status="no_change",
                    fact_id=fact_id,
                    event_id=head.event_id,
                    restored_from_event_id=to_event_id,
                    value=str(head.value),
                    request_id=request_id,
                )
            event = await self.store._copy_as_revert(
                fact_id,
                to_event_id,
                provenance=None,
                trust_level=None,
                actor=actor,
                reason=f"revert to {str(to_event_id)[:8]} requested by {actor or 'agent'}",
            )
            await self.store._supersede(head.event_id, event.event_id)
            await self.store._set_head(fact_id, event.event_id)
            if retracted:
                await self._reactivate(fact_id)
            return MutationResult(
                status="applied",
                fact_id=fact_id,
                event_id=event.event_id,
                previous_event_id=head.event_id,
                restored_from_event_id=to_event_id,
                value=str(event.value),
                request_id=request_id,
            )

        return await self._mutate("revert", request_id, payload, actor=actor, apply=apply)

    async def retract(
        self,
        fact_id: UUID,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None = None,
        reason: str | None = None,
    ) -> MutationResult:
        """Hide a memory from reads, keeping its history. Revert or update restores it."""
        for name, identifier in (
            ("fact_id", fact_id),
            ("expected_event_id", expected_event_id),
            ("request_id", request_id),
        ):
            self._uuid(identifier, name)
        if reason is not None:
            self._text(reason, "reason")
        payload = self._payload(
            actor, fact_id=str(fact_id), expected_event_id=str(expected_event_id), reason=reason
        )

        async def apply() -> MutationResult:
            head, retracted = await self._checked_head(fact_id, expected_event_id)
            if retracted:
                return MutationResult(
                    status="no_change",
                    fact_id=fact_id,
                    event_id=head.event_id,
                    value=str(head.value),
                    request_id=request_id,
                )
            event = await self.store.invalidate(
                fact_id, actor=actor, reason=f"retracted: {reason}" if reason else "retracted"
            )
            return MutationResult(
                status="applied",
                fact_id=fact_id,
                event_id=event.event_id,
                previous_event_id=head.event_id,
                value=str(event.value),
                request_id=request_id,
            )

        return await self._mutate("retract", request_id, payload, actor=actor, apply=apply)

    async def get(
        self, fact_id: UUID, event_id: UUID | None = None
    ) -> CurrentValue | HistoricalValue:
        self._uuid(fact_id, "fact_id")
        if event_id is None:
            fact = await self.store.get(fact_id)
            if fact is None:
                return await self._retracted_value(fact_id)
            return CurrentValue(
                fact_id=fact.fact_id,
                subject=fact.subject,
                predicate=fact.predicate,
                value=str(fact.value),
                current_event_id=fact.event_id,
                provenance=fact.provenance,
                trust_level=fact.trust_level,
                recorded_at=fact.recorded_at,
            )
        self._uuid(event_id, "event_id")
        async with self.store.conn.transaction(readonly=not self.store.conn.is_in_transaction()):
            fact = await self._fact(fact_id)
            row = await self.store.conn.fetchrow(
                f"SELECT {_EVENT_COLS} FROM memory_event WHERE event_id=?1 AND fact_id=?2",
                event_id,
                fact_id,
            )
            if row is None:
                raise MnemoError(ErrorCode.NOT_FOUND, "Memory not found")
            event = Event.from_row(row)
            head = await self.store._get_event(fact["current_event_id"])
            return HistoricalValue(
                fact_id=fact_id,
                event_id=event.event_id,
                op=event.op,
                value=str(event.value),
                provenance=event.provenance,
                trust_level=event.trust_level,
                actor=event.actor,
                recorded_at=event.recorded_at,
                current_event_id=fact["current_event_id"],
                restorable=(
                    (fact["status"] == "active" and self._eligible(head))
                    or self._withdrawn(fact["status"], head)
                )
                and self._eligible(event),
            )

    async def _retracted_value(self, fact_id: UUID) -> CurrentValue:
        fact = await self._fact(fact_id)
        head = await self.store._get_event(fact["current_event_id"])
        if not self._withdrawn(fact["status"], head):
            raise MnemoError(ErrorCode.NOT_FOUND, "Memory not found")
        assert head is not None
        return CurrentValue(
            fact_id=fact_id,
            subject=fact["subject"],
            predicate=fact["predicate"],
            value=str(head.value),
            current_event_id=head.event_id,
            provenance=head.provenance,
            trust_level=head.trust_level,
            recorded_at=head.recorded_at,
            status="archived" if self._archived(fact["status"], head) else "retracted",
        )

    @staticmethod
    def _decode_cursor(cursor: str, fact_id: UUID) -> tuple[int, int]:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError
            record = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(record, dict) or set(record) != {"f", "max", "before"}:
                raise ValueError
            maximum, before = record["max"], record["before"]
            if (
                record["f"] != str(fact_id)
                or type(maximum) is not int
                or type(before) is not int
                or not 0 <= maximum < 2**63
                or not 0 < before < 2**63
                or before > maximum + 1
            ):
                raise ValueError
            return maximum, before
        except (ValueError, TypeError, binascii.Error) as exc:
            raise MnemoError(ErrorCode.INVALID_INPUT, "Invalid history cursor") from exc

    async def history(
        self, fact_id: UUID, *, cursor: str | None = None, limit: int = 20
    ) -> HistoryPage:
        self._uuid(fact_id, "fact_id")
        if type(limit) is not int:
            raise MnemoError(ErrorCode.INVALID_INPUT, "limit must be an integer")
        limit = max(1, min(limit, 100))
        bounds = self._decode_cursor(cursor, fact_id) if cursor is not None else None
        async with self.store.conn.transaction(readonly=not self.store.conn.is_in_transaction()):
            # HEAD and the initial high-water mark share one statement snapshot,
            # including when the caller owns an outer transaction's isolation.
            fact = await self.store.conn.fetchrow(
                "SELECT fact_id, current_event_id, "
                "(SELECT COALESCE(max(seq),0) FROM memory_event e "
                "WHERE e.fact_id=f.fact_id) AS max_seq "
                "FROM memory_fact f WHERE fact_id=?1 "
                "AND namespace=?2 AND user_id=?3 AND agent_id=?4",
                fact_id,
                *self._scope,
            )
            if fact is None:
                raise MnemoError(ErrorCode.NOT_FOUND, "Memory not found")
            maximum, before = bounds or (fact["max_seq"], fact["max_seq"] + 1)
            rows = await self.store.conn.fetch(
                f"SELECT {_EVENT_COLS} FROM memory_event "
                "WHERE fact_id=?1 AND seq <= ?2 AND seq < ?3 ORDER BY seq DESC LIMIT ?4",
                fact_id,
                maximum,
                before,
                limit + 1,
            )
            entries = []
            for row in rows[:limit]:
                event = Event.from_row(row)
                value = str(event.value)
                entries.append(
                    HistoryEntry(
                        event_id=event.event_id,
                        seq=event.seq,
                        op=event.op,
                        value_preview=value[: self.PREVIEW_CHARS],
                        value_truncated=len(value) > self.PREVIEW_CHARS,
                        provenance=event.provenance,
                        trust_level=event.trust_level,
                        actor=event.actor,
                        recorded_at=event.recorded_at,
                        restored_from_event_id=(
                            event.parent_event_id if event.op == "REVERT" else None
                        ),
                    )
                )
            next_cursor = None
            if len(rows) > limit:
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        {"f": str(fact_id), "max": maximum, "before": entries[-1].seq}
                    ).encode()
                ).decode()
            return HistoryPage(
                fact_id=fact_id,
                current_event_id=fact["current_event_id"],
                entries=entries,
                next_cursor=next_cursor,
            )

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        self._text(query, "query")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MnemoError(ErrorCode.INVALID_INPUT, "limit must be an integer in 1..1000")
        hits = await self.store.search(query, k=limit, reinforce=False)
        return [
            SearchHit(
                fact_id=hit.fact_id,
                event_id=hit.event_id,
                subject=hit.subject,
                predicate=hit.predicate,
                value=str(hit.value),
                score=hit.score or 0.0,
                provenance=hit.provenance,
                trust_level=hit.trust_level,
                actor=hit.actor,
                recorded_at=hit.recorded_at,
            )
            for hit in hits
        ]

    async def retracted(self, *, limit: int = 200) -> list[RetractedMemory]:
        """Memories in this scope withdrawn by retract, most recent first, then any the
        removed decay job archived.

        So a person can see what agents withdrew (including any retraction an agent
        was tricked into) and restore it from its history.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MnemoError(ErrorCode.INVALID_INPUT, "limit must be an integer in 1..1000")
        rows = await self.store.conn.fetch(
            f"""
            SELECT f.subject, f.predicate, f.status, {_HEAD_COLS}
            FROM memory_fact f JOIN memory_event e ON e.event_id = f.current_event_id
            WHERE f.namespace=?1 AND f.user_id=?2 AND f.agent_id=?3
              AND ((f.status = 'invalidated' AND e.op = 'INVALIDATE')
                   OR (f.status = 'active' AND e.tier = 'ephemeral'))
            ORDER BY e.tier = 'ephemeral', e.seq DESC LIMIT ?4
            """,
            *self._scope,
            limit,
        )
        found = []
        for row in rows:
            head = Event.from_row(row)
            if not self._withdrawn(row["status"], head):
                continue
            reason = head.reason
            if reason in ("retracted", "invalidated"):
                reason = None
            elif reason and reason.startswith("retracted: "):
                reason = reason.removeprefix("retracted: ")
            found.append(
                RetractedMemory(
                    fact_id=head.fact_id,
                    subject=row["subject"],
                    predicate=row["predicate"],
                    value=str(head.value),
                    provenance=head.provenance,
                    trust_level=head.trust_level,
                    retracted_event_id=head.event_id,
                    retracted_by=head.actor,
                    retracted_at=head.recorded_at,
                    reason=reason,
                )
            )
        return found
