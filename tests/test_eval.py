"""Eval harness: metrics math + the naive baseline mechanics.

The gated-beats-naive assertion (CLAUDE.md test #4) lands with the gate (C3);
here we prove the harness itself: metrics are correct and naive stores the junk.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg
import pytest

from mnemo.eval import FORBIDDEN, GROUND_TRUTH, evaluate, metrics, run_naive
from mnemo.eval_data import load_benchmark_dataset, load_longmemeval
from mnemo.eval_support import DeterministicEmbedder


def test_metrics_math() -> None:
    truth = {("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")}
    forbidden = {("z", "9")}
    stored = {("a", "1"), ("b", "2"), ("z", "9")}  # 2 right, 1 forbidden
    m = metrics(stored, truth, forbidden)
    assert m["stored"] == 3
    assert abs(m["precision"] - 2 / 3) < 1e-9
    assert abs(m["recall"] - 2 / 4) < 1e-9
    assert m["false"] == 1
    assert 0 < m["f1"] < 1


def test_metrics_empty_store() -> None:
    m = metrics(set(), {("a", "1")}, set())
    assert m == {"stored": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0, "false": 0}


async def test_naive_baseline_stores_junk_and_false_facts(
    db: asyncpg.Connection, fake_embedder
) -> None:
    stored = await run_naive(db, fake_embedder)
    m = metrics(stored, GROUND_TRUTH, FORBIDDEN)
    # The naive path must exhibit the disease: junk stored, forbidden facts asserted.
    assert m["false"] > 0
    assert m["precision"] < 1.0
    # ...but it does capture the must-keep facts (recall is not the naive problem).
    assert m["recall"] == 1.0


async def test_gated_beats_naive_the_north_star(_disposable_test_db: str, clean_memory) -> None:
    """CLAUDE.md test #4: gated precision > naive; gated false-count = 0 while
    naive > 0; recall of must-keep facts stays 100%."""
    result = await evaluate(_disposable_test_db)
    assert result["gated"]["precision"] > result["naive"]["precision"]
    assert result["gated"]["false"] == 0
    assert result["naive"]["false"] > 0
    assert result["gated"]["recall"] == 1.0


def test_versioned_benchmark_has_real_splits_and_distinct_slices() -> None:
    dev = load_benchmark_dataset("dev")
    held_out = load_benchmark_dataset("held_out")
    combined = load_benchmark_dataset("all")

    assert len(dev.turns) == 100
    assert len(held_out.turns) == 100
    assert len(combined.turns) == 200
    assert dev.version == held_out.version == "1.0.0"
    assert {turn.turn_id for turn in dev.turns}.isdisjoint(turn.turn_id for turn in held_out.turns)
    assert len({turn.text for turn in combined.turns}) == 200
    assert combined.must_keep
    assert combined.forbidden
    assert all(turn.timestamp and turn.session_id and turn.source_id for turn in combined.turns)


def test_longmemeval_adapter_requires_atomic_labels_and_preserves_context() -> None:
    records = [
        {
            "haystack_session_ids": ["session-42"],
            "haystack_dates": ["2025-04-03T12:30:00Z"],
            "haystack_sessions": [
                [[{"role": "user", "content": "I live in Oslo.", "turn_id": "turn-9"}][0]]
            ],
        }
    ]
    with pytest.raises(ValueError, match="explicit atomic labels"):
        load_longmemeval(records, labels={})

    dataset = load_longmemeval(
        records,
        labels={"turn-9": [{"predicate": "location", "value": "Oslo", "disposition": "must_keep"}]},
    )
    turn = dataset.turns[0]
    assert turn.role == "user"
    assert turn.session_id == "session-42"
    assert turn.source_id == turn.turn_id == "turn-9"
    assert turn.timestamp == datetime.fromisoformat("2025-04-03T12:30:00+00:00")


async def test_evaluate_uses_private_schemas_and_tracks_false_write_history(
    _disposable_test_db: str,
) -> None:
    before = await asyncpg.connect(_disposable_test_db)
    try:
        schemas_before = set(
            await before.fetchval(
                "SELECT array_agg(nspname ORDER BY nspname) FROM pg_namespace "
                "WHERE nspname LIKE 'mnemo_eval_%'"
            )
            or []
        )
    finally:
        await before.close()

    result = await evaluate(
        _disposable_test_db,
        embedder=DeterministicEmbedder(768),
    )
    assert result["metadata"]["synthetic"] is True
    assert result["naive"]["false_writes"] > result["naive"]["false"]
    assert result["gated"]["false_writes"] == 0
    assert result["gated"]["usage"] == {"extractor": {}, "embedder": {}, "verifier": {}}
    assert result["gated"]["cost"]["usd"] is None

    after = await asyncpg.connect(_disposable_test_db)
    try:
        schemas_after = set(
            await after.fetchval(
                "SELECT array_agg(nspname ORDER BY nspname) FROM pg_namespace "
                "WHERE nspname LIKE 'mnemo_eval_%'"
            )
            or []
        )
    finally:
        await after.close()
    assert schemas_after == schemas_before


async def test_false_write_stays_counted_after_later_true_correction(_disposable_test_db):
    from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn

    dataset = EvalDataset(
        name="history-regression",
        version="1",
        split="test",
        turns=(
            EvalTurn(
                "bad",
                "user",
                "I don't live in Austin.",
                "s",
                labels=(AtomicLabel("location", "Austin", "forbidden"),),
            ),
            EvalTurn(
                "good",
                "user",
                "I live in Portland.",
                "s",
                labels=(AtomicLabel("location", "Portland", "must_keep"),),
            ),
        ),
    )
    result = await evaluate(_disposable_test_db, dataset=dataset)
    assert result["naive"]["false"] == 0
    assert result["naive"]["false_writes"] == 1
    assert result["naive"]["historical_precision"] == 0.5
    assert result["gated"]["false_writes"] == 0


async def test_final_metrics_do_not_credit_the_wrong_subject(_disposable_test_db):
    from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn
    from mnemo.models import ExtractedFact

    dataset = EvalDataset(
        name="subject-regression",
        version="1",
        split="test",
        turns=(
            EvalTurn(
                "t",
                "user",
                "I live in Austin.",
                "s",
                labels=(AtomicLabel("location", "Austin", "must_keep"),),
            ),
        ),
    )

    class WrongSubject:
        def extract(self, text, role="user"):
            return [ExtractedFact(subject="Priya", predicate="location", object="Austin")]

    result = await evaluate(_disposable_test_db, dataset=dataset, extractor=WrongSubject())
    assert result["naive"]["recall"] == 0
    assert result["naive"]["precision"] == 0
    assert result["naive"]["unsupported_writes"] == 1


def test_cost_requires_metered_usage_and_explicit_prices():
    from mnemo.eval import estimate_cost

    usage = {"extractor": {"requests": 1, "input_tokens": 1000, "output_tokens": 250}}
    assert estimate_cost(usage, None)["usd"] is None
    assert estimate_cost(usage, {"extractor": {"input": 2, "output": 8}})["usd"] == 0.004
    usage["extractor"]["unmetered_requests"] = 1
    assert estimate_cost(usage, {"extractor": {"input": 2, "output": 8}})["usd"] is None


async def test_cleanup_failure_preserves_scores_and_closes_both_connections(
    _disposable_test_db, monkeypatch
):
    import mnemo.eval as harness

    original = harness._drop_schema
    connections = []
    schemas = []

    async def fail_drop(conn, schema):
        connections.append(conn)
        schemas.append(schema)
        raise asyncpg.LockNotAvailableError("injected cleanup conflict")

    monkeypatch.setattr(harness, "_drop_schema", fail_drop)
    try:
        result = await harness.evaluate(_disposable_test_db)
        assert result["gated"]["recall"] == 1
        assert result["gated"]["writes"]
        assert len(result["metadata"]["cleanup_errors"]) == 2
        assert len(connections) == 2 and all(conn.is_closed() for conn in connections)
    finally:
        conn = await asyncpg.connect(_disposable_test_db)
        try:
            for schema in schemas:
                await original(conn, schema)
        finally:
            await conn.close()
