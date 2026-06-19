"""Mnemo — Git for agent memory.

An append-only, inspectable, reversible memory layer for LLM agents:
``add`` / ``search`` / ``blame`` / ``revert`` / ``diff`` / ``log`` on plain Postgres.

``Mnemo`` is the sync public surface (thin wrappers over the async ``MnemoStore``
core, spec §9) — convenient for the CLI/demo. Each call opens a short-lived
connection; for high throughput, use ``MnemoStore`` against your own pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import UUID

import asyncpg

from mnemo.core import MnemoStore
from mnemo.db import register_vector
from mnemo.direct import DirectMemory
from mnemo.errors import ErrorCode, MnemoError
from mnemo.models import (
    Commit,
    CurrentValue,
    Diff,
    Event,
    Fact,
    HistoricalValue,
    HistoryPage,
    MutationResult,
    SearchHit,
)

__version__ = "0.0.1"
__all__ = [
    "Mnemo",
    "MnemoStore",
    "Event",
    "Fact",
    "Commit",
    "Diff",
    "MutationResult",
    "MnemoError",
    "ErrorCode",
]

T = TypeVar("T")


class Mnemo:
    """Synchronous SDK over the async core (one connection per call)."""

    def __init__(
        self,
        dsn: str,
        embedder: Any,
        *,
        namespace: str = "default",
        user_id: str = "default",
        agent_id: str = "default",
    ) -> None:
        self._dsn = dsn
        self._embedder = embedder
        self._namespace = namespace
        self._user_id = user_id
        self._agent_id = agent_id

    @property
    def direct(self) -> _DirectSDK:
        """Guarded mutations and scoped reads using this client's connection settings."""
        return _DirectSDK(self)

    def _run(self, fn: Callable[[MnemoStore], Awaitable[T]]) -> T:
        async def _wrapped() -> T:
            conn = await asyncpg.connect(self._dsn)
            await register_vector(conn)
            try:
                store = MnemoStore(
                    conn,
                    self._embedder,
                    namespace=self._namespace,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                )
                return await fn(store)
            finally:
                await conn.close()

        return asyncio.run(_wrapped())

    def add(self, subject: str, predicate: str, object: Any, **kwargs: Any) -> Event:
        return self._run(lambda s: s.add(subject, predicate, object, **kwargs))

    def search(self, query: str, **kwargs: Any) -> list[Fact]:
        return self._run(lambda s: s.search(query, **kwargs))

    def get(self, fact_id: UUID) -> Fact | None:
        return self._run(lambda s: s.get(fact_id))

    def blame(self, **kwargs: Any) -> list[Event]:
        return self._run(lambda s: s.blame(**kwargs))

    def revert(self, fact_id: UUID, to_event_id: UUID, **kwargs: Any) -> Event:
        return self._run(lambda s: s.revert(fact_id, to_event_id, **kwargs))

    def invalidate(self, fact_id: UUID, **kwargs: Any) -> Event:
        return self._run(lambda s: s.invalidate(fact_id, **kwargs))

    def diff(self, commit_a: Commit | UUID, commit_b: Commit | UUID) -> Diff:
        return self._run(lambda s: s.diff(commit_a, commit_b))

    def log(self, **kwargs: Any) -> list[Event]:
        return self._run(lambda s: s.log(**kwargs))

    def commit(self, label: str | None = None) -> Commit:
        return self._run(lambda s: s.commit(label))

    def observe(self, turn_id: str, text: str, session_id: str, *, role: str = "user") -> None:
        return self._run(lambda s: s.observe(turn_id, text, session_id, role=role))


class _DirectSDK:
    """Synchronous adapter for DirectMemory; validation and transactions stay in the core."""

    def __init__(self, client: Mnemo) -> None:
        self._client = client

    def _run(self, fn: Callable[[DirectMemory], Awaitable[T]]) -> T:
        return self._client._run(lambda store: fn(DirectMemory(store)))

    def create(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        request_id: UUID,
        actor: str | None = None,
        source_span: Any | None = None,
    ) -> MutationResult:
        return self._run(
            lambda d: d.create(
                subject,
                predicate,
                value,
                request_id=request_id,
                actor=actor,
                source_span=source_span,
            )
        )

    def update(
        self,
        fact_id: UUID,
        value: str,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None = None,
    ) -> MutationResult:
        return self._run(
            lambda d: d.update(
                fact_id,
                value,
                expected_event_id=expected_event_id,
                request_id=request_id,
                actor=actor,
            )
        )

    def revert(
        self,
        fact_id: UUID,
        to_event_id: UUID,
        *,
        expected_event_id: UUID,
        request_id: UUID,
        actor: str | None = None,
    ) -> MutationResult:
        return self._run(
            lambda d: d.revert(
                fact_id,
                to_event_id,
                expected_event_id=expected_event_id,
                request_id=request_id,
                actor=actor,
            )
        )

    def get(self, fact_id: UUID, event_id: UUID | None = None) -> CurrentValue | HistoricalValue:
        return self._run(lambda d: d.get(fact_id, event_id))

    def history(self, fact_id: UUID, *, cursor: str | None = None, limit: int = 20) -> HistoryPage:
        return self._run(lambda d: d.history(fact_id, cursor=cursor, limit=limit))

    def search_direct(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        return self._run(lambda d: d.search(query, limit=limit))
