"""The pilot must execute model requests faithfully and score actual database effects."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from mnemo.direct import DirectMemory


def reply(name=None, **arguments):
    message = {"role": "assistant", "content": "Done." if name is None else ""}
    if name:
        message["tool_calls"] = [{"function": {"name": name, "arguments": arguments}}]
    return {"message": message, "done": True, "eval_count": 10}


class ScriptedHost:
    def __init__(self, *responses):
        self.responses = iter(responses)

    async def __call__(self, messages, tools):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


async def test_pilot_bridge_executes_all_six_tools_with_real_receipts(store):
    from examples.agent_undo import PilotTools

    tools = await PilotTools.build(DirectMemory(store))
    first = await tools.call(
        "memory_create",
        dict(
            subject="user",
            predicate="preferred_database",
            value="PostgreSQL",
            request_id=str(uuid4()),
        ),
    )
    changed = await tools.call(
        "memory_update",
        dict(
            fact_id=first["fact_id"],
            value="MySQL",
            expected_event_id=first["event_id"],
            request_id=str(uuid4()),
        ),
    )
    history = await tools.call("memory_history", {"fact_id": first["fact_id"]})
    assert [entry["op"] for entry in history["entries"]] == ["UPDATE", "ADD"]
    restored = await tools.call(
        "memory_revert",
        dict(
            fact_id=first["fact_id"],
            to_event_id=first["event_id"],
            expected_event_id=changed["event_id"],
            request_id=str(uuid4()),
        ),
    )
    current = await tools.call("memory_get", {"fact_id": first["fact_id"]})
    assert current["value"] == "PostgreSQL" and current["trust_level"] == "low"
    hits = await tools.call("memory_search", {"query": "PostgreSQL"})
    assert hits["hits"][0]["event_id"] == restored["event_id"]
    assert await store.conn.fetchval("SELECT count(*) FROM memory_mutation_receipt") == 3


@pytest.mark.parametrize(
    "arguments",
    [{"actor": "human"}, {"namespace": "foreign"}, {"value": 42}, {"request_id": "bad"}],
)
async def test_model_cannot_smuggle_scope_or_malformed_mutations(store, arguments):
    from examples.agent_undo import PilotTools

    tools = await PilotTools.build(DirectMemory(store))
    result = await tools.call(
        "memory_create",
        {
            "subject": "user",
            "predicate": "preferred_database",
            "value": "PostgreSQL",
            "request_id": str(uuid4()),
            **arguments,
        },
    )
    assert result["code"] == "INVALID_INPUT"
    assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 0


async def test_loop_preserves_tool_error_and_continues_with_model_retry(store):
    from examples.agent_undo import PilotTools, run_turn

    direct = DirectMemory(store)
    first = await direct.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())
    tools = await PilotTools.build(direct)
    host = ScriptedHost(
        reply(
            "memory_update",
            fact_id=str(first.fact_id),
            value="MySQL",
            expected_event_id=str(uuid4()),
            request_id=str(uuid4()),
        ),
        reply("memory_get", fact_id=str(first.fact_id)),
        reply(
            "memory_update",
            fact_id=str(first.fact_id),
            value="MySQL",
            expected_event_id=str(first.event_id),
            request_id=str(uuid4()),
        ),
        reply(),
    )
    result = await run_turn(host, tools, "Change my preferred database to MySQL.")
    assert result["status"] == "answered"
    assert result["tool_calls"][0]["result"]["code"] == "REVISION_CONFLICT"
    sent_after_error = result["model_calls"][1]["messages"][-1]
    assert sent_after_error["tool_name"] == "memory_update"
    assert json.loads(sent_after_error["content"])["code"] == "REVISION_CONFLICT"
    assert (await direct.get(first.fact_id)).value == "MySQL"
    assert len((await direct.history(first.fact_id)).entries) == 2


async def test_loop_bounds_calls_without_inventing_a_success(store):
    from examples.agent_undo import PilotTools, run_turn

    tools = await PilotTools.build(DirectMemory(store))
    host = ScriptedHost(*(reply("memory_search", query="database") for _ in range(5)))
    result = await run_turn(host, tools, "Remember PostgreSQL", max_tool_calls=2)
    assert result["status"] == "tool_limit" and len(result["tool_calls"]) == 2
    assert result["final_text"] == ""


async def test_host_failure_retains_prior_mutation_and_partial_transcript(store):
    from examples.agent_undo import PilotTools, run_turn

    tools = await PilotTools.build(DirectMemory(store))
    host = ScriptedHost(
        reply(
            "memory_create",
            subject="user",
            predicate="preferred_database",
            value="PostgreSQL",
            request_id=str(uuid4()),
        ),
        httpx.ReadTimeout("model timed out"),
    )
    result = await run_turn(host, tools, "Remember PostgreSQL")
    assert result["status"] == "host_error" and "ReadTimeout" in result["error"]
    assert len(result["tool_calls"]) == 1
    assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 1


async def test_conflict_injection_is_an_actual_intervening_revision(store):
    from examples.agent_undo import PilotTools

    direct = DirectMemory(store)
    first = await direct.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())
    tools = await PilotTools.build(direct, conflict_fact_id=first.fact_id)
    result = await tools.call(
        "memory_update",
        {
            "fact_id": str(first.fact_id),
            "value": "MySQL",
            "expected_event_id": str(first.event_id),
            "request_id": str(uuid4()),
        },
    )
    assert result["code"] == "REVISION_CONFLICT"
    assert (await direct.get(first.fact_id)).value == "SQLite"
    assert len(tools.injections) == 1
    current = await direct.get(first.fact_id)
    result = await tools.call(
        "memory_update",
        {
            "fact_id": str(first.fact_id),
            "value": "MySQL",
            "expected_event_id": str(current.current_event_id),
            "request_id": str(uuid4()),
        },
    )
    assert result["value"] == "MySQL"
    assert len((await direct.history(first.fact_id)).entries) == 3


async def test_scorer_counts_wrong_and_extra_events_even_if_final_head_is_correct(store):
    from examples.agent_undo import SCENARIOS, assess, snapshot

    direct = DirectMemory(store)
    before = await snapshot(direct)
    first = await direct.create("user", "preferred_database", "Wrong", request_id=uuid4())
    await direct.update(
        first.fact_id, "PostgreSQL", expected_event_id=first.event_id, request_id=uuid4()
    )
    turn = {"status": "answered", "tool_calls": [], "final_text": "Done."}
    score = assess(SCENARIOS[0], before, await snapshot(direct), turn, [])
    assert not score["passed"]
    assert score["unintended_mutations"] == 2


@pytest.mark.parametrize("final_text", ["I restored it.", "Done. Anything else?"])
async def test_ambiguous_noop_is_not_scored_as_a_clarification(store, final_text):
    from examples.agent_undo import SCENARIOS, assess, snapshot

    state = await snapshot(DirectMemory(store))
    turn = {"status": "answered", "tool_calls": [], "final_text": final_text}
    score = assess(SCENARIOS[4], state, state, turn, [])
    assert not score["passed"] and score["unintended_mutations"] == 0


@pytest.mark.parametrize(
    "host,model",
    [
        ("https://api.ollama.com", "qwen3"),
        ("http://localhost:11434", "gpt-oss:120b-cloud"),
    ],
)
def test_pilot_rejects_paid_or_remote_execution_before_connecting(host, model):
    from examples.agent_undo import OllamaHost

    with pytest.raises(ValueError):
        OllamaHost(host, model)


@pytest.mark.parametrize(
    "history_before,historical_get", [(False, False), (True, False), (True, True)]
)
async def test_pilot_requires_current_read_and_prior_history(store, history_before, historical_get):
    from examples.agent_undo import SCENARIOS, PilotTools, assess, seed, snapshot

    direct = DirectMemory(store)
    fact_id = await seed(direct, SCENARIOS[2])
    before = await snapshot(direct)
    tools = await PilotTools.build(direct)
    calls = []

    async def call(name, **arguments):
        result = await tools.call(name, arguments)
        calls.append({"name": name, "arguments": arguments, "result": result})
        return result

    await call("memory_search", query="database")
    get_args = {"fact_id": str(fact_id)}
    if historical_get:
        get_args["event_id"] = before["events"][0]["event_id"]
    current = await call("memory_get", **get_args)
    if history_before:
        await call("memory_history", fact_id=str(fact_id).upper())
    await call(
        "memory_revert",
        fact_id=str(fact_id),
        to_event_id=before["events"][0]["event_id"],
        expected_event_id=current["current_event_id"],
        request_id=str(uuid4()),
    )
    await call("memory_history", fact_id=str(fact_id))
    score = assess(
        SCENARIOS[2],
        before,
        await snapshot(direct),
        {"status": "answered", "tool_calls": calls, "final_text": "Restored PostgreSQL."},
        [],
    )
    assert score["passed"] == (history_before and not historical_get)
    if not history_before:
        assert "history" in " ".join(score["reasons"])


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"message": {"role": "assistant", "content": 42}},
        {"message": {"role": "assistant", "content": "", "tool_calls": "bad"}},
    ],
)
async def test_malformed_host_envelopes_remain_failed_records(store, response):
    from examples.agent_undo import PilotTools, run_turn

    tools = await PilotTools.build(DirectMemory(store))
    result = await run_turn(ScriptedHost(response), tools, "Remember PostgreSQL")
    assert result["status"] == "invalid_response"
    assert result["model_calls"][0]["response"] == response
    assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 0


async def test_round_budget_stops_a_repeated_read_loop(store):
    from examples.agent_undo import PilotTools, run_turn

    tools = await PilotTools.build(DirectMemory(store))
    result = await run_turn(
        ScriptedHost(reply("memory_search", query="database")),
        tools,
        "Remember PostgreSQL",
        max_rounds=1,
    )
    assert result["status"] == "round_limit" and len(result["model_calls"]) == 1


async def test_interrupted_turn_retains_the_same_live_record_after_a_write(store):
    import asyncio

    from examples.agent_undo import PilotTools, run_turn

    tools = await PilotTools.build(DirectMemory(store))
    responses = iter(
        [
            reply(
                "memory_create",
                subject="user",
                predicate="preferred_database",
                value="PostgreSQL",
                request_id=str(uuid4()),
            )
        ]
    )

    async def interrupted(messages, schemas):
        try:
            return next(responses)
        except StopIteration:
            raise asyncio.CancelledError() from None

    record = {}
    with pytest.raises(asyncio.CancelledError):
        await run_turn(interrupted, tools, "Remember PostgreSQL", record=record)
    assert record["status"] == "cancelled"
    assert len(record["tool_calls"]) == 1
    assert record["tool_calls"][0]["result"]["value"] == "PostgreSQL"
    assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 1


async def test_cancelled_pilot_saves_audit_before_dropping_only_its_database(
    _disposable_test_db, monkeypatch, tmp_path
):
    import asyncio
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg

    from examples import agent_undo
    from mnemo.config import Settings

    class InterruptedHost:
        def __init__(self, *args):
            self.count = 0

        async def metadata(self):
            return {"model": "scripted-test"}

        async def __call__(self, *args):
            self.count += 1
            if self.count > 1:
                raise asyncio.CancelledError()
            return reply(
                "memory_create",
                subject="user",
                predicate="preferred_database",
                value="PostgreSQL",
                request_id=str(uuid4()),
            )

        async def close(self):
            pass

    monkeypatch.setattr(agent_undo, "OllamaHost", InterruptedHost)
    monkeypatch.setattr(
        agent_undo, "get_settings", lambda: Settings(_env_file=None, test_dsn=_disposable_test_db)
    )
    directory = tmp_path / "evidence"
    with pytest.raises(asyncio.CancelledError):
        await agent_undo.pilot("scripted-test", directory)
    report = json.loads((directory / "results.json").read_text())
    assert report["database_removed"]
    assert report["cases"][0]["turn"]["status"] == "cancelled"
    assert report["cases"][0]["after"]["events"][0]["value"] == "PostgreSQL"
    assert len(report["cases"]) == 6 and report["cases"][1]["status"] == "not_run"
    parts = urlsplit(_disposable_test_db)
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        assert not await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", report["database_name"]
        )
        assert await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", parts.path[1:])
    finally:
        await admin.close()
