"""Replay saved synthetic-development user candidates to isolate verifier changes.

Old reports retain every gated user candidate, but not every assistant extraction.
Only the gated arm is published; its source report contains the original naive arm.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from mnemo.config import get_settings
from mnemo.embedder import build_embedder
from mnemo.eval import _MaterializedExtractor, evaluate
from mnemo.eval_audit import replay_candidates
from mnemo.eval_data import load_benchmark_dataset
from mnemo.eval_suite import policy_fingerprint
from mnemo.quality import build_verifier


async def run(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise ValueError("preserve existing evidence; use a new output path")
    saved = json.loads(args.report.read_text())
    dataset = load_benchmark_dataset("dev")
    if saved["metadata"]["fingerprints"]["dataset"] != dataset.fingerprint():
        raise ValueError("saved source data/labels do not match synthetic development")
    if saved["metadata"]["cleanup_errors"]:
        raise ValueError("source report has unresolved cleanup errors")
    by_turn = {}
    for decision in saved["decisions"]:
        decisions = by_turn.setdefault(decision["turn_id"], [])
        if decision["outcome"] == "error":
            raise ValueError("cannot replay a source report with extraction errors")
        decisions.append(decision)
    extracted = []
    for turn in dataset.turns:
        if turn.turn_id not in by_turn:
            raise ValueError("source report is missing a turn's candidate decisions")
        extracted.append(replay_candidates(by_turn[turn.turn_id]) if turn.role == "user" else [])
    settings = get_settings()
    fingerprint = policy_fingerprint(settings)
    embedder, verifier = build_embedder(settings), build_verifier(settings)
    try:
        report = await evaluate(
            dataset=dataset,
            extractor=_MaterializedExtractor(dataset.turns, extracted),
            embedder=embedder,
            verifier=verifier,
            settings=settings,
            probe_retrieval=True,
        )
    finally:
        for component in (embedder, verifier):
            if hasattr(component, "close"):
                component.close()
    del report["naive"]
    report["metadata"]["saved_candidate_replay"] = {
        "source_report": str(args.report),
        "source_report_sha256": hashlib.sha256(args.report.read_bytes()).hexdigest(),
        "scope": "Gated user candidates only; original naive comparison remains in source report.",
        "original_extractor": saved["metadata"]["model_names"]["extractor"],
        "extraction_repeated": False,
    }
    report["metadata"]["policy_sha256"] = fingerprint
    report["metadata"]["policy_changed_during_run"] = policy_fingerprint(settings) != fingerprint
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    if report["metadata"]["cleanup_errors"] or report["metadata"]["policy_changed_during_run"]:
        raise RuntimeError("replay did not complete cleanly; report retained")
    print(json.dumps({k: report["gated"][k] for k in ("precision", "recall", "must_keep_recall")}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
