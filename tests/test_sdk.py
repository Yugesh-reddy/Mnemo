"""M2 smoke test for the sync Mnemo SDK wrapper (what the CLI/demo use).

Runs against the live test DB and commits for real, so it relies on the
``clean_memory`` teardown to truncate afterwards (keeps other tests isolated).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from mnemo import Mnemo


def test_sync_sdk_add_search_blame_revert(_disposable_test_db: str, fake_embedder, clean_memory):
    m = Mnemo(_disposable_test_db, fake_embedder)

    e1 = m.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
    assert e1.op == "ADD"

    e2 = m.add("user", "preferred_database", "MongoDB", provenance="agent_inference")
    assert e2.op == "UPDATE"

    results = m.search("database")
    assert any(f.object_text == "MongoDB" for f in results)

    history = m.blame(subject="user", predicate="preferred_database")
    assert [e.op for e in history] == ["ADD", "UPDATE"]

    e3 = m.revert(e1.fact_id, e1.event_id)
    assert e3.op == "REVERT"
    assert any(f.object_text == "PostgreSQL" for f in m.search("PostgreSQL"))


def test_sync_sdk_guarded_lifecycle(_disposable_test_db: str, fake_embedder, clean_memory):
    from mnemo import ErrorCode, MnemoError, MutationResult

    scope = dict(namespace="sdk-direct-" + uuid4().hex, user_id="user", agent_id="agent")
    m = Mnemo(_disposable_test_db, fake_embedder, **scope)
    first = m.direct.create(
        "user", "preferred_database", "PostgreSQL", request_id=uuid4(), actor="sdk-agent"
    )
    assert isinstance(first, MutationResult)
    changed = m.direct.update(
        first.fact_id, "MySQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    with pytest.raises(MnemoError) as error:
        m.direct.update(
            first.fact_id, "SQLite", expected_event_id=first.event_id, request_id=uuid4()
        )
    assert error.value.code == ErrorCode.REVISION_CONFLICT
    assert error.value.details["current_event_id"] == str(changed.event_id)
    restored = m.direct.revert(
        first.fact_id,
        first.event_id,
        expected_event_id=changed.event_id,
        request_id=uuid4(),
        actor="sdk-agent",
    )
    assert m.direct.get(first.fact_id).current_event_id == restored.event_id
    assert m.direct.get(first.fact_id, changed.event_id).value == "MySQL"
    page = m.direct.history(first.fact_id, limit=2)
    assert [entry.op for entry in page.entries] == ["REVERT", "UPDATE"]
    assert (
        m.direct.history(first.fact_id, cursor=page.next_cursor).entries[0].event_id
        == first.event_id
    )
    hits = m.direct.search_direct("PostgreSQL", limit=1)
    assert hits[0].event_id == restored.event_id and hits[0].value == "PostgreSQL"
    restarted = Mnemo(_disposable_test_db, fake_embedder, **scope)
    retry = restarted.direct.revert(
        first.fact_id,
        first.event_id,
        expected_event_id=changed.event_id,
        request_id=restored.request_id,
        actor="sdk-agent",
    )
    assert retry.replayed and retry.event_id == restored.event_id
    foreign = Mnemo(_disposable_test_db, fake_embedder, **{**scope, "agent_id": "other-agent"})
    with pytest.raises(MnemoError) as error:
        foreign.direct.get(first.fact_id)
    assert error.value.code == ErrorCode.NOT_FOUND
