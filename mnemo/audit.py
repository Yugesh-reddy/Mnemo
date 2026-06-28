"""Append-only quality decisions and scoped operational diagnostics."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import asyncpg

from mnemo.config import Settings


def gate_snapshot(settings: Settings) -> dict[str, Any]:
    """Only non-secret settings that affect a decision belong in its audit record."""
    fields = (
        "backend",
        "extractor_model",
        "embed_model",
        "embed_dim",
        "verifier_backend",
        "verifier_model",
        "verifier_entailment_threshold",
        "verifier_fallback_backend",
        "verifier_fallback_model",
        "verifier_timeout_seconds",
        "verifier_max_retries",
        "extractor_timeout_seconds",
        "extractor_max_retries",
        "confidence_floor",
        "w_imp",
        "w_spec",
        "w_nov",
        "w_src",
        "transient_penalty",
        "durable_cutoff",
        "ephemeral_floor",
        "predicate_vocab",
        "session_ttl_seconds",
        "update_sim",
        "dedup_sim",
    )
    values = {field: getattr(settings, field) for field in fields}
    # Routing is recorded only when enabled, so the default policy's fingerprint
    # stays identical to every earlier evaluation.
    if settings.identity_routing != "off":
        values["identity_routing"] = settings.identity_routing
        values["single_value_predicates"] = settings.single_value_predicates
    if settings.novelty_mode != "cosine":
        values["novelty_mode"] = settings.novelty_mode
    default_markers = type(settings).model_fields["transient_markers"].default
    if settings.transient_markers != default_markers:
        values["transient_markers"] = settings.transient_markers
    values["pipeline_version"] = "2026-09-13-v6.2"
    values["fingerprint"] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return values


async def record_decision(
    conn: asyncpg.Connection,
    *,
    job: Any,
    candidate: dict[str, Any],
    outcome: str,
    reason: str,
    settings: Settings,
    candidate_index: int = 0,
    verification: dict[str, Any] | None = None,
    score_components: dict[str, Any] | None = None,
    source_span: dict[str, Any] | None = None,
    event_id: UUID | None = None,
) -> UUID:
    """Call inside the same transaction as the event and job completion."""
    data = dict(job)
    span = source_span or {"turn_ids": [data["turn_id"]]}
    if data.get("cache_id"):
        span = {**span, "cache_ids": [str(data["cache_id"])]}
    return await conn.fetchval(
        """
        INSERT INTO quality_decision
          (job_id, namespace, user_id, agent_id, session_id, turn_id, candidate_index,
           candidate, outcome, reason, verification, score_components, config_snapshot,
           source_span, event_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11::jsonb,$12::jsonb,
                $13::jsonb,$14::jsonb,$15)
        RETURNING decision_id
        """,
        data.get("job_id"),
        data["namespace"],
        data["user_id"],
        data["agent_id"],
        data.get("session_id"),
        data.get("turn_id"),
        candidate_index,
        json.dumps(candidate),
        outcome,
        reason,
        json.dumps(verification) if verification is not None else None,
        json.dumps(score_components) if score_components is not None else None,
        json.dumps(gate_snapshot(settings)),
        json.dumps(span),
        event_id,
    )


async def decisions(
    conn: asyncpg.Connection,
    *,
    namespace: str,
    user_id: str,
    agent_id: str,
    limit: int = 100,
    turn_id: str | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    rows = await conn.fetch(
        "SELECT * FROM quality_decision WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 "
        "AND ($4::text IS NULL OR turn_id=$4) ORDER BY recorded_at DESC, decision_id LIMIT $5",
        namespace,
        user_id,
        agent_id,
        turn_id,
        limit,
    )
    results = []
    for row in rows:
        data = dict(row)
        for field in (
            "candidate",
            "verification",
            "score_components",
            "config_snapshot",
            "source_span",
        ):
            if isinstance(data[field], str):
                data[field] = json.loads(data[field])
        results.append(data)
    return results


async def queue_health(
    conn: asyncpg.Connection,
    *,
    namespace: str,
    user_id: str,
    agent_id: str,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT status, count(*) AS count,
               max(EXTRACT(EPOCH FROM (clock_timestamp()-created_at))) AS oldest_seconds,
               sum(GREATEST(attempts-1,0)) AS retries
        FROM extraction_job WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
        GROUP BY status
        """,
        namespace,
        user_id,
        agent_id,
    )
    statuses = {row["status"]: int(row["count"]) for row in rows}
    pending_age = max(
        (
            float(row["oldest_seconds"])
            for row in rows
            if row["status"] in ("pending", "processing")
        ),
        default=0.0,
    )
    failures = await conn.fetch(
        "SELECT job_id, turn_id, attempts, last_error, updated_at FROM extraction_job "
        "WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND status='failed' "
        "ORDER BY updated_at DESC LIMIT 20",
        namespace,
        user_id,
        agent_id,
    )
    latency = await conn.fetchrow(
        "SELECT avg(processing_ms) AS mean_ms, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY processing_ms) AS p95_ms "
        "FROM extraction_job WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 "
        "AND completed_at > now()-interval '24 hours'",
        namespace,
        user_id,
        agent_id,
    )
    archived = await conn.fetchval(
        "SELECT count(*) FROM memory_event e JOIN memory_fact f USING (fact_id) "
        "WHERE f.namespace=$1 AND f.user_id=$2 AND f.agent_id=$3 "
        "AND e.actor='decay_sweep' AND e.recorded_at > now()-interval '24 hours'",
        namespace,
        user_id,
        agent_id,
    )
    return {
        "statuses": statuses,
        "processing_latency_24h": dict(latency),
        "archived_24h": archived,
        "oldest_pending_seconds": pending_age,
        "retries": sum(int(row["retries"]) for row in rows),
        "failures": [dict(row) for row in failures],
    }
