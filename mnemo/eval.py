"""Reproducible precision/recall evaluation for Mnemo's write-quality gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from typing import Any

import asyncpg
import httpx

from mnemo.audit import gate_snapshot
from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore, canonicalize
from mnemo.db import (
    drop_isolated_schema as _drop_schema,
)
from mnemo.db import (
    prepare_isolated_schema as _prepare_schema,
)
from mnemo.eval_data import (
    load_benchmark_dataset,
    load_longmemeval,
    load_naturalistic_dataset,
    load_smoke_dataset,
)
from mnemo.eval_support import (
    DeterministicEmbedder,
    EvalDataset,
    EvalTurn,
    LabeledReplayExtractor,
    component_fingerprint,
)
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact

logger = logging.getLogger(__name__)

_SMOKE = load_smoke_dataset()
EVAL_CONVERSATION = [(t.turn_id, t.role, t.text) for t in _SMOKE.turns]
GROUND_TRUTH = _SMOKE.truth
FORBIDDEN = _SMOKE.forbidden


class EvalExtractor(LabeledReplayExtractor):
    """Backwards-compatible deterministic extractor for the smoke fixture."""

    def __init__(self) -> None:
        super().__init__(_SMOKE.turns)


def metrics(
    stored: set[tuple[str, str]], truth: set[tuple[str, str]], forbidden: set[tuple[str, str]]
) -> dict[str, int | float]:
    """Compute final-state metrics; the return shape preserves the original API."""
    tp = len(stored & truth)
    precision = tp / len(stored) if stored else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "stored": len(stored),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false": len(stored & forbidden),
    }


@dataclass(frozen=True)
class ArmResult:
    stored: int
    precision: float
    recall: float
    f1: float
    false: int
    must_keep_recall: float
    false_writes: int
    historical_precision: float
    historical_recall: float
    unsupported_writes: int
    visible_facts: int
    total_events: int
    total_rows: int
    storage_bytes: int
    latency_ms: float
    usage: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _stored_pairs(conn: asyncpg.Connection) -> set[tuple[str, str]]:
    rows = await conn.fetch(
        "SELECT predicate, COALESCE(object_text, object_number::text, object_json::text) "
        "AS object_text FROM memory_current"
    )
    return {(r["predicate"], r["object_text"]) for r in rows if r["object_text"] is not None}


async def _historical_pairs(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    rows = await conn.fetch("""
        SELECT f.predicate, e.object_text FROM memory_event e
        JOIN memory_fact f ON f.fact_id=e.fact_id
        WHERE e.op IN ('ADD','UPDATE','REVERT') AND e.object_text IS NOT NULL ORDER BY e.seq
    """)
    return [(r["predicate"], r["object_text"]) for r in rows]


async def run_naive(
    conn: asyncpg.Connection,
    embedder: Any,
    *,
    dataset: EvalDataset | None = None,
    extractor: Any | None = None,
    namespace: str = "eval-naive",
    settings: Settings | None = None,
    turn_errors: list[dict[str, Any]] | None = None,
) -> set[tuple[str, str]]:
    """Store every extracted candidate without filtering or verification."""
    selected = dataset or _SMOKE
    source = extractor or LabeledReplayExtractor(selected.turns)
    store = MnemoStore(conn, embedder, namespace=namespace, settings=settings)
    for turn in selected.turns:
        try:
            candidates = await asyncio.to_thread(source.extract, turn.text, turn.role)
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            if turn_errors is None:
                raise
            turn_errors.append(
                {
                    "turn_id": turn.turn_id,
                    "session_id": turn.session_id,
                    "stage": "extraction",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for candidate in candidates:
            await store.add(
                candidate.subject,
                candidate.predicate,
                candidate.object,
                provenance="direct_user_statement" if turn.role == "user" else "agent_inference",
                confidence=candidate.confidence,
                importance=candidate.importance,
                session_id=turn.session_id,
                source_span={"turn_ids": [turn.turn_id]},
            )
    return await _stored_pairs(conn)


async def run_gated(
    conn: asyncpg.Connection,
    embedder: Any,
    *,
    dataset: EvalDataset | None = None,
    extractor: Any | None = None,
    verifier: Any | None = None,
    namespace: str = "eval-gated",
    settings: Settings | None = None,
    turn_errors: list[dict[str, Any]] | None = None,
) -> set[tuple[str, str]]:
    """Run turns through observe → worker → quality gate."""
    selected = dataset or _SMOKE
    source = extractor or LabeledReplayExtractor(selected.turns)
    observed = _ObservedExtractor(source)
    store = MnemoStore(conn, embedder, namespace=namespace, settings=settings)
    worker = ExtractionWorker(
        conn, embedder, observed, verifier, namespace=namespace, settings=settings
    )
    for turn in selected.turns:
        await store.observe(turn.turn_id, turn.text, turn.session_id, role=turn.role)
    while True:
        if await worker.process_one():
            continue
        remaining = await conn.fetchval(
            "SELECT count(*) FROM extraction_job WHERE status IN ('pending','processing')"
        )
        if not remaining:
            break
        await asyncio.sleep(0.1)
    failed = await conn.fetch(
        "SELECT turn_id, session_id, attempts, last_error FROM extraction_job "
        "WHERE status='failed' ORDER BY created_at"
    )
    if failed and turn_errors is None:
        raise RuntimeError(
            f"evaluation has {len(failed)} failed extraction jobs; no complete score is available"
        )
    if turn_errors is not None:
        turns = {t.turn_id: t for t in selected.turns}
        for row in failed:
            turn = turns[row["turn_id"]]
            message = observed.errors.get((turn.role, turn.text))
            turn_errors.append(
                {
                    "turn_id": turn.turn_id,
                    "session_id": row["session_id"],
                    "stage": "extraction" if message else "worker",
                    "attempts": row["attempts"],
                    "error": message or row["last_error"],
                }
            )
    return await _stored_pairs(conn)


class _ObservedExtractor:
    """Keep evaluation diagnostics while the real worker owns retries and audit events."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.errors: dict[tuple[str, str], str] = {}

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        try:
            result = self.source.extract(text, role)
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            self.errors[(role, text)] = f"{type(exc).__name__}: {exc}"
            raise
        self.errors.pop((role, text), None)
        return result


@dataclass(frozen=True)
class _ExtractionFailure:
    error: str


class _MaterializedExtractor:
    backend = "materialized-candidate-replay"

    def __init__(
        self,
        turns: Iterable[EvalTurn],
        extracted: list[list[ExtractedFact] | _ExtractionFailure],
    ) -> None:
        self._items: dict[tuple[str, str], tuple[ExtractedFact, ...] | _ExtractionFailure] = {}
        for turn, candidates in zip(turns, extracted, strict=True):
            key = (turn.role, turn.text)
            value = candidates if isinstance(candidates, _ExtractionFailure) else tuple(candidates)
            if key in self._items and self._items[key] != value:
                raise ValueError("conflicting candidate replay for identical role/text input")
            self._items[key] = value

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        item = self._items.get((role, text), ())
        if isinstance(item, _ExtractionFailure):
            raise ValueError(item.error)
        return list(item)


def _materialize_candidates(dataset: EvalDataset, extractor: Any) -> _MaterializedExtractor:
    extracted: list[list[ExtractedFact] | _ExtractionFailure] = []
    for turn in dataset.turns:
        try:
            extracted.append(extractor.extract(turn.text, turn.role))
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            extracted.append(_ExtractionFailure(f"{type(exc).__name__}: {exc}"))
    return _MaterializedExtractor(dataset.turns, extracted)


async def assertion_snapshot(conn: asyncpg.Connection) -> dict[str, Any]:
    """Retain reviewable evidence before disposable schemas are removed."""
    return {
        "current_assertions": [
            dict(row)
            for row in await conn.fetch(
                "SELECT subject,predicate,object_text,object_number,object_json "
                "FROM memory_current ORDER BY subject,predicate"
            )
        ],
        "writes": [
            dict(row)
            for row in await conn.fetch(
                "SELECT f.subject,f.predicate,e.op,e.object_text,e.object_number,e.object_json,"
                "e.source_span,e.seq FROM memory_event e JOIN memory_fact f USING(fact_id) "
                "ORDER BY e.seq"
            )
        ],
    }


async def retrieval_probes(
    conn: asyncpg.Connection,
    embedder: Any,
    dataset: EvalDataset,
    *,
    namespace: str,
    settings: Settings,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Separate absent assertions from failed retrieval of stored assertions."""
    latest, required = {}, set()
    for turn in dataset.turns:
        for label in turn.labels:
            key = assertion_key(label.subject, label.predicate, label.value)
            if label.disposition in {"truth", "must_keep"}:
                latest[key[0]] = (turn, label)
            if label.disposition == "must_keep":
                required.add(key[0])
    current = (await assertion_snapshot(conn))["current_assertions"]
    present = {
        assertion_key(
            r["subject"],
            r["predicate"],
            (
                r["object_text"]
                if r["object_text"] is not None
                else r["object_number"] if r["object_number"] is not None else r["object_json"]
            ),
        )
        for r in current
    }
    store = MnemoStore(conn, embedder, namespace=namespace, settings=settings)
    results = []
    for identity in sorted(required):
        turn, label = latest[identity]
        target = assertion_key(label.subject, label.predicate, label.value)
        query = f"{label.subject} {label.predicate} {label.value}"
        started = time.perf_counter()
        found = await store.search(query, k=k, session_id=turn.session_id, reinforce=False)
        hit = any(assertion_key(f.subject, f.predicate, f.value) == target for f in found)
        results.append(
            {
                "turn_id": turn.turn_id,
                "query": query,
                "k": k,
                "session_id": turn.session_id,
                "in_current": target in present,
                "retrieved": hit,
                "stage": (
                    "retrieved"
                    if hit
                    else (
                        "retrieval_or_session_filter"
                        if target in present
                        else "upstream_or_label_mismatch"
                    )
                ),
                "latency_ms": (time.perf_counter() - started) * 1000,
                "results": [
                    {
                        "subject": f.subject,
                        "predicate": f.predicate,
                        "object": f.value,
                        "tier": f.tier,
                        "source": f.source,
                    }
                    for f in found
                ],
            }
        )
    return results


def _telemetry(component: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    usage, cost = getattr(component, "usage", None), getattr(component, "cost", None)
    return (
        dict(usage) if isinstance(usage, dict) else None,
        dict(cost) if isinstance(cost, dict) else None,
    )


async def _arm_result(
    conn: asyncpg.Connection,
    stored: set[tuple[str, str]],
    dataset: EvalDataset,
    elapsed: float,
    telemetry_source: Any,
) -> ArmResult:
    current = await conn.fetch(
        "SELECT subject,predicate,object_text,object_number,object_json FROM memory_current"
    )
    actual = set()
    for row in current:
        value = (
            row["object_text"]
            if row["object_text"] is not None
            else (row["object_number"] if row["object_number"] is not None else row["object_json"])
        )
        actual.add(assertion_key(row["subject"], row["predicate"], value))
    expected, must_keep_keys, forbidden = {}, set(), set()
    for turn in dataset.turns:
        for label in turn.labels:
            key = assertion_key(label.subject, label.predicate, label.value)
            if label.disposition in {"truth", "must_keep"}:
                expected[key[0]] = key
            if label.disposition == "must_keep":
                must_keep_keys.add(key[0])
            if label.disposition == "forbidden":
                forbidden.add(key)
    truth = set(expected.values())
    must_keep = {key for identity, key in expected.items() if identity in must_keep_keys}
    base = metrics(actual, truth, forbidden - truth)
    counts = await conn.fetchrow("""
        SELECT (SELECT count(*) FROM memory_current) visible,
               (SELECT count(*) FROM memory_event) events,
               (SELECT count(*) FROM memory_fact)+(SELECT count(*) FROM memory_event)+
               (SELECT count(*) FROM fast_cache)+(SELECT count(*) FROM extraction_job)+
               (SELECT count(*) FROM quality_decision) rows
    """)
    storage = await conn.fetchval("""
        SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0)::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','m')
    """)
    usage, cost = _telemetry(telemetry_source)
    historical = await historical_metrics(conn, dataset)
    return ArmResult(
        **base,
        must_keep_recall=len(actual & must_keep) / len(must_keep) if must_keep else 1.0,
        false_writes=historical["false_writes"],
        historical_precision=historical["precision"],
        historical_recall=historical["recall"],
        unsupported_writes=historical["unsupported_writes"],
        visible_facts=int(counts["visible"]),
        total_events=int(counts["events"]),
        total_rows=int(counts["rows"]),
        storage_bytes=int(storage),
        latency_ms=elapsed * 1000,
        usage=usage,
        cost=cost,
    )


def assertion_key(subject: str, predicate: str, value: Any) -> tuple[str, str]:
    value = str(value).strip().casefold()
    if value in {"postgres", "postgresql", "psql"}:
        value = "postgresql"
    return canonicalize(subject, predicate), value


async def historical_metrics(
    conn: asyncpg.Connection, dataset: EvalDataset, *, restrict: bool = False
) -> dict[str, Any]:
    labels = {t.turn_id: t for t in dataset.turns}
    required = {
        assertion_key(label.subject, label.predicate, label.value)
        for t in dataset.turns
        for label in t.labels
        if label.disposition in {"truth", "must_keep"}
    }
    observed, correct, false, unsupported, evaluated = set(), 0, 0, 0, 0
    rows = await conn.fetch(
        "SELECT f.subject,f.predicate,e.object_text,e.object_number,e.object_json,e.source_span "
        "FROM memory_event e JOIN memory_fact f USING(fact_id) "
        "WHERE e.op IN ('ADD','UPDATE','REVERT') ORDER BY e.seq"
    )
    for row in rows:
        span = (
            json.loads(row["source_span"])
            if isinstance(row["source_span"], str)
            else row["source_span"]
        )
        value = (
            row["object_text"]
            if row["object_text"] is not None
            else (row["object_number"] if row["object_number"] is not None else row["object_json"])
        )
        key = assertion_key(row["subject"], row["predicate"], value)
        sources = [labels[t] for t in (span or {}).get("turn_ids", []) if t in labels]
        if restrict and not sources:
            continue
        evaluated += 1
        allowed = {
            assertion_key(label.subject, label.predicate, label.value)
            for t in sources
            for label in t.labels
            if t.role != "assistant" and label.disposition in {"truth", "must_keep", "candidate"}
        }
        forbidden = {
            assertion_key(label.subject, label.predicate, label.value)
            for t in sources
            for label in t.labels
            if label.disposition == "forbidden" or t.role == "assistant"
        }
        if key in allowed and key not in forbidden:
            observed.add(key)
            correct += 1
        elif key in forbidden or any(t.role == "assistant" for t in sources):
            false += 1
        else:
            unsupported += 1  # unmatched atomic labels; not an adjudicated hallucination
    return {
        "precision": correct / evaluated if evaluated else 0.0,
        "recall": len(observed & required) / len(required) if required else 1.0,
        "false_writes": false,
        "unsupported_writes": unsupported,
    }


def usage_snapshot(*components: Any) -> dict[str, Any]:
    result = {}
    for name, component in zip(("extractor", "embedder", "verifier"), components, strict=True):
        result[name] = dict(getattr(component, "usage", {}) or {})
    return result


def usage_delta(before: dict, after: dict) -> dict:
    return {
        name: {key: value - before.get(name, {}).get(key, 0) for key, value in fields.items()}
        for name, fields in after.items()
    }


def estimate_cost(usage: dict[str, Any], prices: dict[str, Any] | None) -> dict[str, Any]:
    """Prices are explicit per-component USD per million input/output tokens."""
    if prices is None:
        return {
            "usd": None,
            "reason": "No pricing supplied; measured usage is reported separately.",
        }
    total = 0.0
    for name, counters in usage.items():
        if not counters or counters.get("requests", 0) == 0:
            continue
        rates = prices.get(name)
        if rates is None or counters.get("unmetered_requests", 0):
            return {"usd": None, "reason": f"Missing pricing or token usage for {name}"}
        for side in ("input", "output"):
            if side not in rates:
                return {"usd": None, "reason": f"Missing explicit {side} price for {name}"}
            rate = float(rates[side])
            if rate < 0 or not math.isfinite(rate):
                raise ValueError("token prices must be finite and non-negative")
            total += counters.get(side + "_tokens", 0) * rate / 1_000_000
    return {
        "usd": total,
        "reason": "Estimated API cost from measured tokens and supplied prices; "
        "excludes local compute.",
    }


async def evaluate(
    dsn: str | None = None,
    *,
    dataset: EvalDataset | None = None,
    extractor: Any | None = None,
    verifier: Any | None = None,
    embedder: Any | None = None,
    mode: str = "candidate_replay",
    settings: Settings | None = None,
    prices: dict[str, Any] | None = None,
    probe_retrieval: bool = False,
) -> dict[str, Any]:
    """Run isolated comparison arms; never truncate or reuse application tables."""
    if mode not in {"candidate_replay", "pipeline"}:
        raise ValueError("mode must be 'candidate_replay' or 'pipeline'")
    settings = settings or get_settings()
    selected = dataset or load_smoke_dataset()
    selected_embedder = embedder or DeterministicEmbedder(settings.embed_dim)
    original_extractor = extractor or LabeledReplayExtractor(selected.turns)
    before_materialize = usage_snapshot(original_extractor, selected_embedder, verifier)
    materialize_started = time.perf_counter()
    selected_extractor = (
        await asyncio.to_thread(_materialize_candidates, selected, original_extractor)
        if mode == "candidate_replay"
        else original_extractor
    )
    materialize_ms = (time.perf_counter() - materialize_started) * 1000
    candidate_usage = usage_delta(
        before_materialize, usage_snapshot(original_extractor, selected_embedder, verifier)
    )["extractor"]
    target_dsn = dsn or settings.dsn
    schemas = [f"mnemo_eval_{uuid.uuid4().hex}", f"mnemo_eval_{uuid.uuid4().hex}"]
    connections: list[asyncpg.Connection | None] = [None, None]
    result: dict[str, Any] | None = None
    cleanup_errors: list[dict[str, str]] = []
    naive_errors: list[dict[str, Any]] = []
    gated_errors: list[dict[str, Any]] = []
    try:
        connections[0] = await _prepare_schema(target_dsn, schemas[0], embed_dim=settings.embed_dim)
        connections[1] = await _prepare_schema(target_dsn, schemas[1], embed_dim=settings.embed_dim)
        before_naive = usage_snapshot(original_extractor, selected_embedder, verifier)
        started = time.perf_counter()
        naive_pairs = await run_naive(
            connections[0],
            selected_embedder,
            dataset=selected,
            extractor=selected_extractor,
            settings=settings,
            turn_errors=naive_errors,
        )
        naive_elapsed = time.perf_counter() - started
        after_naive = usage_snapshot(original_extractor, selected_embedder, verifier)
        started = time.perf_counter()
        gated_pairs = await run_gated(
            connections[1],
            selected_embedder,
            dataset=selected,
            extractor=selected_extractor,
            verifier=verifier,
            settings=settings,
            turn_errors=gated_errors,
        )
        gated_elapsed = time.perf_counter() - started
        after_gated = usage_snapshot(original_extractor, selected_embedder, verifier)
        naive = await _arm_result(
            connections[0], naive_pairs, selected, naive_elapsed, selected_extractor
        )
        gated = await _arm_result(
            connections[1], gated_pairs, selected, gated_elapsed, selected_extractor
        )
        config_hash = gate_snapshot(settings)["fingerprint"]
        naive_data, gated_data = naive.as_dict(), gated.as_dict()
        naive_data["turn_errors"] = naive_errors
        gated_data["turn_errors"] = gated_errors
        naive_data["usage"] = usage_delta(before_naive, after_naive)
        gated_data["usage"] = usage_delta(after_naive, after_gated)
        for arm in (naive_data, gated_data):
            arm["cost"] = estimate_cost(arm["usage"], prices)
        for conn, data in ((connections[0], naive_data), (connections[1], gated_data)):
            split_results = {}
            for split in ("dev", "held_out"):
                turns = tuple(
                    turn for turn in selected.turns if turn.turn_id.startswith(split + "-")
                )
                if turns:
                    split_results[split] = await historical_metrics(
                        conn, replace(selected, turns=turns), restrict=True
                    )
            data["historical_by_split"] = split_results
            data.update(await assertion_snapshot(conn))
        if probe_retrieval:
            for conn, data, namespace in (
                (connections[0], naive_data, "eval-naive"),
                (connections[1], gated_data, "eval-gated"),
            ):
                before = usage_snapshot(original_extractor, selected_embedder, verifier)
                data["retrieval_probes"] = await retrieval_probes(
                    conn, selected_embedder, selected, namespace=namespace, settings=settings
                )
                data["retrieval_probe_usage"] = usage_delta(
                    before, usage_snapshot(original_extractor, selected_embedder, verifier)
                )
        result = {
            "naive": naive_data,
            "gated": gated_data,
            "decisions": [
                dict(r)
                for r in await connections[1].fetch(
                    "SELECT turn_id, candidate, outcome, reason, verification, score_components "
                    "FROM quality_decision ORDER BY recorded_at, candidate_index"
                )
            ],
            "metadata": {
                "status": "scored_with_errors" if naive_errors or gated_errors else "scored",
                "dataset": selected.name,
                "dataset_version": selected.version,
                "split": selected.split,
                "turns": len(selected.turns),
                "synthetic": bool(selected.metadata.get("synthetic", False)),
                "mode": mode,
                "candidate_preparation_ms": materialize_ms,
                "shared_candidate_usage": (candidate_usage if mode == "candidate_replay" else None),
                "shared_candidate_cost": (
                    estimate_cost({"extractor": candidate_usage}, prices)
                    if mode == "candidate_replay"
                    else None
                ),
                "model_names": {
                    "extractor": getattr(
                        original_extractor, "model", type(original_extractor).__name__
                    ),
                    "embedder": getattr(
                        selected_embedder, "model", type(selected_embedder).__name__
                    ),
                    "verifier": getattr(verifier, "model", type(verifier).__name__),
                },
                "gate_config": gate_snapshot(settings),
                "fingerprints": {
                    "dataset": selected.fingerprint(),
                    "extractor": component_fingerprint(original_extractor),
                    "verifier": (
                        component_fingerprint(verifier)
                        if verifier is not None
                        else "default-worker-verifier"
                    ),
                    "embedder": component_fingerprint(selected_embedder),
                    "config": config_hash,
                },
            },
        }
    finally:
        for conn, schema in zip(connections, schemas, strict=True):
            if conn is not None:
                try:
                    await _drop_schema(conn, schema)
                except Exception as exc:
                    cleanup_errors.append({"schema": schema, "error": type(exc).__name__})
                    logger.warning(
                        "Evaluation cleanup failed for %s: %s", schema, type(exc).__name__
                    )
                finally:
                    await conn.close()
    assert result is not None
    result["metadata"]["cleanup_errors"] = cleanup_errors
    return result


def report(result: dict[str, Any]) -> None:
    meta = result.get("metadata", {})
    label = (
        "synthetic" if meta.get("synthetic", True) else "external conversations; supplied labels"
    )
    print(f"PRECISION / RECALL WRITE-QUALITY EVAL ({meta.get('dataset', 'smoke')}; {label})")
    if meta.get("status") == "scored_with_errors":
        print("  FAILED TURNS PRESENT: their labels remain in the recall denominator.")
    for name in ("naive", "gated"):
        row = result[name]
        print(
            f"  {name.upper():<6}: visible {row['visible_facts']:3d} | "
            f"P {row['precision'] * 100:5.1f}% | R {row['recall'] * 100:5.1f}% | "
            f"F1 {row['f1'] * 100:5.1f}% | "
            f"must-keep {row['must_keep_recall'] * 100:5.1f}% | "
            f"false now/history {row['false']}/{row['false_writes']} | "
            f"events {row['total_events']} | {row['latency_ms']:.1f} ms"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", choices=("smoke", "benchmark", "naturalistic"), default="smoke"
    )
    parser.add_argument("--split", choices=("dev", "held_out", "all"), default="held_out")
    parser.add_argument(
        "--mode", choices=("candidate_replay", "pipeline"), default="candidate_replay"
    )
    parser.add_argument(
        "--real", action="store_true", help="Use configured extraction, embeddings and verifier"
    )
    parser.add_argument("--verifier", choices=("heuristic", "ollama", "openai", "cross_encoder"))
    parser.add_argument("--longmemeval", help="Path to source LongMemEval records")
    parser.add_argument("--labels", help="Path to explicit atomic labels keyed by session:turn")
    parser.add_argument(
        "--limit", type=int, help="Limit turns for a clearly labeled model smoke run"
    )
    parser.add_argument(
        "--prices", help="JSON file of per-component USD per million input/output tokens"
    )
    parser.add_argument("--output", help="Write the complete JSON report")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--probe-retrieval", action="store_true")
    args = parser.parse_args()
    if args.dataset == "naturalistic":
        if args.split == "held_out":
            parser.error("naturalistic is authored development data, not a holdout")
        dataset = load_naturalistic_dataset()
    else:
        dataset = (
            load_smoke_dataset() if args.dataset == "smoke" else load_benchmark_dataset(args.split)
        )
    from pathlib import Path

    from mnemo.embedder import build_embedder
    from mnemo.extraction import build_extractor
    from mnemo.quality import build_verifier

    if args.longmemeval:
        if not args.labels:
            parser.error("--longmemeval requires --labels")
        dataset = load_longmemeval(
            args.longmemeval, labels=json.loads(Path(args.labels).read_text())
        )
    if args.limit:
        if args.limit < 1:
            parser.error("--limit must be positive")
        dataset = replace(
            dataset, turns=dataset.turns[: args.limit], split=dataset.split + "-partial"
        )
    settings = get_settings()
    if args.verifier:
        settings = settings.model_copy(update={"verifier_backend": args.verifier})
    extractor = build_extractor(settings) if args.real else None
    embedder = build_embedder(settings) if args.real else None
    verifier = build_verifier(settings)
    try:
        result = asyncio.run(
            evaluate(
                dataset=dataset,
                mode=args.mode,
                extractor=extractor,
                embedder=embedder,
                verifier=verifier,
                settings=settings,
                prices=json.loads(Path(args.prices).read_text()) if args.prices else None,
                probe_retrieval=args.probe_retrieval,
            )
        )
    finally:
        for component in (extractor, embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
        )
    (
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        if args.json
        else report(result)
    )
    if result["metadata"].get("cleanup_errors"):
        raise SystemExit(
            "Evaluation scored; cleanup failed. See metadata.cleanup_errors in the report."
        )
    if result["metadata"].get("status") == "scored_with_errors":
        raise SystemExit("Evaluation scored with failed turns; see each arm's turn_errors.")


if __name__ == "__main__":
    main()


__all__ = [
    "DeterministicEmbedder",
    "EVAL_CONVERSATION",
    "EvalDataset",
    "EvalExtractor",
    "FORBIDDEN",
    "GROUND_TRUTH",
    "evaluate",
    "load_benchmark_dataset",
    "load_longmemeval",
    "load_smoke_dataset",
    "main",
    "metrics",
    "report",
    "run_gated",
    "run_naive",
]
