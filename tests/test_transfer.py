"""Phase 6 export/import: exact round trip into an empty store, all-or-nothing refusals."""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.db import drop_isolated_schema, prepare_isolated_schema
from mnemo.direct import DirectMemory
from mnemo.transfer import TransferError, export_store, import_store

SETTINGS = Settings(backend="hash", worker_enabled=False)
BIG = Decimal("12345678901234567890.123456789")


def scope(namespace):
    return {"namespace": namespace, "user_id": "default", "agent_id": "default"}


@pytest.fixture
async def source_and_destination(_disposable_test_db):
    schemas = ["mnemo_worker_" + uuid4().hex for _ in range(2)]
    conns = [await prepare_isolated_schema(_disposable_test_db, s) for s in schemas]
    try:
        yield conns
    finally:
        for conn, schema in zip(conns, schemas, strict=True):
            await drop_isolated_schema(conn, schema)
            await conn.close()


async def seed(conn, embedder, namespace):
    """Every event shape the store writes: ADD/UPDATE/REVERT/INVALIDATE, typed values,
    session tier, commits and guarded mutations with receipts."""
    store = MnemoStore(conn, embedder, namespace=namespace)
    first = await store.add(
        "user",
        "preferred_database",
        "PostgreSQL",
        provenance="direct_user_statement",
        source_span={"turn_ids": ["t1"]},
    )
    await store.commit("first")
    await store.add("user", "preferred_database", "MySQL")
    await store.revert(first.fact_id, first.event_id)
    await store.add("project", "budget", BIG)
    await store.add("user", "prefs", {"editor": "vim", "tabs": [2, 4]})
    await store.add("user", "currently_debugging", "flaky test", tier="session", session_id="s1")
    gone = await store.add("user", "location", "Berlin")
    await store.invalidate(gone.fact_id, reason="moved")
    await store.commit("second")

    direct = DirectMemory(store)
    created = await direct.create("user", "timezone", "UTC", request_id=uuid4())
    update_request = uuid4()
    updated = await direct.update(
        created.fact_id, "CET", expected_event_id=created.event_id, request_id=update_request
    )
    await direct.revert(
        created.fact_id, created.event_id, expected_event_id=updated.event_id, request_id=uuid4()
    )
    return store, created, update_request


def without_timestamp(document):
    doc = json.loads(document, parse_float=Decimal)
    doc.pop("exported_at")
    return doc


async def row_counts(conn):
    return tuple(
        await conn.fetchrow(
            "SELECT (SELECT count(*) FROM memory_fact), (SELECT count(*) FROM memory_event), "
            "(SELECT count(*) FROM memory_commit), (SELECT count(*) FROM memory_mutation_receipt)"
        )
    )


async def test_round_trip_is_exact_and_restored_store_keeps_working(
    source_and_destination, fake_embedder
):
    source, destination = source_and_destination
    store, created, update_request = await seed(source, fake_embedder, "transfer-a")
    await seed(source, fake_embedder, "transfer-b")

    document = await export_store(source, scope=scope("transfer-a"), settings=SETTINGS)
    counts = await import_store(destination, document, settings=SETTINGS)
    assert counts == {"facts": 6, "events": 11, "commits": 2, "receipts": 3}
    assert await row_counts(destination) == (6, 11, 2, 3)

    again = await export_store(destination, scope=scope("transfer-a"), settings=SETTINGS)
    assert without_timestamp(again) == without_timestamp(document)
    assert again.split("\n") != document.split("\n")  # only exported_at differs
    assert [line for line in again.split("\n") if '"exported_at"' not in line] == [
        line for line in document.split("\n") if '"exported_at"' not in line
    ]

    restored = MnemoStore(destination, fake_embedder, namespace="transfer-a")
    assert await restored.list_current() == await store.list_current()
    for fact in await store.list_current():
        assert await restored.blame(fact_id=fact.fact_id) == await store.blame(fact_id=fact.fact_id)
    budget = [f for f in await restored.list_current() if f.predicate == "budget"]
    assert budget[0].value == BIG
    assert await MnemoStore(destination, fake_embedder, namespace="transfer-b").list_current() == []

    # Receipts survive: the original update request replays instead of writing.
    direct = DirectMemory(restored)
    timezone_fact = created.fact_id
    head = (await direct.get(timezone_fact)).current_event_id
    replay = await direct.update(
        timezone_fact, "CET", expected_event_id=created.event_id, request_id=update_request
    )
    assert replay.replayed and replay.value == "CET"
    # New writes continue the imported sequence instead of reusing it.
    max_seq = await destination.fetchval("SELECT max(seq) FROM memory_event")
    fresh = await direct.update(timezone_fact, "PST", expected_event_id=head, request_id=uuid4())
    assert (
        await destination.fetchval("SELECT seq FROM memory_event WHERE event_id=$1", fresh.event_id)
        > max_seq
    )
    hits = await direct.search("PostgreSQL")
    assert [hit.value for hit in hits] == ["PostgreSQL"]


async def test_whole_store_export_carries_every_scope(source_and_destination, fake_embedder):
    source, destination = source_and_destination
    await seed(source, fake_embedder, "transfer-a")
    await seed(source, fake_embedder, "transfer-b")

    document = await export_store(source, scope=None, settings=SETTINGS)
    assert json.loads(document)["scope"] is None
    assert await import_store(destination, document, settings=SETTINGS) == {
        "facts": 12,
        "events": 22,
        "commits": 4,
        "receipts": 6,
    }
    assert without_timestamp(
        await export_store(destination, scope=None, settings=SETTINGS)
    ) == without_timestamp(document)


async def test_import_refuses_a_non_empty_destination(source_and_destination, fake_embedder):
    source, destination = source_and_destination
    await seed(source, fake_embedder, "transfer-a")
    document = await export_store(source, scope=scope("transfer-a"), settings=SETTINGS)
    await MnemoStore(destination, fake_embedder, namespace="other").add("user", "name", "Ada")
    before = await row_counts(destination)

    with pytest.raises(TransferError, match="not empty"):
        await import_store(destination, document, settings=SETTINGS)
    assert await row_counts(destination) == before


@pytest.mark.parametrize(
    ("old", "new", "settings", "message"),
    [
        ('"format_version": 1', '"format_version": 2', SETTINGS, "format_version"),
        ('"0010_mutation_receipts.sql"', '"0011_future.sql"', SETTINGS, "schema mismatch"),
        ("", "", Settings(backend="ollama", worker_enabled=False), "embedding mismatch"),
        ('"facts": 6', '"facts": 5', SETTINGS, "counts"),
    ],
)
async def test_import_refuses_incompatible_documents(
    source_and_destination, fake_embedder, old, new, settings, message
):
    source, destination = source_and_destination
    await seed(source, fake_embedder, "transfer-a")
    document = await export_store(source, scope=scope("transfer-a"), settings=SETTINGS)
    assert old in document
    with pytest.raises(TransferError, match=message):
        await import_store(destination, document.replace(old, new, 1), settings=settings)
    assert await row_counts(destination) == (0, 0, 0, 0)


async def test_import_rejects_rows_outside_the_declared_scope(
    source_and_destination, fake_embedder
):
    source, destination = source_and_destination
    await seed(source, fake_embedder, "transfer-a")
    doc = json.loads(await export_store(source, scope=scope("transfer-a"), settings=SETTINGS))
    doc["facts"][0]["namespace"] = "someone-else"
    with pytest.raises(TransferError, match="outside the document scope"):
        await import_store(destination, json.dumps(doc), settings=SETTINGS)

    doc = json.loads(await export_store(source, scope=scope("transfer-a"), settings=SETTINGS))
    doc["events"][0]["fact_id"] = str(uuid4())
    with pytest.raises(TransferError, match="missing from the document"):
        await import_store(destination, json.dumps(doc), settings=SETTINGS)
    assert await row_counts(destination) == (0, 0, 0, 0)


async def test_import_rolls_back_when_restored_rows_differ(source_and_destination, fake_embedder):
    source, destination = source_and_destination
    await seed(source, fake_embedder, "transfer-a")
    doc = json.loads(await export_store(source, scope=scope("transfer-a"), settings=SETTINGS))
    # Postgres silently drops unknown keys; verification must notice the loss.
    doc["events"][-1]["unexpected"] = "field"
    with pytest.raises(TransferError, match="differ from the document in events"):
        await import_store(destination, json.dumps(doc), settings=SETTINGS)
    assert await row_counts(destination) == (0, 0, 0, 0)
