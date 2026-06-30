"""Contradiction-gated identity routing in the extraction worker (spec §17)."""

from __future__ import annotations

import json

import pytest

from mnemo.audit import gate_snapshot
from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact
from mnemo.quality import Verdict

ROUTING = Settings(_env_file=None, identity_routing="contradiction")


class Script:
    """Extractor: turn text -> candidates. Verifier: substring entailment, plus
    explicit contradiction (stored value, turn) and restatement (candidate, stored) pairs."""

    def __init__(self, facts, *, contradicts=(), restates=(), importance=5):
        self.facts, self.contradicts, self.restates = facts, set(contradicts), set(restates)
        self.importance = importance
        self.calls: list[tuple[str, str]] = []

    def extract(self, text, role="user"):
        return [
            ExtractedFact(subject="user", predicate=p, object=v, importance=self.importance)
            for p, v in self.facts[text]
        ]

    def verify(self, candidate, source_text):
        value = str(candidate.object)
        self.calls.append((value, source_text))
        if (value, source_text) in self.contradicts:
            return self._verdict("contradiction", False)
        if any(value == c and f" {s}." in source_text for c, s in self.restates):
            return self._verdict("entailment", True)
        entailed = value in source_text
        return self._verdict("entailment" if entailed else "neutral", entailed)

    @staticmethod
    def _verdict(label, accepted):
        return Verdict(accepted=accepted, label=label, probability=1.0, reason="s", backend="t")


async def run_turns(conn, embedder, script, turns, settings=ROUTING, *, same_session=False):
    store = MnemoStore(conn, embedder, settings=settings)
    worker = ExtractionWorker(conn, embedder, script, script, settings=settings)
    for i, text in enumerate(turns):
        await store.observe(f"t{i}", text, "s" if same_session else f"s{i}")
        assert await worker.process_one()
    return store


async def current(store, predicate):
    facts = [f for f in await store.list_current() if f.predicate == predicate]
    return sorted((str(f.value), f.identity_mode) for f in facts)


async def decisions(conn):
    return await conn.fetch(
        "SELECT outcome, reason, score_components, event_id FROM quality_decision "
        "ORDER BY recorded_at, candidate_index"
    )


KIMCHI = "I learned how to make sauerkraut and kimchi."
LASAGNA = "I learned how to make vegan lasagna."
FACTS = {
    KIMCHI: [("learned_to_make", "sauerkraut and kimchi")],
    LASAGNA: [("learned_to_make", "vegan lasagna")],
}


async def test_routing_off_keeps_the_legacy_overwrite(worker_connections, fake_embedder):
    conn, _ = worker_connections
    off = Settings(_env_file=None)
    store = await run_turns(conn, fake_embedder, Script(FACTS), [KIMCHI, LASAGNA], off)
    assert await current(store, "learned_to_make") == [("vegan lasagna", "attribute")]
    assert "identity_routing" not in gate_snapshot(off)
    assert (
        gate_snapshot(off)["fingerprint"] == gate_snapshot(Settings(_env_file=None))["fingerprint"]
    )
    assert gate_snapshot(ROUTING)["fingerprint"] != gate_snapshot(off)["fingerprint"]


async def test_values_that_do_not_contradict_coexist(worker_connections, fake_embedder):
    conn, _ = worker_connections
    store = await run_turns(conn, fake_embedder, Script(FACTS), [KIMCHI, LASAGNA])
    assert await current(store, "learned_to_make") == [
        ("sauerkraut and kimchi", "attribute"),
        ("vegan lasagna", "member"),
    ]
    last = (await decisions(conn))[-1]
    assert last["reason"].endswith("identity=new_member")
    identity = json.loads(last["score_components"])["identity"]
    assert identity["route"] == "new_member"
    assert [c["contradicts"] for c in identity["checks"]] == [False]


async def test_a_contradicting_turn_corrects_the_value_in_place(worker_connections, fake_embedder):
    conn, _ = worker_connections
    first, second = "My budget is 100 dollars.", "My budget is now 150 dollars, not 100."
    script = Script(
        {first: [("budget", "100 dollars")], second: [("budget", "150 dollars")]},
        contradicts={("100 dollars", second)},
    )
    store = await run_turns(conn, fake_embedder, script, [first, second])
    assert await current(store, "budget") == [("150 dollars", "attribute")]
    assert [e.op for e in await store.blame(subject="user", predicate="budget")] == [
        "ADD",
        "UPDATE",
    ]
    assert (await decisions(conn))[-1]["reason"].endswith("identity=correction")


async def test_single_value_predicates_always_replace(worker_connections, fake_embedder):
    conn, _ = worker_connections
    first, second = "I live in Paris.", "I live in Berlin."
    script = Script({first: [("location", "Paris")], second: [("location", "Berlin")]})
    store = await run_turns(conn, fake_embedder, script, [first, second])
    assert await current(store, "location") == [("Berlin", "attribute")]
    # No routing model calls: only the two gate checks ran.
    assert len(script.calls) == 2


async def test_a_vaguer_restatement_keeps_the_specific_value(worker_connections, fake_embedder):
    conn, _ = worker_connections
    first = "I attended a fermentation workshop at a local food co-op."
    second = "I made kimchi from a fermentation workshop I attended."
    specific, vague = "a fermentation workshop at a local food co-op", "a fermentation workshop"
    script = Script(
        {first: [("attended_workshop", specific)], second: [("attended_workshop", vague)]},
        restates={(vague, specific)},
    )
    store = await run_turns(conn, fake_embedder, script, [first, second], same_session=True)
    assert await current(store, "attended_workshop") == [(specific, "attribute")]
    rows = await decisions(conn)
    assert rows[-1]["outcome"] == "duplicate"
    assert rows[-1]["event_id"] == rows[0]["event_id"]
    assert rows[-1]["reason"].endswith("identity=restatement")
    assert await conn.fetchval("SELECT count(*) FROM memory_event") == 1
    assert (
        await conn.fetchval("SELECT reconciled_event_id FROM fast_cache WHERE turn_id='t1'")
        == rows[0]["event_id"]
    )


async def test_contradicting_several_values_is_unresolved(worker_connections, fake_embedder):
    conn, _ = worker_connections
    third = "Actually I never learned any of those dishes; I learned bread."
    script = Script(
        {**FACTS, third: [("learned_to_make", "bread")]},
        contradicts={("sauerkraut and kimchi", third), ("vegan lasagna", third)},
    )
    store = await run_turns(conn, fake_embedder, script, [KIMCHI, LASAGNA, third])
    assert await current(store, "learned_to_make") == [
        ("bread", "member"),
        ("sauerkraut and kimchi", "attribute"),
        ("vegan lasagna", "member"),
    ]
    row = (await decisions(conn))[-1]
    assert row["reason"].endswith("identity=unresolved")
    assert (await store.list_current())[0].tier == "session"
    assert len(json.loads(row["score_components"])["identity"]["contradicted"]) == 2


async def test_repeating_a_member_value_writes_nothing(worker_connections, fake_embedder):
    conn, _ = worker_connections
    store = MnemoStore(conn, fake_embedder, settings=ROUTING)
    again = "As I said, I learned how to make vegan lasagna."
    script = Script({**FACTS, again: [("learned_to_make", "vegan lasagna")]})
    worker = ExtractionWorker(conn, fake_embedder, script, script, settings=ROUTING)
    for turn_id, text in (("a", KIMCHI), ("b", LASAGNA), ("c", again)):
        await store.observe(turn_id, text, "same-session")
        assert await worker.process_one()
    assert await conn.fetchval("SELECT count(*) FROM memory_event") == 2
    assert (await decisions(conn))[-1]["outcome"] == "duplicate"


@pytest.mark.parametrize("routing", ["off", "contradiction"])
async def test_first_value_under_a_key_is_always_the_attribute(
    worker_connections, fake_embedder, routing
):
    conn, _ = worker_connections
    settings = Settings(_env_file=None, identity_routing=routing)
    store = await run_turns(conn, fake_embedder, Script(FACTS), [KIMCHI], settings)
    assert await current(store, "learned_to_make") == [("sauerkraut and kimchi", "attribute")]


async def test_restating_another_sessions_value_writes_it_in_this_session(
    worker_connections, fake_embedder
):
    """v9 bug: a session-tier value from another session is invisible here and
    expires with its session, so a restatement must not be dropped as a duplicate.
    Importance 3 keeps both values in the session tier under the lasting defaults."""
    conn, _ = worker_connections
    first = "I baked a chocolate cake for my sister's birthday party."
    second = "Last weekend I baked a chocolate cake for my sister's birthday."
    specific, restated = "a chocolate cake for my sister's birthday party", "a chocolate cake"
    script = Script(
        {first: [("completed_baking", specific)], second: [("completed_baking", restated)]},
        restates={(restated, specific)},
        importance=3,
    )
    store = await run_turns(conn, fake_embedder, script, [first, second])
    facts = [f for f in await store.list_current() if f.predicate == "completed_baking"]
    assert {(str(f.value), f.tier, f.session_id) for f in facts} == {
        (specific, "session", "s0"),
        (restated, "session", "s1"),
    }
    row = (await decisions(conn))[-1]
    assert row["reason"].endswith("identity=new_member")
    identity = json.loads(row["score_components"])["identity"]
    assert len(identity["restates_other_session"]) == 1
    hits = await store.search("chocolate cake", session_id="s1", reinforce=False)
    assert restated in {str(h.value) for h in hits}


async def test_restating_a_durable_value_from_another_session_is_a_duplicate(
    worker_connections, fake_embedder
):
    """Durable values are visible in every session, so they absorb restatements."""
    conn, _ = worker_connections
    first = "I baked a chocolate cake for my sister's birthday party."
    second = "Last weekend I baked a chocolate cake for my sister's birthday."
    specific, restated = "a chocolate cake for my sister's birthday party", "a chocolate cake"
    script = Script(
        {first: [("completed_baking", specific)], second: [("completed_baking", restated)]},
        restates={(restated, specific)},
        importance=7,
    )
    store = await run_turns(conn, fake_embedder, script, [first, second])
    facts = [f for f in await store.list_current() if f.predicate == "completed_baking"]
    assert [(str(f.value), f.tier) for f in facts] == [(specific, "durable")]
    assert (await decisions(conn))[-1]["reason"].endswith("identity=restatement")
