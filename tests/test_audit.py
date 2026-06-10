"""Decision history is scoped, immutable, and contains no API credentials."""

import json

import asyncpg
import pytest

from mnemo.audit import decisions, gate_snapshot, queue_health, record_decision
from mnemo.config import Settings


def test_gate_fingerprint_excludes_credentials():
    first = Settings(_env_file=None, openai_api_key="secret-first")
    second = Settings(_env_file=None, openai_api_key="secret-second")
    assert gate_snapshot(first) == gate_snapshot(second)
    assert "secret" not in json.dumps(gate_snapshot(first))
    assert (
        gate_snapshot(first)["fingerprint"]
        != gate_snapshot(Settings(_env_file=None, w_imp=0.5))["fingerprint"]
    )


async def test_decisions_are_append_only_and_scoped(db):
    settings = Settings(_env_file=None)
    job = {"namespace": "project", "user_id": "alice", "agent_id": "agent", "turn_id": "t1"}
    decision_id = await record_decision(
        db,
        job=job,
        candidate={"subject": "user", "predicate": "location", "object": "Austin"},
        outcome="rejected",
        reason="unsupported assertion",
        settings=settings,
    )
    own = await decisions(db, namespace="project", user_id="alice", agent_id="agent")
    assert own[0]["decision_id"] == decision_id
    assert own[0]["candidate"]["object"] == "Austin"
    assert await decisions(db, namespace="project", user_id="bob", agent_id="agent") == []
    with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
        async with db.transaction():
            await db.execute(
                "UPDATE quality_decision SET reason='changed' WHERE decision_id=$1", decision_id
            )
    with pytest.raises(asyncpg.CheckViolationError, match="append-only"):
        async with db.transaction():
            await db.execute("DELETE FROM quality_decision WHERE decision_id=$1", decision_id)


async def test_queue_health_counts_only_its_scope(db):
    await db.execute(
        "INSERT INTO extraction_job(namespace,user_id,agent_id,turn_id,payload,status,"
        "attempts,last_error) "
        "VALUES ('project','alice','agent','t1','{}','failed',3,'timeout'), "
        "('project','bob','agent','t2','{}','pending',0,NULL)"
    )
    health = await queue_health(db, namespace="project", user_id="alice", agent_id="agent")
    assert health["statuses"] == {"failed": 1}
    assert health["retries"] == 2
    assert health["failures"][0]["last_error"] == "timeout"
