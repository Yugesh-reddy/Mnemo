"""M2 smoke test for the sync Mnemo SDK wrapper (what the CLI/demo use).

Runs against the live test DB and commits for real, so it relies on the
``clean_memory`` teardown to truncate afterwards (keeps other tests isolated).
"""

from __future__ import annotations

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
