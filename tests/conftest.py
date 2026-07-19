"""Shared test fixtures.

Two principles for the suite:
- **Hermetic by default.** Embeddings use a deterministic FakeEmbedder — no model,
  no network — so the canonical lifecycle/diff tests are fast and free.
- **A fresh store per test.** The schema is migrated once per run into a template
  file; each test gets its own copy, so nothing a test writes reaches another, and
  subprocesses (MCP servers, the CLI) can open the same file the test inspects.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mnemo.config import get_settings
from mnemo.db import apply_migrations
from mnemo.embedder import HashEmbedder as FakeEmbedder
from mnemo.sqlite import Connection, connect

# Test modules still written for Postgres while the port proceeds phase by phase.
# Each phase removes the files it ports; the list is empty when the port is done.
collect_ignore = [
    "test_agent_eval.py",
    "test_cli.py",
    "test_config.py",
    "test_demo.py",
    "test_install.py",
    "test_packaging.py",
    "test_project.py",
    "test_transfer.py",
    "test_web.py",
]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(get_settings().embed_dim)


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "mnemo.db"

    async def migrate() -> None:
        conn = await connect(path)
        try:
            await apply_migrations(conn)
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            await conn.close()

    asyncio.run(migrate())
    return path


@pytest.fixture
def _disposable_test_db(_migrated_template: Path, tmp_path: Path) -> str:
    """A migrated store file of this test's own (its path as text)."""
    path = tmp_path / "mnemo.db"
    shutil.copyfile(_migrated_template, path)
    path.chmod(0o600)
    return str(path)


@pytest.fixture
async def db(_disposable_test_db: str) -> AsyncIterator[Connection]:
    """A connection to this test's store."""
    conn = await connect(_disposable_test_db, create=False)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def store(db: Connection, fake_embedder: FakeEmbedder):
    """An async MnemoStore bound to this test's store."""
    from mnemo.core import MnemoStore

    return MnemoStore(db, fake_embedder)


@pytest.fixture
def clean_memory():
    """Kept for tests that commit through several connections: every test already
    has a store of its own, so there is nothing to clean."""
    yield


async def legacy_archive(store, fact_id, event_id):
    """Write the archived HEAD that the removed decay job left in older databases.

    Decay copied HEAD into a new ``tier='ephemeral'`` revision. Mnemo no longer
    creates one, but stores upgraded from earlier versions can contain it, so
    reads and reverts must still handle it.
    """
    from uuid import uuid4

    from mnemo.core import _EVENT_COLS
    from mnemo.models import Event

    async with store.conn.transaction():
        row = await store.conn.fetchrow(
            f"""
            INSERT INTO memory_event
                (event_id, fact_id, op, object_text, object_number, object_json,
                 provenance, actor, confidence, trust_level, source_span, session_id,
                 expires_at, valid_from, valid_to, parent_event_id, importance, write_score,
                 tier, reason, strength, recall_count, last_used)
            SELECT ?2, fact_id, 'UPDATE', object_text, object_number, object_json,
                   provenance, 'decay', confidence, trust_level, source_span, session_id,
                   expires_at, valid_from, valid_to, event_id, importance, write_score,
                   'ephemeral', 'archived by decay', strength, recall_count, last_used
            FROM memory_event WHERE event_id=?1
            RETURNING {_EVENT_COLS}
            """,
            event_id,
            uuid4(),
        )
        event = Event.from_row(row)
        await store._supersede(event_id, event.event_id)
        await store._set_head(fact_id, event.event_id)
    return event
