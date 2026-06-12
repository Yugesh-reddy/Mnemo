"""Measurements must distinguish retrieval misses and unpriced costs."""

import math

import pytest

from mnemo.benchmark import local_compute_cost, summarize_latencies
from mnemo.config import Settings
from mnemo.eval import estimate_cost, retrieval_probes
from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn


def test_pricing_requires_both_rates_and_finite_numbers():
    usage = {"extractor": {"requests": 1, "input_tokens": 1000, "output_tokens": 100}}
    assert estimate_cost(usage, {"extractor": {"input": 2}})["usd"] is None
    assert estimate_cost(usage, {"extractor": {"input": 2, "output": 4}})["usd"] == pytest.approx(
        0.0024
    )
    for rate in (-1, math.nan, math.inf):
        with pytest.raises(ValueError):
            estimate_cost(usage, {"extractor": {"input": rate, "output": 1}})
        with pytest.raises(ValueError):
            local_compute_cost(20, rate)
    assert local_compute_cost(3600, 2)["usd"] == 2
    assert local_compute_cost(3600, None)["usd"] is None


def test_latency_percentiles_use_documented_nearest_rank():
    assert summarize_latencies(list(range(1, 101)))["p95_ms"] == 95
    with pytest.raises(ValueError):
        summarize_latencies([])


async def test_retrieval_probe_distinguishes_missing_fact_from_search_failure(store, db):
    await store.add("user", "location", "Austin")
    before = await db.fetchval("SELECT sum(recall_count) FROM memory_event")
    dataset = EvalDataset(
        "probe",
        "1",
        "dev",
        (
            EvalTurn(
                "known",
                "user",
                "I live in Austin.",
                "s",
                labels=(AtomicLabel("location", "Austin", "must_keep"),),
            ),
            EvalTurn(
                "absent",
                "user",
                "My manager is Dana.",
                "s",
                labels=(AtomicLabel("manager", "Dana", "must_keep"),),
            ),
        ),
    )
    rows = await retrieval_probes(
        db, store.embedder, dataset, namespace=store.namespace, settings=Settings(_env_file=None)
    )
    assert next(r for r in rows if r["turn_id"] == "known")["stage"] == "retrieved"
    assert (
        next(r for r in rows if r["turn_id"] == "absent")["stage"] == "upstream_or_label_mismatch"
    )
    assert await db.fetchval("SELECT sum(recall_count) FROM memory_event") == before
