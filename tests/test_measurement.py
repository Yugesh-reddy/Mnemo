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


@pytest.mark.parametrize("mode", ["retry_then_success", "mixed", "all_failed"])
@pytest.mark.parametrize("warmup", [0, 1])
async def test_benchmark_keeps_failed_jobs_retries_and_usage(
    mode, warmup, monkeypatch, tmp_path, fake_embedder, _disposable_test_db
):
    import argparse
    import json

    import mnemo.benchmark as benchmark
    from mnemo.quality import HeuristicVerifier

    class IntermittentExtractor:
        def __init__(self):
            self.calls = 0
            self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

        def extract(self, text, role="user"):
            self.calls += 1
            self.usage["requests"] += 1
            self.usage["input_tokens"] += 10
            if mode == "all_failed" or self.calls <= (1 if mode == "retry_then_success" else 2):
                raise ValueError("invalid source evidence")
            return []

    extractor = IntermittentExtractor()
    settings = Settings(
        _env_file=None,
        dsn=_disposable_test_db,
        job_max_attempts=2,
        job_retry_base_seconds=0,
        job_retry_max_seconds=0,
    )
    monkeypatch.setattr(benchmark, "get_settings", lambda: settings)
    monkeypatch.setattr(benchmark, "build_extractor", lambda s: extractor)
    monkeypatch.setattr(benchmark, "build_embedder", lambda s: fake_embedder)
    monkeypatch.setattr(benchmark, "build_verifier", lambda s: HeuristicVerifier())
    output = tmp_path / "benchmark.json"
    result = await benchmark.run(
        argparse.Namespace(
            samples=2, warmup=warmup, output=output, prices=None, local_usd_per_hour=None
        )
    )
    expected_failed = {"retry_then_success": 0, "mixed": 0 if warmup else 1, "all_failed": 2}[mode]
    failed_warmups = int(bool(warmup) and mode != "retry_then_success")
    assert result["complete"] is True
    assert result["failed_samples"] == expected_failed
    assert result["successful_samples"] == 2 - expected_failed
    assert len(result["requests"]) == 2
    assert result["requests"][0]["attempts"] == (1 if warmup and mode != "all_failed" else 2)
    assert result["usage"]["extractor"]["requests"] == (
        4 if mode == "all_failed" else 2 if warmup else 3
    )
    assert result["warmup_usage"]["extractor"]["requests"] == 2 * warmup
    assert len(result["warmup_requests"]) == warmup
    assert result["failed_warmup_samples"] == failed_warmups
    assert (result["successful_latency"] is None) == (mode == "all_failed")
    assert json.loads(output.read_text()) == result
    assert result["status"] == (
        "scored_with_errors" if expected_failed or failed_warmups else "scored"
    )
