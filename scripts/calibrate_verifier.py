"""Evaluate frozen verifier policies on development assertions, never held-out data."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

from mnemo.config import get_settings
from mnemo.eval import assertion_key
from mnemo.eval_data import load_benchmark_dataset
from mnemo.models import ExtractedFact
from mnemo.quality import build_verifier, representation_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-quality", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("output already exists; preserve the saved calibration report")
    spec = importlib.util.spec_from_file_location("baseline_quality", args.baseline_quality)
    baseline_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline_module
    spec.loader.exec_module(baseline_module)
    settings = get_settings()
    baseline = baseline_module.build_verifier(settings)
    current = build_verifier(settings)
    dataset = load_benchmark_dataset("dev")
    turns = {t.turn_id: t for t in dataset.turns}
    cases = {}
    for turn in dataset.turns:
        if turn.role != "user":
            continue
        for label in turn.labels:
            fact = label.candidate()
            # The name ending in "revised" is explicitly unresolved source wording.
            if turn.turn_id == "dev-001d":
                continue
            cases[(turn.turn_id, assertion_key(fact.subject, fact.predicate, fact.object))] = (
                turn,
                fact,
                label.disposition in {"truth", "must_keep", "candidate"},
                "source_label",
            )
    review = json.loads(Path("docs/quality-v2/adjudication.json").read_text())
    for item in review["items"]:
        if item["turn_id"] not in turns or item["category"] == "ambiguous_source":
            continue
        fact = ExtractedFact(**item["assertion"])
        expected = item["category"] in {"supported_equivalent", "supported_lossy", "label_error"}
        cases[(item["turn_id"], assertion_key(fact.subject, fact.predicate, fact.object))] = (
            turns[item["turn_id"]],
            fact,
            expected,
            item["category"],
        )
    rows = []
    try:
        for turn, fact, expected, category in cases.values():
            before = baseline.verify(fact, turn.text).model_dump(mode="json")
            error = representation_error(fact, turn.text)
            after = (
                {
                    "accepted": False,
                    "label": "neutral",
                    "reason": error,
                    "backend": "representation_guard",
                }
                if error
                else current.verify(fact, turn.text).model_dump(mode="json")
            )
            rows.append(
                {
                    "turn_id": turn.turn_id,
                    "source": turn.text,
                    "candidate": fact.model_dump(mode="json"),
                    "expected_accepted": expected,
                    "category": category,
                    "before": before,
                    "after": after,
                }
            )
            args.output.write_text(
                json.dumps({"split": "dev", "complete": False, "rows": rows}, indent=2) + "\n"
            )
    finally:
        baseline.close()
        current.close()
    totals = {}
    for name in ("before", "after"):
        counts = Counter(
            (
                ("tp" if r["expected_accepted"] else "fp")
                if r[name]["accepted"]
                else ("fn" if r["expected_accepted"] else "tn")
            )
            for r in rows
        )
        totals[name] = dict(counts)
    args.output.write_text(
        json.dumps(
            {
                "split": "dev",
                "complete": True,
                "threshold": settings.verifier_entailment_threshold,
                "counts": totals,
                "rows": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
