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
from mnemo.models import Commit, Diff, Event, Fact

__version__ = "0.0.1"
__all__ = ["Mnemo", "MnemoStore", "Event", "Fact", "Commit", "Diff"]

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

    def diff(self, commit_a: Commit | UUID, commit_b: Commit | UUID) -> Diff:
        return self._run(lambda s: s.diff(commit_a, commit_b))

    def log(self, **kwargs: Any) -> list[Event]:
        return self._run(lambda s: s.log(**kwargs))

    def commit(self, label: str | None = None) -> Commit:
        return self._run(lambda s: s.commit(label))

    def observe(self, turn_id: str, text: str, session_id: str, *, role: str = "user") -> None:
        return self._run(lambda s: s.observe(turn_id, text, session_id, role=role))
