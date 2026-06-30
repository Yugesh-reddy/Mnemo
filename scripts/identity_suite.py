"""Run the frozen identity-routing cases through the real worker, gate and verifier.

Candidates are authored (not extracted) so the suite measures routing alone. Each
case runs in its own private schema; a case passes only when the predicate's
current values equal its labeled set exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from mnemo.audit import gate_snapshot
from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore
from mnemo.db import drop_isolated_schema, prepare_isolated_schema
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact


class AuthoredExtractor:
    """Returns each turn's labeled candidate; never calls a model."""

    def __init__(self, turns: list[dict[str, str]]) -> None:
        self.by_text = {turn["text"]: turn for turn in turns}

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        turn = self.by_text[text]
        return [
            ExtractedFact(
                subject="user",
                predicate=turn["predicate"],
                object=turn["value"],
                confidence=0.95,
                importance=8,
            )
        ]


async def run_case(
    case: dict[str, Any],
    *,
    embedder: Any,
    verifier: Any,
    settings: Settings,
    dsn: str,
    identity_judge: Any | None = None,
) -> dict[str, Any]:
    schema = "mnemo_eval_" + uuid4().hex
    conn = await prepare_isolated_schema(dsn, schema, embed_dim=settings.embed_dim)
    try:
        store = MnemoStore(conn, embedder, settings=settings)
        worker = ExtractionWorker(
            conn,
            embedder,
            AuthoredExtractor(case["turns"]),
            verifier,
            settings=settings,
            identity_judge=identity_judge,
        )
        for index, turn in enumerate(case["turns"]):
            await store.observe(f"t{index}", turn["text"], f"s{index}")
            while await worker.process_one():
                pass
        predicate = case["turns"][0]["predicate"]
        current = sorted(
            str(f.value) for f in await store.list_current() if f.predicate == predicate
        )
        rows = await conn.fetch(
            "SELECT turn_id, outcome, reason, score_components FROM quality_decision "
            "ORDER BY recorded_at, candidate_index"
        )
        failed = await conn.fetch(
            "SELECT turn_id, last_error FROM extraction_job WHERE status <> 'done'"
        )
        expected = case["expected_current"]
        gate_rejected = [r["turn_id"] for r in rows if r["outcome"] == "rejected"]
        return {
            "id": case["id"],
            "kind": case["kind"],
            "expected_current": expected,
            "current": current,
            "missing": sorted(set(expected) - set(current)),
            "extra": sorted(set(current) - set(expected)),
            "passed": current == expected and not failed,
            "gate_rejected_turns": gate_rejected,
            "decisions": [
                {
                    "turn_id": r["turn_id"],
                    "outcome": r["outcome"],
                    "reason": r["reason"],
                    "identity": (
                        (json.loads(r["score_components"]) or {}).get("identity")
                        if r["score_components"]
                        else None
                    ),
                }
                for r in rows
            ],
            "unfinished_jobs": [dict(r) for r in failed],
        }
    finally:
        await drop_isolated_schema(conn, schema)
        await conn.close()


async def run_suite(
    cases_path: Path,
    *,
    settings: Settings,
    embedder: Any,
    verifier: Any,
    dsn: str,
    identity_judge: Any | None = None,
) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text())["cases"]
    results = [
        await run_case(
            case,
            embedder=embedder,
            verifier=verifier,
            settings=settings,
            dsn=dsn,
            identity_judge=identity_judge,
        )
        for case in cases
    ]
    return {
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "identity_routing": settings.identity_routing,
        "gate_config": gate_snapshot(settings),
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "false_replacements": sum(len(r["missing"]) for r in results),
        "extra_current_values": sum(len(r["extra"]) for r in results),
        "results": results,
        "verifier_usage": dict(getattr(verifier, "usage", {}) or {}),
        "identity_judge": (
            None
            if identity_judge is None
            else {
                "model": identity_judge.model,
                "usage": identity_judge.usage,
                "stopped": identity_judge.fatal_error,
                "exchanges": identity_judge.exchanges,
            }
        ),
    }


def main() -> None:
    from mnemo.embedder import build_embedder
    from mnemo.quality import build_verifier

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--identity-judge", choices=("verifier", "azure"), default="verifier")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("preserve prior evidence; choose a new output path")
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.replay_extraction_probe import build_identity_judge

    settings = get_settings()
    embedder, verifier = build_embedder(settings), build_verifier(settings)
    judge = build_identity_judge(args.identity_judge, settings)
    try:
        report = asyncio.run(
            run_suite(
                args.cases,
                settings=settings,
                embedder=embedder,
                verifier=verifier,
                dsn=settings.dsn,
                identity_judge=judge,
            )
        )
    finally:
        for component in (embedder, verifier, judge):
            if hasattr(component, "close"):
                component.close()
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({k: report[k] for k in ("identity_routing", "passed", "total")}))


if __name__ == "__main__":
    main()
