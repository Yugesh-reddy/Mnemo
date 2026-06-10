"""Ebbinghaus decay + reinforcement (spec §4 Layer 5, MemoryBank).

R = exp(−λ_eff · t_days / max(S, 1)), λ_eff = λ_base · (1 − (importance/10) · 0.8).
Recall reinforces (S += 1, t → 0). Below the threshold a fact is ARCHIVED — an
appended UPDATE event with tier='ephemeral' — never deleted, always revertible.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from mnemo.core import MnemoStore


def retention(
    *,
    strength: float,
    importance: int | None,
    last_used: datetime,
    now: datetime,
    lambda_base: float,
) -> float:
    imp = (importance if importance is not None else 5) / 10.0
    lam = lambda_base * (1.0 - imp * 0.8)
    t_days = max(0.0, (now - last_used).total_seconds() / 86400.0)
    return math.exp(-lam * t_days / max(strength, 1.0))


async def decay_sweep(store: MnemoStore, *, now: datetime | None = None) -> int:
    """Archive HEAD facts whose retention has faded. Returns the number archived.

    Append-only: archival emits a new UPDATE event (same payload, tier=ephemeral,
    auditable reason) — it never mutates the live event's tier in place.
    """
    now = now or datetime.now(tz=UTC)
    settings = store.settings
    rows = await store.conn.fetch(
        """
        SELECT fact_id, event_id, object_text, object_number, object_json,
               provenance, trust_level, confidence, importance, strength, last_used
        FROM memory_current
        WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
        """,
        store.namespace,
        store.user_id,
        store.agent_id,
    )
    archived = 0
    for r in rows:
        score = retention(
            strength=r["strength"],
            importance=r["importance"],
            last_used=r["last_used"],
            now=now,
            lambda_base=settings.decay_lambda_base,
        )
        if score >= settings.decay_archive_below:
            continue
        event = await store.archive_if_head(
            r["fact_id"],
            r["event_id"],
            actor="decay_sweep",
            expected_last_used=r["last_used"],
            reason=f"archived by decay (reversible); retention={score:.2f}",
        )
        archived += event is not None
    return archived
