"""Controlled serial worker workload with measured latency and explicit pricing."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import drop_isolated_schema, prepare_isolated_schema
from mnemo.embedder import build_embedder
from mnemo.eval import estimate_cost, usage_delta, usage_snapshot
from mnemo.eval_data import load_benchmark_dataset
from mnemo.eval_suite import policy_fingerprint
from mnemo.extraction import ExtractionWorker, build_extractor
from mnemo.quality import build_verifier


def summarize_latencies(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("at least one latency measurement required")
    ordered = sorted(values)
    return {
        "p50_ms": ordered[math.ceil(0.50 * len(ordered)) - 1],
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "mean_ms": sum(values) / len(values),
        "max_ms": max(values),
    }


def local_compute_cost(seconds: float, hourly_rate: float | None) -> dict[str, Any]:
    if hourly_rate is None:
        return {"usd": None, "reason": "No explicit local USD/hour rate supplied."}
    if not math.isfinite(hourly_rate) or hourly_rate < 0:
        raise ValueError("local hourly rate must be finite and non-negative")
    return {
        "usd": seconds * hourly_rate / 3600,
        "usd_per_hour": hourly_rate,
        "reason": "Measured wall-clock allocation at the supplied local hourly rate.",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.samples < 1 or args.warmup < 0:
        raise ValueError("samples must be positive; warmup cannot be negative")
    if args.output.exists():
        raise ValueError("output already exists; choose a new measurement report")
    local_compute_cost(0, args.local_usd_per_hour)
    settings = get_settings()
    extractor, embedder, verifier = (
        build_extractor(settings),
        build_embedder(settings),
        build_verifier(settings),
    )
    schema = "mnemo_eval_" + uuid4().hex
    conn = None
    rows = []
    try:
        conn = await prepare_isolated_schema(settings.dsn, schema, embed_dim=settings.embed_dim)
        source = [t for t in load_benchmark_dataset("dev").turns if t.turn_id.endswith("a")]
        worker = ExtractionWorker(conn, embedder, extractor, verifier, settings=settings)
        before_warmup = usage_snapshot(extractor, embedder, verifier)
        for index in range(-args.warmup, args.samples):
            if index == 0:
                before = usage_snapshot(extractor, embedder, verifier)
                measured_at = datetime.now(UTC).isoformat()
                started = time.perf_counter()
            turn = source[index % len(source)]
            store = MnemoStore(
                conn, embedder, namespace="benchmark-" + str(index), settings=settings
            )
            start = time.perf_counter()
            await store.observe(str(index), turn.text, "benchmark")
            observed = time.perf_counter()
            processed = await worker.process_one()
            completed = time.perf_counter()
            status = await conn.fetchval(
                "SELECT status FROM extraction_job WHERE turn_id=$1", str(index)
            )
            if not processed or status != "done":
                raise RuntimeError(f"benchmark job did not complete: {status}")
            if index >= 0:
                rows.append(
                    {
                        "sample": index,
                        "source_turn": turn.turn_id,
                        "observe_ms": (observed - start) * 1000,
                        "processing_ms": (completed - observed) * 1000,
                        "total_ms": (completed - start) * 1000,
                    }
                )
        elapsed = time.perf_counter() - started
        after = usage_snapshot(extractor, embedder, verifier)
        usage = usage_delta(before, after)
        result = {
            "measured_at": measured_at,
            "policy_sha256": policy_fingerprint(settings),
            "samples": args.samples,
            "warmup_samples": args.warmup,
            "load_model": "closed-loop serial; one outstanding observation and one worker",
            "concurrency": 1,
            "percentile_method": "nearest rank",
            "measurement_seconds": elapsed,
            "throughput_observations_per_second": args.samples / elapsed,
            "latency": summarize_latencies([r["total_ms"] for r in rows]),
            "observe_latency": summarize_latencies([r["observe_ms"] for r in rows]),
            "processing_latency": summarize_latencies([r["processing_ms"] for r in rows]),
            "requests": rows,
            "usage": usage,
            "warmup_usage": usage_delta(before_warmup, before),
            "api_cost": estimate_cost(
                usage, json.loads(args.prices.read_text()) if args.prices else None
            ),
            "local_compute_cost": local_compute_cost(elapsed, args.local_usd_per_hour),
            "models": {
                "extractor": settings.extractor_model,
                "embedder": settings.embed_model,
                "verifier": settings.verifier_model,
                "verifier_backend": settings.verifier_backend,
                "fallback_backend": settings.verifier_fallback_backend,
            },
            "platform": platform.platform(),
            "limitations": [
                "Not a saturation or concurrent-worker capacity benchmark.",
                "Run without other model workloads; OS activity is not isolated.",
                "API and local-compute estimates are separate, not automatically summed.",
            ],
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        try:
            if conn is not None:
                try:
                    await drop_isolated_schema(conn, schema)
                finally:
                    await conn.close()
        finally:
            for component in (extractor, embedder, verifier):
                if hasattr(component, "close"):
                    component.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--local-usd-per-hour", type=float)
    parser.add_argument("--output", type=Path, required=True)
    result = asyncio.run(run(parser.parse_args()))
    print(
        json.dumps(
            {
                "latency": result["latency"],
                "api_cost": result["api_cost"],
                "local_compute_cost": result["local_compute_cost"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
