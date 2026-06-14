"""Compare extracted candidates on frozen development source turns before gating.

Strict candidate coverage is distinct from final-memory recall. Source review must
still distinguish equivalent wording, missing event binding and false assertions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

from mnemo.config import get_settings
from mnemo.eval import assertion_key
from mnemo.eval_audit import decoded
from mnemo.eval_suite import load_cases, policy_fingerprint
from mnemo.extraction import build_extractor


def coverage(candidates: list[dict], labels: list[dict]) -> dict:
    actual = {assertion_key(c["subject"], c["predicate"], c["object"]) for c in candidates}
    eligible = [label for label in labels if label["disposition"] in {"truth", "must_keep"}]
    expected = {
        assertion_key(label["subject"], label["predicate"], label["value"]) for label in eligible
    }
    required = {
        assertion_key(label["subject"], label["predicate"], label["value"])
        for label in eligible
        if label["disposition"] == "must_keep"
    }
    return {
        "strict_target_matches": len(actual & expected),
        "targets": len(expected),
        "strict_must_keep_matches": len(actual & required),
        "must_keep_targets": len(required),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--all-user-turns", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("preserve prior output; choose a new path")
    baseline = json.loads(args.baseline.read_text())
    if baseline.get("split") != "dev" or not baseline.get("complete"):
        raise ValueError("baseline must be a complete development suite")
    settings = get_settings()
    fingerprint = policy_fingerprint(settings)
    report = {
        "split": "dev",
        "complete": False,
        "policy_sha256": fingerprint,
        "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "selection": (
            "all user turns" if args.all_user_turns else "user turns with must-keep labels"
        ),
        "scope": "Strict extraction coverage; not verification accuracy or stored-memory recall.",
        "rows": [],
    }
    extractor = build_extractor(settings)
    try:
        for identity, dataset in load_cases(args.manifest, "dev"):
            old = baseline["cases"][identity]
            if old["metadata"]["fingerprints"]["dataset"] != dataset.fingerprint():
                raise ValueError("baseline source/label fingerprint mismatch")
            for turn in dataset.turns:
                if turn.role != "user" or (
                    not args.all_user_turns
                    and not any(label.disposition == "must_keep" for label in turn.labels)
                ):
                    continue
                previous = [
                    decoded(d["candidate"])
                    for d in old["decisions"]
                    if d["turn_id"] == turn.turn_id and decoded(d["candidate"])
                ]
                started = time.perf_counter()
                try:
                    after = [f.model_dump(mode="json") for f in extractor.extract(turn.text)]
                    error = None
                except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                    after, error = [], str(exc)
                labels = [vars(label) for label in turn.labels]
                report["rows"].append(
                    {
                        "case": identity,
                        "turn_id": turn.turn_id,
                        "source": turn.text,
                        "labels": labels,
                        "before": previous,
                        "after": after,
                        "error": error,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "before_coverage": coverage(previous, labels),
                        "after_coverage": coverage(after, labels),
                    }
                )
                report["usage"] = dict(getattr(extractor, "usage", {}))
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(identity, turn.turn_id, len(after), "candidates", flush=True)
    finally:
        if hasattr(extractor, "close"):
            extractor.close()
    if policy_fingerprint(settings) != fingerprint:
        raise RuntimeError("policy changed; partial report preserved")
    report["totals"] = {
        arm: {
            key: sum(r[arm + "_coverage"][key] for r in report["rows"])
            for key in (
                "strict_target_matches",
                "targets",
                "strict_must_keep_matches",
                "must_keep_targets",
            )
        }
        for arm in ("before", "after")
    }
    report["complete"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["totals"]))


if __name__ == "__main__":
    main()
