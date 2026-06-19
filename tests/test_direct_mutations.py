"""Guarded direct mutations, durable retry receipts and scoped reads."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from mnemo.core import MnemoStore
from mnemo.db import register_vector
from mnemo.errors import ErrorCode, MnemoError
from mnemo.models import MutationResult


def direct(store):
    from mnemo.direct import DirectMemory

    return DirectMemory(store)


async def create(d, value="PostgreSQL", **kwargs):
    return await d.create("user", "preferred_database", value, request_id=uuid4(), **kwargs)


async def counts(store):
    return tuple(
        await store.conn.fetchrow(
            "SELECT (SELECT count(*) FROM memory_event), "
            "(SELECT count(*) FROM memory_mutation_receipt)"
        )
    )


@asynccontextmanager
async def two_stores(dsn, embedder):
    a = await asyncpg.connect(dsn)
    try:
        b = await asyncpg.connect(dsn)
        try:
            await register_vector(a)
            await register_vector(b)
            namespace = "direct-test-" + uuid4().hex
            yield (
                MnemoStore(a, embedder, namespace=namespace),
                MnemoStore(b, embedder, namespace=namespace),
            )
        finally:
            await b.close()
    finally:
        await a.close()


def test_structured_error_serialization() -> None:
    from mnemo.errors import ErrorCode, MnemoError

    error = MnemoError(ErrorCode.NOT_FOUND, "Memory not found", context="read")
    assert error.to_dict() == {
        "code": "NOT_FOUND",
        "message": "Memory not found",
        "details": {"context": "read"},
    }


async def test_revert_copy_preserves_payload_trust_and_confidence(store) -> None:
    first = await store.add(
        "user",
        "preferences",
        {"database": "PostgreSQL"},
        confidence=0.42,
        source_span={"turn_ids": ["source-turn"]},
        actor="original-agent",
    )
    copied = await store._copy_as_revert(
        first.fact_id,
        first.event_id,
        provenance=None,
        trust_level=None,
        actor="host-llm",
        reason="restore source payload",
    )
    assert copied.value == first.value
    assert copied.source_span == first.source_span
    assert copied.confidence == first.confidence
    assert (copied.provenance, copied.trust_level, copied.actor) == (
        "agent_inference",
        "low",
        "host-llm",
    )
    vectors = await store.conn.fetch(
        "SELECT embedding::text FROM memory_event WHERE event_id=ANY($1::uuid[])",
        [first.event_id, copied.event_id],
    )
    assert len(vectors) == 2 and vectors[0] == vectors[1]
    legacy = await store.revert(first.fact_id, first.event_id)
    assert (legacy.provenance, legacy.trust_level, legacy.confidence) == (
        "human_review",
        "high",
        1.0,
    )


async def test_01_lifecycle_exact_strings_and_immutable_history(store):
    d = direct(store)
    original = "  PostgreSQL\nwith café 数据  "
    first = await create(d, original)
    changed = await d.update(
        first.fact_id, "MySQL\n", expected_event_id=first.event_id, request_id=uuid4()
    )
    payloads = """SELECT (to_jsonb(e) - ARRAY['superseded_at', 'superseded_by'])::text
                  FROM memory_event e WHERE fact_id=$1 ORDER BY seq"""
    before = await store.conn.fetch(payloads, first.fact_id)
    restored = await d.revert(
        first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
    )
    assert restored.value == original and restored.previous_event_id == changed.event_id
    assert restored.restored_from_event_id == first.event_id
    assert (await d.get(first.fact_id)).current_event_id == restored.event_id
    assert (await d.get(first.fact_id)).value == original
    assert (await store.conn.fetch(payloads, first.fact_id))[:2] == before
    page = await d.history(first.fact_id)
    assert [row.op for row in page.entries] == ["REVERT", "UPDATE", "ADD"]
    assert [row.value_preview for row in page.entries] == [original, "MySQL\n", original]
    assert page.entries[0].restored_from_event_id == first.event_id
    assert page.next_cursor is None


async def test_02_revert_of_revert_targets_displaced_head(store):
    d = direct(store)
    first = await create(d)
    other = await d.create("user", "name", "Ada", request_id=uuid4())
    changed = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    restored = await d.revert(
        first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
    )
    undone = await d.revert(
        first.fact_id,
        restored.previous_event_id,
        expected_event_id=restored.event_id,
        request_id=uuid4(),
    )
    assert undone.value == "MySQL"
    assert undone.previous_event_id == restored.event_id
    assert (await d.get(other.fact_id)).current_event_id == other.event_id


async def test_03_no_change_is_explicit_and_receipted(store):
    d = direct(store)
    first = await create(d)
    unchanged = await d.update(
        first.fact_id, first.value, expected_event_id=first.event_id, request_id=uuid4()
    )
    restored = await d.revert(
        first.fact_id, first.event_id, expected_event_id=first.event_id, request_id=uuid4()
    )
    for result in (unchanged, restored):
        assert result.status == "no_change" and result.event_id == first.event_id
        assert result.previous_event_id is None
    assert restored.restored_from_event_id == first.event_id
    assert await counts(store) == (1, 3)
    retry = await d.update(
        first.fact_id,
        first.value,
        expected_event_id=first.event_id,
        request_id=unchanged.request_id,
    )
    assert retry.replayed and retry.status == "no_change"
    assert await counts(store) == (1, 3)


async def test_04_spelling_change_is_a_revision_here_not_in_legacy_add(store):
    d = direct(store)
    first = await create(d)
    alias = await store.add("user", "preferred_database", "Postgres")
    assert alias.event_id == first.event_id
    changed = await d.update(
        first.fact_id, "Postgres", expected_event_id=first.event_id, request_id=uuid4()
    )
    assert changed.event_id != first.event_id and changed.value == "Postgres"
    with pytest.raises(MnemoError) as error:
        await d.create(" USER ", "favorite_db", "SQLite", request_id=uuid4())
    assert error.value.code == ErrorCode.ALREADY_EXISTS
    assert error.value.details == {
        "fact_id": str(first.fact_id),
        "current_event_id": str(changed.event_id),
        "fact_key": "user|preferred_database",
    }


async def test_05_two_writers_same_expected_head_one_conflicts(
    _disposable_test_db, clean_memory, fake_embedder
):
    async with two_stores(_disposable_test_db, fake_embedder) as (a, b):
        da, db = direct(a), direct(b)
        first = await create(da)
        results = await asyncio.gather(
            da.update(first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()),
            db.update(
                first.fact_id, "SQLite", expected_event_id=first.event_id, request_id=uuid4()
            ),
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, MutationResult)]
        errors = [r for r in results if isinstance(r, MnemoError)]
        assert len(ok) == len(errors) == 1
        assert errors[0].code == ErrorCode.REVISION_CONFLICT
        assert (await da.get(first.fact_id)).current_event_id == ok[0].event_id
        assert await counts(a) == (2, 2)


async def test_06_a_b_a_still_rejects_stale_expected_event(store):
    d = direct(store)
    first = await create(d)
    changed = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    again = await d.update(
        first.fact_id, first.value, expected_event_id=changed.event_id, request_id=uuid4()
    )
    before = await counts(store)
    for mutate in (
        lambda: d.update(
            first.fact_id, first.value, expected_event_id=first.event_id, request_id=uuid4()
        ),
        lambda: d.revert(
            first.fact_id, first.event_id, expected_event_id=first.event_id, request_id=uuid4()
        ),
    ):
        with pytest.raises(MnemoError) as error:
            await mutate()
        assert error.value.code == ErrorCode.REVISION_CONFLICT
        assert error.value.details == {"current_event_id": str(again.event_id)}
    assert await counts(store) == before


async def test_07_duplicate_request_replays_receipt(store):
    d = direct(store)
    first = await create(d)
    replay = await d.create("user", "preferred_database", first.value, request_id=first.request_id)
    assert replay.model_dump(exclude={"replayed"}) == first.model_dump(exclude={"replayed"})
    assert replay.replayed and not first.replayed
    assert await counts(store) == (1, 1)


async def test_08_replay_after_later_update_leaves_head_intact(store):
    d = direct(store)
    first = await create(d)
    changed = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    reverted = await d.revert(
        first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
    )
    later = await d.update(
        first.fact_id, "SQLite", expected_event_id=reverted.event_id, request_id=uuid4()
    )
    retry = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=changed.request_id
    )
    restore_retry = await d.revert(
        first.fact_id,
        first.event_id,
        expected_event_id=changed.event_id,
        request_id=reverted.request_id,
    )
    assert retry.replayed and retry.event_id == changed.event_id
    assert restore_retry.replayed and restore_retry.event_id == reverted.event_id
    assert (await d.get(first.fact_id)).current_event_id == later.event_id
    assert await counts(store) == (4, 4)


async def test_09_request_id_reuse_with_different_payload_fails(store):
    d = direct(store)
    first = await create(d)
    for mutate in (
        lambda: d.create("user", "preferred_database", "MySQL", request_id=first.request_id),
        lambda: d.update(
            first.fact_id,
            first.value,
            expected_event_id=first.event_id,
            request_id=first.request_id,
        ),
        lambda: d.create(
            "user",
            "preferred_database",
            first.value,
            actor="different",
            request_id=first.request_id,
        ),
    ):
        with pytest.raises(MnemoError) as error:
            await mutate()
        assert error.value.code == ErrorCode.REQUEST_ID_REUSED
    assert await counts(store) == (1, 1)


async def test_10_failure_before_receipt_rolls_back_event_and_head(store, monkeypatch):
    d = direct(store)
    first = await create(d)
    before = await store.conn.fetchrow(
        "SELECT * FROM memory_event WHERE event_id=$1", first.event_id
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("receipt failure")

    monkeypatch.setattr(d, "_insert_receipt", boom)
    with pytest.raises(RuntimeError, match="receipt failure"):
        await d.update(first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4())
    assert await counts(store) == (1, 1)
    assert (await d.get(first.fact_id)).current_event_id == first.event_id
    assert (
        await store.conn.fetchrow("SELECT * FROM memory_event WHERE event_id=$1", first.event_id)
        == before
    )


async def test_11_receipts_persist_across_connections(
    _disposable_test_db, clean_memory, fake_embedder
):
    async with two_stores(_disposable_test_db, fake_embedder) as (a, b):
        first = await create(direct(a))
        await a.conn.close()
        replay = await direct(b).create(
            "user", "preferred_database", first.value, request_id=first.request_id
        )
        assert replay.replayed and replay.event_id == first.event_id
        assert await counts(b) == (1, 1)


@pytest.mark.parametrize("scope_field", ["namespace", "user_id", "agent_id"])
async def test_12_foreign_ids_disclose_nothing_and_write_nothing(store, scope_field):
    d = direct(store)
    first = await create(d)
    foreign = direct(MnemoStore(store.conn, store.embedder, **{scope_field: "foreign"}))
    before = await counts(store)
    for fact_id in (first.fact_id, uuid4()):
        for attempt in (
            lambda fact_id=fact_id: foreign.get(fact_id),
            lambda fact_id=fact_id: foreign.get(fact_id, first.event_id),
            lambda fact_id=fact_id: foreign.history(fact_id),
            lambda fact_id=fact_id: foreign.update(
                fact_id, "x", expected_event_id=first.event_id, request_id=uuid4()
            ),
            lambda fact_id=fact_id: foreign.revert(
                fact_id, first.event_id, expected_event_id=first.event_id, request_id=uuid4()
            ),
        ):
            with pytest.raises(MnemoError) as error:
                await attempt()
            assert error.value.code == ErrorCode.NOT_FOUND
            assert str(fact_id) not in str(error.value) and str(first.event_id) not in str(
                error.value
            )
    with pytest.raises(MnemoError) as error:
        await d.revert(first.fact_id, uuid4(), expected_event_id=first.event_id, request_id=uuid4())
    assert error.value.code == ErrorCode.INVALID_RESTORE_TARGET
    assert error.value.details == {}
    assert await counts(store) == before


@pytest.mark.parametrize("state", ["archived", "invalidated", "session", "expired", "session_id"])
async def test_13_unsupported_states_cannot_be_revived(store, state):
    d = direct(store)
    first = await create(d)
    if state == "archived":
        bad = await store.archive_if_head(
            first.fact_id, first.event_id, actor="test", reason="test"
        )
    elif state == "invalidated":
        bad = await store.invalidate(first.fact_id)
    elif state in ("session", "session_id"):
        bad = await store.add(
            "user",
            "preferred_database",
            "session value",
            tier="session" if state == "session" else "durable",
            session_id="s",
        )
    else:
        bad = await store._emit_update(
            first.fact_id,
            first.event_id,
            "expired",
            provenance="agent_inference",
            actor=None,
            confidence=1.0,
            trust_level="low",
            source_span=None,
            valid_from=None,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    before = await counts(store)
    for mutate in (
        lambda: d.update(first.fact_id, "new", expected_event_id=bad.event_id, request_id=uuid4()),
        lambda: d.revert(
            first.fact_id, first.event_id, expected_event_id=bad.event_id, request_id=uuid4()
        ),
    ):
        with pytest.raises(MnemoError) as error:
            await mutate()
        assert error.value.code == ErrorCode.UNSUPPORTED_STATE
    assert await counts(store) == before
    # The same ineligible event stays unrestorable when a legacy human restore
    # has made the current HEAD usable again.
    head = await store.revert(first.fact_id, first.event_id)
    historical = await d.get(first.fact_id, bad.event_id)
    assert not historical.restorable
    with pytest.raises(MnemoError) as error:
        await d.revert(
            first.fact_id, bad.event_id, expected_event_id=head.event_id, request_id=uuid4()
        )
    assert error.value.code == ErrorCode.UNSUPPORTED_STATE


async def test_14_direct_revert_preserves_source_trust_and_records_actor(store):
    d = direct(store)
    first = await create(d, source_span={"turn_ids": ["turn-1"]})
    changed = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4(), actor="writer"
    )
    for result in (first, changed):
        event = await store._get_event(result.event_id)
        assert (event.provenance, event.trust_level) == ("agent_inference", "low")
    restored = await d.revert(
        first.fact_id,
        first.event_id,
        expected_event_id=changed.event_id,
        request_id=uuid4(),
        actor="host-llm",
    )
    event = await store._get_event(restored.event_id)
    assert (event.provenance, event.trust_level, event.actor) == (
        "agent_inference",
        "low",
        "host-llm",
    )
    assert event.source_span == {"turn_ids": ["turn-1"]}
    assert restored.previous_event_id == changed.event_id
    legacy = await store.revert(first.fact_id, first.event_id)
    assert (legacy.provenance, legacy.trust_level) == ("human_review", "high")


async def test_15_current_reads_exclude_superseded_after_revert(store):
    d = direct(store)
    first = await create(d)
    changed = await d.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    restored = await d.revert(
        first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
    )
    hits = await d.search("preferred_database")
    assert [(hit.fact_id, hit.event_id, hit.value) for hit in hits] == [
        (first.fact_id, restored.event_id, "PostgreSQL")
    ]
    assert (await d.get(first.fact_id)).value == "PostgreSQL"
    assert (await store._get_event(restored.event_id)).recall_count == 0
    historical = await d.get(first.fact_id, changed.event_id)
    assert historical.value == "MySQL" and historical.restorable
    assert historical.current_event_id == restored.event_id


async def test_16_history_pages_are_stable_under_concurrent_writes(
    _disposable_test_db, clean_memory, fake_embedder
):
    async with two_stores(_disposable_test_db, fake_embedder) as (a, b):
        da, db = direct(a), direct(b)
        first = await create(da, "x" * 300)
        head = first
        ids = [first.event_id]
        for value in ("B", "C", "D"):
            head = await da.update(
                first.fact_id, value, expected_event_id=head.event_id, request_id=uuid4()
            )
            ids.append(head.event_id)
        page = await da.history(first.fact_id, limit=2)
        assert page.current_event_id == head.event_id and page.next_cursor
        seen = [entry.event_id for entry in page.entries]
        await db.update(first.fact_id, "E", expected_event_id=head.event_id, request_id=uuid4())
        tail = await da.history(first.fact_id, cursor=page.next_cursor, limit=2)
        seen.extend(entry.event_id for entry in tail.entries)
        assert seen == ids[::-1] and tail.next_cursor is None
        assert tail.entries[-1].value_preview == "x" * 256
        assert tail.entries[-1].value_truncated
        assert (await da.get(first.fact_id, first.event_id)).value == "x" * 300


@pytest.mark.parametrize("value", [None, 42, "", " \n\t", "é" * 4097, "x\x00y", "\ud800"])
async def test_invalid_values_do_not_embed_or_write(store, monkeypatch, value):
    d = direct(store)
    first = await create(d)

    def forbidden(*args):
        raise AssertionError("invalid values must fail before embedding")

    monkeypatch.setattr(store.embedder, "embed", forbidden)
    for mutate in (
        lambda: d.create("user", "name", value, request_id=uuid4()),
        lambda: d.update(
            first.fact_id, value, expected_event_id=first.event_id, request_id=uuid4()
        ),
    ):
        with pytest.raises(MnemoError) as error:
            await mutate()
        assert error.value.code == ErrorCode.INVALID_INPUT
    assert await counts(store) == (1, 1)


async def test_legacy_typed_values_can_be_read_and_restored(store):
    d = direct(store)
    for predicate, value in (("count", 42), ("settings", {"database": "PostgreSQL"})):
        first = await store.add("user", predicate, value)
        assert (await d.get(first.fact_id)).value == str(first.value)
        changed = await d.update(
            first.fact_id, "text", expected_event_id=first.event_id, request_id=uuid4()
        )
        restored = await d.revert(
            first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4()
        )
        assert restored.value == str(first.value)
        assert (await store.get(first.fact_id)).value == first.value


async def test_invalid_history_cursors_and_foreign_targets_are_rejected(store):
    d = direct(store)
    first = await create(d)
    other = await d.create("user", "name", "Ada", request_id=uuid4())
    bad = base64.urlsafe_b64encode(
        json.dumps({"f": str(other.fact_id), "max": 9, "before": 8}).encode()
    ).decode()
    overflow = base64.urlsafe_b64encode(
        json.dumps({"f": str(first.fact_id), "max": 2**63 - 1, "before": 2**63}).encode()
    ).decode()
    for cursor in ("not-base64", bad, overflow, base64.urlsafe_b64encode(b"[]").decode()):
        with pytest.raises(MnemoError) as error:
            await d.history(first.fact_id, cursor=cursor)
        assert error.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(MnemoError) as error:
        await d.revert(
            first.fact_id, other.event_id, expected_event_id=first.event_id, request_id=uuid4()
        )
    assert error.value.code == ErrorCode.INVALID_RESTORE_TARGET
    assert str(other.event_id) not in str(error.value)
    with pytest.raises(MnemoError) as error:
        await d.get(first.fact_id, other.event_id)
    assert error.value.code == ErrorCode.NOT_FOUND


async def test_simultaneous_identical_requests_share_one_receipt(
    _disposable_test_db, clean_memory, fake_embedder
):
    async with two_stores(_disposable_test_db, fake_embedder) as (a, b):
        request_id = uuid4()
        results = await asyncio.gather(
            *(
                direct(store).create("user", "name", "Ada", request_id=request_id)
                for store in (a, b)
            )
        )
        assert results[0].event_id == results[1].event_id
        assert sorted(result.replayed for result in results) == [False, True]
        assert await counts(a) == (1, 1)


async def test_receipt_payload_comparison_distinguishes_json_booleans(store):
    d = direct(store)
    first = await create(d, source_span={"marker": 1})
    with pytest.raises(MnemoError) as error:
        await d.create(
            "user",
            "preferred_database",
            first.value,
            request_id=first.request_id,
            source_span={"marker": True},
        )
    assert error.value.code == ErrorCode.REQUEST_ID_REUSED
    assert await counts(store) == (1, 1)


async def test_value_at_utf8_size_limit_is_preserved_and_creation_undo_is_unsupported(store):
    d = direct(store)
    first = await create(d, "é" * 4096)
    assert (await d.get(first.fact_id)).value == "é" * 4096
    with pytest.raises(MnemoError) as error:
        await d.revert(first.fact_id, None, expected_event_id=first.event_id, request_id=uuid4())
    assert error.value.code == ErrorCode.UNSUPPORTED_OPERATION
    assert await counts(store) == (1, 1)
