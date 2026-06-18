"""Measure the batch parser on frozen dev replies, then gate recovered candidates.

The controlled comparison is final-response parsing, not a second extraction run.
Only newly recovered candidates enter the incremental gate measurement. Original
labels remain unchanged, and missing facts remain in its recall denominator.
"""

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mnemo.config import get_settings
from mnemo.embedder import build_embedder
from mnemo.eval import _ExtractionFailure, _MaterializedExtractor, evaluate
from mnemo.eval_suite import policy_fingerprint
from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn
from mnemo.extraction import OllamaExtractor
from mnemo.quality import build_verifier


async def run(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise ValueError("preserve prior evidence; use a fresh output path")
    saved = json.loads(args.probe.read_text())
    if saved["split"] != "dev" or not saved["complete"]:
        raise ValueError("requires a complete development probe")
    settings = get_settings()
    fingerprint = policy_fingerprint(settings)
    if saved["policy_sha256"] != fingerprint:
        raise ValueError("probe and measurement policies differ")
    report = {
        "split": "dev",
        "complete": False,
        "probe_sha256": hashlib.sha256(args.probe.read_bytes()).hexdigest(),
        "policy_sha256": fingerprint,
        "scope": "Identical final replies; not a full before/after generation comparison.",
        "probe_script_sha256": hashlib.sha256(
            Path("scripts/probe_extraction_dev.py").read_bytes()
        ).hexdigest(),
        "measurement_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parser_rows": [],
        "raw_verifier_responses": [],
    }
    turns, before, after = [], [], []
    parser = OllamaExtractor(max_retries=0)
    try:
        for row in saved["rows"]:
            if row["error"]:
                # A provider failure after a partial reply cannot revive that reply.
                strict, partial, error = [], [], row["error"]
            else:
                raw = row["raw_attempts"][-1]
                parser._chat = lambda *args, _raw=raw: _raw
                try:
                    strict = parser._parse(raw, row["source"])
                    error = None
                except ValueError as exc:
                    strict, error = [], str(exc)
                partial = parser.extract(row["source"])
            if [f.model_dump(mode="json") for f in partial] != row["after"]:
                raise ValueError("final parser survivors differ from frozen probe")
            if getattr(partial, "rejections", []) != row["rejections"]:
                raise ValueError("final parser rejections differ from frozen probe")
            report["parser_rows"].append(
                {
                    "case": row["case"],
                    "turn_id": row["turn_id"],
                    "strict_candidates": len(strict),
                    "partial_candidates": len(partial),
                    "strict_error": error,
                }
            )
            if error and partial:
                turns.append(
                    EvalTurn(
                        row["turn_id"],
                        "user",
                        row["source"],
                        row["turn_id"].rsplit(":", 1)[0],
                        labels=tuple(AtomicLabel(**label) for label in row["labels"]),
                    )
                )
                before.append(_ExtractionFailure(error))
                after.append(partial)
    finally:
        parser.close()
    report["parser_totals"] = {
        "sources": len(report["parser_rows"]),
        "recovered_sources": len(turns),
        "strict_candidates": sum(r["strict_candidates"] for r in report["parser_rows"]),
        "partial_candidates": sum(r["partial_candidates"] for r in report["parser_rows"]),
        "recovered_candidates": sum(len(batch) for batch in after),
        "rejected_members": sum(len(batch.rejections) for batch in after),
    }
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    if not turns:
        report["complete"] = True
        report["gate_measurement"] = "No newly recovered candidates to evaluate."
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        return
    dataset = EvalDataset(
        name="recovered-development-batches", version="1", split="dev", turns=tuple(turns)
    )
    embedder, verifier = build_embedder(settings), build_verifier(settings)
    fallback = getattr(verifier, "fallback", verifier)
    request = fallback._request

    def traced_request(prompt: str) -> Mapping[str, Any]:
        result = request(prompt)
        report["raw_verifier_responses"].append({"prompt": prompt, "response": result})
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        return result

    fallback._request = traced_request
    try:
        for name, batches in (("strict", before), ("partial", after)):
            report[name] = await evaluate(
                dataset=dataset,
                extractor=_MaterializedExtractor(dataset.turns, batches),
                embedder=embedder,
                verifier=verifier,
                settings=settings,
                probe_retrieval=True,
            )
            args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
            print(name, report[name]["gated"]["total_events"], "writes", flush=True)
    finally:
        for component in (embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    if policy_fingerprint(settings) != fingerprint:
        raise RuntimeError("policy changed; incomplete evidence retained")
    if any(report[name]["metadata"]["cleanup_errors"] for name in ("strict", "partial")):
        raise RuntimeError("schema cleanup failed; incomplete evidence retained")
    report["complete"] = True
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(report["parser_totals"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
