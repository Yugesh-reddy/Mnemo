"""Eval harness: metrics math + the naive baseline mechanics.

The gated-beats-naive assertion (CLAUDE.md test #4) lands with the gate (C3);
here we prove the harness itself: metrics are correct and naive stores the junk.
"""

from __future__ import annotations

import asyncpg

from mnemo.eval import FORBIDDEN, GROUND_TRUTH, metrics, run_naive


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
    from mnemo.eval import evaluate

    result = await evaluate(_disposable_test_db)
    assert result["gated"]["precision"] > result["naive"]["precision"]
    assert result["gated"]["false"] == 0
    assert result["naive"]["false"] > 0
    assert result["gated"]["recall"] == 1.0
