"""A memory's identity is permanent (spec §31): guards, seals and `mnemo verify`."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from mnemo import db as mdb
from mnemo.chain import FACT_COLUMNS, Witness, sha256_hex, verify, witness_path
from mnemo.core import MnemoStore
from mnemo.direct import DirectMemory
from mnemo.sqlite import connect
from tests.test_chain import around_mnemo, history

MIGRATION = (mdb.MIGRATIONS_DIR / "0003_fact_identity.sql").read_text()


def reseal(path: str, edit: str) -> None:
    """Change a fact's identity and recompute every seal: a self-consistent forgery."""
    around_mnemo(path, edit)
    raw = sqlite3.connect(path)
    previous, seals = "", []
    for seq, payload in raw.execute(
        "SELECT s.seq, json_array("
        + ", ".join(f"f.{c}" for c in FACT_COLUMNS)
        + ") FROM memory_fact_seal s JOIN memory_fact f USING (fact_id) ORDER BY s.seq"
    ).fetchall():
        previous = sha256_hex(previous + payload)
        seals.append((previous, seq))
    raw.executemany("UPDATE memory_fact_seal SET hash=? WHERE seq=?", seals)
    raw.commit()
    raw.close()


def test_the_seal_and_the_guard_cover_exactly_the_identity_columns():
    sealed = re.search(r"json_array\((NEW\..*?)\)\)\);", MIGRATION, re.S).group(1)
    assert tuple(c.strip().removeprefix("NEW.") for c in sealed.split(",")) == FACT_COLUMNS
    assert tuple(re.findall(r"NEW\.(\w+) IS NOT OLD", MIGRATION)) == FACT_COLUMNS
    schema = (mdb.MIGRATIONS_DIR / "0001_sqlite_init.sql").read_text()
    table = re.search(r"CREATE TABLE memory_fact \((.*?)\n\);", schema, re.S).group(1)
    columns = set(re.findall(r"^\s{2}(\w+)\s", table, re.M)) - {"UNIQUE", "CHECK"}
    # Everything but the two columns Mnemo moves: status and the HEAD pointer.
    assert columns - set(FACT_COLUMNS) == {"status", "current_event_id"}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE memory_fact SET namespace='another-project'",
        "UPDATE memory_fact SET subject='deploy'",
        "UPDATE memory_fact SET fact_key='x'",
        "UPDATE memory_fact SET session_id='s1'",
        "DELETE FROM memory_fact",
        "UPDATE memory_fact_seal SET hash='" + "0" * 64 + "'",
        "DELETE FROM memory_fact_seal",
    ],
)
async def test_sql_cannot_move_rename_or_delete_a_memory(store, db, statement):
    await history(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable|permanent|append-only"):
        await db.execute(statement)


async def test_mnemo_still_moves_status_and_head(store, db):
    await history(store)  # an update, a revert and a retract
    result = await verify(db)
    assert result.ok and result.facts == 2 and len(result.fact_head) == 64


async def test_an_identity_edit_around_the_triggers_fails_verify(
    _disposable_test_db, fake_embedder
):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
    finally:
        await conn.close()
    around_mnemo(_disposable_test_db, "UPDATE memory_fact SET subject='deploy' WHERE rowid=1")
    conn = await connect(_disposable_test_db)
    try:
        result = await verify(conn)
    finally:
        await conn.close()
    assert not result.ok
    assert [p for p in result.problems if "identity" in p and "changed outside Mnemo" in p]


async def test_deleted_and_smuggled_facts_fail_verify(_disposable_test_db, fake_embedder):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
    finally:
        await conn.close()
    around_mnemo(
        _disposable_test_db,
        "PRAGMA foreign_keys=OFF",
        "DELETE FROM memory_fact WHERE rowid=2",
        "INSERT INTO memory_fact (fact_id, subject, predicate, fact_key, created_at) "
        "VALUES ('" + str(uuid4()) + "', 'deploy', 'command', 'deploy|command', 'x')",
    )
    conn = await connect(_disposable_test_db)
    try:
        problems = (await verify(conn)).problems
    finally:
        await conn.close()
    assert [p for p in problems if "was deleted outside Mnemo (its seal remains)" in p]
    assert [p for p in problems if "has no seal" in p]


async def test_resealing_every_fact_is_caught_by_the_witness(_disposable_test_db, fake_embedder):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
        before = (await verify(conn)).witness
        assert before.fact_seq == 2 and len(before.fact_hash) == 64
    finally:
        await conn.close()
    reseal(_disposable_test_db, "UPDATE memory_fact SET namespace='elsewhere' WHERE rowid=1")
    conn = await connect(_disposable_test_db)
    try:
        assert (await verify(conn)).ok  # self-consistent on its own
        result = await verify(conn, before)
    finally:
        await conn.close()
    assert not result.ok
    assert "memory identities were rewritten since they were last verified" in result.problems[0]


async def test_a_witness_without_the_seal_head_still_verifies(store, db, tmp_path):
    await history(store)
    result = await verify(db)
    path = witness_path(tmp_path / "mnemo.db")
    path.write_text(f"{result.head_seq} {result.head}\n")
    old = Witness.read(path)
    assert old == Witness(result.head_seq, result.head)
    assert (await verify(db, old)).ok
    result.witness.write(path)
    assert Witness.read(path) == result.witness and len(path.read_text().split()) == 4


async def test_a_0_1_2_store_upgrades_with_its_chain_unchanged(tmp_path, fake_embedder):
    two = tmp_path / "migrations"
    two.mkdir()
    for name in ("0001_sqlite_init.sql", "0002_event_chain.sql"):
        shutil.copy(mdb.MIGRATIONS_DIR / name, two)
    conn = await connect(tmp_path / "old.db")
    try:
        await mdb.apply_migrations(conn, two)
        await history(MnemoStore(conn, fake_embedder))
        # A witness that records the event chain's head only, as before migration 0003.
        seq, digest = await conn.fetchrow(
            "SELECT seq, hash FROM memory_event_chain ORDER BY seq DESC LIMIT 1"
        )
        assert await mdb.apply_migrations(conn) == ["0003_fact_identity.sql"]
        after = await verify(conn, Witness(seq, digest))
        assert after.ok and after.head == digest and after.facts == 2
        await DirectMemory(MnemoStore(conn, fake_embedder)).create(
            "editor", "is", "vim", request_id=uuid4()
        )
        newest = await verify(conn, after.witness)
        assert newest.ok and newest.facts == 3 and newest.fact_head != after.fact_head
    finally:
        await conn.close()


async def test_an_imported_store_is_sealed(_migrated_template, tmp_path, fake_embedder):
    from mnemo.transfer import export_store, import_store

    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    for path in paths:
        shutil.copyfile(_migrated_template, path)
    source, destination = [await connect(path) for path in paths]
    try:
        await history(MnemoStore(source, fake_embedder))
        await import_store(destination, await export_store(source, scope=None))
        result = await verify(destination)
        assert result.ok and result.facts == 2
    finally:
        await source.close()
        await destination.close()


def test_a_tampered_identity_fails_the_command(_disposable_test_db, monkeypatch, capsys):
    import asyncio

    import mnemo.cli as cli
    from mnemo.config import get_settings
    from mnemo.embedder import HashEmbedder

    async def write() -> None:
        conn = await connect(_disposable_test_db)
        try:
            await history(MnemoStore(conn, HashEmbedder(16)))
        finally:
            await conn.close()

    asyncio.run(write())
    monkeypatch.setenv("MNEMO_DB", _disposable_test_db)
    get_settings.cache_clear()
    try:
        assert cli.main(["verify"]) == 0
        assert "2 memory identities sealed" in capsys.readouterr().out
        reseal(_disposable_test_db, "UPDATE memory_fact SET subject='x' WHERE rowid=1")
        assert cli.main(["verify"]) == 1
        err = capsys.readouterr().err
        assert "memory identities were rewritten" in err and "delete" in err
        assert len(witness_path(Path(_disposable_test_db)).read_text().split()) == 4
    finally:
        get_settings.cache_clear()
