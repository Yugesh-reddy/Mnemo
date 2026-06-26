"""Pilot B: member/occurrence identities beside the legacy one-attribute-per-key rule."""

import asyncio
import json
from uuid import UUID, uuid4

import asyncpg
import pytest

from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector

ZERO = UUID(int=0)


async def heads(store, predicate):
    return sorted(str(f.value) for f in await store.list_current() if f.predicate == predicate)


@pytest.fixture
async def committed(_disposable_test_db, fake_embedder):
    """Autocommit connection in a fresh namespace: now() advances between writes."""
    conn = await asyncpg.connect(_disposable_test_db)
    await register_vector(conn)
    try:
        yield conn, "identity-" + uuid4().hex
    finally:
        await conn.close()


async def test_existing_facts_stay_attributes_and_the_schema_enforces_modes(store, db):
    event = await store.add("user", "budget", 100)
    fact = await store.get(event.fact_id)
    assert (fact.identity_mode, fact.identity_ref) == ("attribute", None)
    assert (
        await db.fetchval("SELECT identity_ref FROM memory_fact WHERE fact_id=$1", event.fact_id)
        == ZERO
    )
    for mode, ref in (("attribute", uuid4()), ("member", ZERO), ("bogus", uuid4())):
        with pytest.raises(asyncpg.CheckViolationError):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO memory_fact (subject, predicate, fact_key, identity_mode, "
                    "identity_ref) VALUES ('user', 'x', 'user|x', $1, $2)",
                    mode,
                    ref,
                )


@pytest.mark.parametrize(
    ("mode", "ref", "message"),
    [
        ("attribute", uuid4(), "attribute identities take no identity_ref"),
        ("member", None, "member identities require a nonzero identity_ref"),
        ("occurrence", ZERO, "occurrence identities require a nonzero identity_ref"),
        ("collection", uuid4(), "identity_mode must be"),
    ],
)
async def test_identity_arguments_are_validated_before_any_write(store, db, mode, ref, message):
    before = await db.fetchval("SELECT count(*) FROM memory_event")
    with pytest.raises(ValueError, match=message):
        await store.add("user", "learned_to_make", "kimchi", identity_mode=mode, identity_ref=ref)
    assert await db.fetchval("SELECT count(*) FROM memory_event") == before


async def test_attribute_replacement_keeps_one_head(store):
    first = await store.add("project", "budget", 100)
    second = await store.add("project", "budget", 150)
    assert second.fact_id == first.fact_id and second.op == "UPDATE"
    assert await heads(store, "budget") == ["150"]
    assert [e.value for e in await store.blame(subject="project", predicate="budget")] == [
        100,
        150,
    ]


async def test_members_coexist_under_one_subject_and_predicate(store):
    kimchi, lasagna = uuid4(), uuid4()
    a = await store.add(
        "user",
        "learned_to_make",
        "sauerkraut and kimchi",
        identity_mode="member",
        identity_ref=kimchi,
    )
    b = await store.add(
        "user", "learned_to_make", "vegan lasagna", identity_mode="member", identity_ref=lasagna
    )
    assert a.fact_id != b.fact_id and (a.op, b.op) == ("ADD", "ADD")
    assert await heads(store, "learned_to_make") == ["sauerkraut and kimchi", "vegan lasagna"]
    # Legacy key-based calls address only the attribute identity, never a member.
    legacy = await store.add("user", "learned_to_make", "bread")
    assert legacy.fact_id not in {a.fact_id, b.fact_id} and legacy.op == "ADD"
    assert len(await heads(store, "learned_to_make")) == 3
    assert [e.value for e in await store.blame(subject="user", predicate="learned_to_make")] == [
        "bread"
    ]
    blamed = await store.blame(
        subject="user", predicate="learned_to_make", identity_mode="member", identity_ref=kimchi
    )
    assert [e.event_id for e in blamed] == [a.event_id]
    member = await store.get(a.fact_id)
    assert (member.identity_mode, member.identity_ref) == ("member", kimchi)


async def test_correcting_one_occurrence_leaves_its_sibling(store):
    first, second = uuid4(), uuid4()
    one = await store.add(
        "user", "interviewed", "Lena on May 3", identity_mode="occurrence", identity_ref=first
    )
    two = await store.add(
        "user", "interviewed", "Lena on May 10", identity_mode="occurrence", identity_ref=second
    )
    fixed = await store.add(
        "user", "interviewed", "Lena on May 11", identity_mode="occurrence", identity_ref=second
    )
    assert (fixed.fact_id, fixed.op, fixed.parent_event_id) == (two.fact_id, "UPDATE", two.event_id)
    assert await heads(store, "interviewed") == ["Lena on May 11", "Lena on May 3"]
    assert [e.event_id for e in await store.blame(fact_id=one.fact_id)] == [one.event_id]
    # Repeating a member's exact value is idempotent.
    again = await store.add(
        "user", "interviewed", "Lena on May 3", identity_mode="occurrence", identity_ref=first
    )
    assert again.event_id == one.event_id


async def test_revert_and_archive_stay_inside_one_member(store):
    a_ref, b_ref = uuid4(), uuid4()
    a = await store.add(
        "user", "attended", "vegan class", identity_mode="member", identity_ref=a_ref
    )
    b1 = await store.add(
        "user", "attended", "fermentation workshop", identity_mode="member", identity_ref=b_ref
    )
    b2 = await store.add(
        "user",
        "attended",
        "fermentation workshop at the co-op",
        identity_mode="member",
        identity_ref=b_ref,
    )
    with pytest.raises(ValueError):
        await store.revert(b1.fact_id, a.event_id)  # a sibling's event is not a target
    await store.revert(b1.fact_id, b1.event_id)
    assert await heads(store, "attended") == ["fermentation workshop", "vegan class"]
    assert (await store.get(a.fact_id)).event_id == a.event_id
    archived = await store.archive_if_head(
        b1.fact_id, (await store.get(b1.fact_id)).event_id, actor="test", reason="archive one"
    )
    assert archived is not None and b2.fact_id == b1.fact_id
    assert await heads(store, "attended") == ["vegan class"]


async def test_historical_search_reads_members_as_of_their_time(committed, fake_embedder):
    db, namespace = committed
    store = MnemoStore(db, fake_embedder, namespace=namespace)
    first = await store.add(
        "user", "learned_to_make", "kimchi", identity_mode="member", identity_ref=uuid4()
    )
    cut = await db.fetchval("SELECT clock_timestamp()")
    await store.add(
        "user", "learned_to_make", "lasagna", identity_mode="member", identity_ref=uuid4()
    )
    then = await store.search("learned_to_make", as_of=cut, reinforce=False)
    assert [(h.fact_id, h.value, h.identity_mode) for h in then] == [
        (first.fact_id, "kimchi", "member")
    ]
    now = await store.search("learned_to_make", reinforce=False)
    assert sorted(h.value for h in now) == ["kimchi", "lasagna"]
    assert all(h.identity_ref is not None for h in now)


async def test_session_member_expires_without_touching_a_durable_sibling(committed, fake_embedder):
    db, namespace = committed
    settings = Settings(**{**get_settings().model_dump(), "session_ttl_seconds": 0.05})
    store = MnemoStore(db, fake_embedder, settings=settings, namespace=namespace)
    await store.add("user", "tried", "tempeh", identity_mode="member", identity_ref=uuid4())
    await store.add(
        "user",
        "tried",
        "natto",
        identity_mode="member",
        identity_ref=uuid4(),
        tier="session",
        session_id="s1",
    )
    assert await heads(store, "tried") == ["natto", "tempeh"]
    await asyncio.sleep(0.1)
    assert await heads(store, "tried") == ["tempeh"]


async def test_member_refs_are_scoped(db, fake_embedder):
    ref = uuid4()
    a = MnemoStore(db, fake_embedder, namespace="identity-a")
    b = MnemoStore(db, fake_embedder, namespace="identity-b")
    ea = await a.add("user", "learned_to_make", "kimchi", identity_mode="member", identity_ref=ref)
    eb = await b.add("user", "learned_to_make", "pasta", identity_mode="member", identity_ref=ref)
    assert ea.fact_id != eb.fact_id and eb.op == "ADD"
    assert await heads(a, "learned_to_make") == ["kimchi"]
    assert await b.get(ea.fact_id) is None


async def test_concurrent_retries_of_one_member_write_once(_disposable_test_db, fake_embedder):
    namespace, ref = "identity-race-" + uuid4().hex, uuid4()
    conns = [await asyncpg.connect(_disposable_test_db) for _ in range(2)]
    try:
        for conn in conns:
            await register_vector(conn)
        stores = [MnemoStore(c, fake_embedder, namespace=namespace) for c in conns]
        events = await asyncio.gather(
            *(
                s.add(
                    "user", "attended", "kimchi workshop", identity_mode="member", identity_ref=ref
                )
                for s in stores
            )
        )
        assert events[0].fact_id == events[1].fact_id
        assert events[0].event_id == events[1].event_id
        assert (
            await conns[0].fetchval(
                "SELECT count(*) FROM memory_event e JOIN memory_fact f USING (fact_id) "
                "WHERE f.namespace=$1",
                namespace,
            )
            == 1
        )
    finally:
        for conn in conns:
            await conn.close()


async def test_direct_create_is_create_only_for_the_attribute_identity(store):
    from mnemo.direct import DirectMemory
    from mnemo.errors import ErrorCode, MnemoError

    await store.add("user", "timezone", "UTC", identity_mode="member", identity_ref=uuid4())
    direct = DirectMemory(store)
    created = await direct.create("user", "timezone", "CET", request_id=uuid4())
    assert (await store.get(created.fact_id)).identity_mode == "attribute"
    with pytest.raises(MnemoError) as exc:
        await direct.create("user", "timezone", "PST", request_id=uuid4())
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


async def test_export_round_trip_keeps_member_identity(_disposable_test_db, fake_embedder):
    from mnemo.db import drop_isolated_schema, prepare_isolated_schema
    from mnemo.transfer import export_store, import_store

    settings = Settings(backend="hash", worker_enabled=False)
    schemas = ["mnemo_worker_" + uuid4().hex for _ in range(2)]
    source, destination = [await prepare_isolated_schema(_disposable_test_db, s) for s in schemas]
    try:
        ref = uuid4()
        await MnemoStore(source, fake_embedder).add(
            "user", "learned_to_make", "kimchi", identity_mode="member", identity_ref=ref
        )
        scope = {"namespace": "default", "user_id": "default", "agent_id": "default"}
        document = await export_store(source, scope=scope, settings=settings)
        assert json.loads(document)["facts"][0]["identity_ref"] == str(ref)
        await import_store(destination, document, settings=settings)
        [fact] = await MnemoStore(destination, fake_embedder).list_current()
        assert (fact.identity_mode, fact.identity_ref) == ("member", ref)
    finally:
        for conn, schema in zip((source, destination), schemas, strict=True):
            await drop_isolated_schema(conn, schema)
            await conn.close()
