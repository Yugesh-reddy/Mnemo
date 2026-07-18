"""Eight agent processes on one store file (plan §3): the file's write lock must give
exactly one winner per guarded change, one fact per identity, and a sound file."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from uuid import UUID, uuid4

from mnemo.core import MnemoStore
from mnemo.direct import DirectMemory
from mnemo.embedder import HashEmbedder
from mnemo.errors import MnemoError
from mnemo.sqlite import connect

PROCESSES = 8


def _wait(start_at: float) -> None:
    time.sleep(max(0.0, start_at - time.time()))


async def _store_call(path: str, call):
    conn = await connect(path, create=False)
    try:
        return await call(DirectMemory(MnemoStore(conn, HashEmbedder(16))))
    finally:
        await conn.close()


def _guarded_update(path: str, fact_id: str, expected: str, n: int, start_at: float) -> str:
    _wait(start_at)

    async def call(direct: DirectMemory) -> str:
        try:
            result = await direct.update(
                UUID(fact_id),
                f"make test-{n}",
                expected_event_id=UUID(expected),
                request_id=uuid4(),
                actor=f"agent-{n}",
            )
        except MnemoError as exc:
            return exc.code.value
        return result.status

    return asyncio.run(_store_call(path, call))


def _create(path: str, n: int, start_at: float) -> str:
    _wait(start_at)

    async def call(direct: DirectMemory) -> str:
        try:
            await direct.create("deploy", "command", f"make ship-{n}", request_id=uuid4())
        except MnemoError as exc:
            return exc.code.value
        return "applied"

    return asyncio.run(_store_call(path, call))


def _many_writes(path: str, n: int, start_at: float) -> int:
    _wait(start_at)

    async def call(direct: DirectMemory) -> int:
        store = direct.store
        for i in range(25):
            await store.add(f"agent-{n}", "note", f"value {i}", embedding=[1.0] * 16)
        return 25

    return asyncio.run(_store_call(path, call))


def _pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(PROCESSES, mp_context=multiprocessing.get_context("spawn"))


async def test_eight_processes_racing_one_guarded_update_give_one_winner(
    _disposable_test_db, db
) -> None:
    direct = DirectMemory(MnemoStore(db, HashEmbedder(16)))
    first = await direct.create("tests", "command", "make test-db", request_id=uuid4())
    start_at = time.time() + 2.0
    with _pool() as pool:
        futures = [
            pool.submit(
                _guarded_update,
                _disposable_test_db,
                str(first.fact_id),
                str(first.event_id),
                n,
                start_at,
            )
            for n in range(PROCESSES)
        ]
        outcomes = sorted(f.result(timeout=120) for f in futures)
    assert outcomes.count("applied") == 1
    assert outcomes.count("REVISION_CONFLICT") == PROCESSES - 1, outcomes
    history = await direct.history(first.fact_id)
    assert [e.op for e in history.entries] == ["UPDATE", "ADD"]


async def test_eight_processes_creating_one_subject_store_one_fact(_disposable_test_db, db) -> None:
    start_at = time.time() + 2.0
    with _pool() as pool:
        futures = [pool.submit(_create, _disposable_test_db, n, start_at) for n in range(PROCESSES)]
        outcomes = sorted(f.result(timeout=120) for f in futures)
    assert outcomes.count("applied") == 1
    assert outcomes.count("ALREADY_EXISTS") == PROCESSES - 1, outcomes
    assert await db.fetchval("SELECT count(*) FROM memory_fact") == 1
    assert await db.fetchval("SELECT count(*) FROM memory_event") == 1


async def test_concurrent_writers_leave_correct_heads_and_a_sound_file(
    _disposable_test_db, db
) -> None:
    start_at = time.time() + 2.0
    with _pool() as pool:
        futures = [
            pool.submit(_many_writes, _disposable_test_db, n, start_at) for n in range(PROCESSES)
        ]
        assert sum(f.result(timeout=120) for f in futures) == PROCESSES * 25
    assert await db.fetchval("SELECT count(*) FROM memory_event") == PROCESSES * 25
    # Each fact's HEAD is its newest event, and only that event is unsuperseded.
    wrong = await db.fetchval("""
        SELECT count(*) FROM memory_fact f
        WHERE f.current_event_id IS NOT (
          SELECT event_id FROM memory_event e WHERE e.fact_id = f.fact_id ORDER BY seq DESC
          LIMIT 1)
           OR (SELECT count(*) FROM memory_event e
               WHERE e.fact_id = f.fact_id AND e.superseded_at IS NULL) != 1
        """)
    assert wrong == 0
    assert await db.fetchval("PRAGMA integrity_check") == "ok"
    assert await db.fetchval("SELECT count(*) FROM memory_embedding") == PROCESSES * 25
    await asyncio.sleep(0)
