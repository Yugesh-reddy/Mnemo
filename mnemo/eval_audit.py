"""Source-linked failure attribution for saved evaluation reports; no model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from mnemo.eval import assertion_key
from mnemo.eval_data import load_benchmark_dataset, load_naturalistic_dataset, load_smoke_dataset
from mnemo.eval_support import EvalDataset


def decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def payload(row: dict) -> Any:
    for field in ("object", "object_text", "object_number", "object_json"):
        if row.get(field) is not None:
            return row[field]
    return None


def key(row: dict) -> tuple[str, str]:
    return assertion_key(row["subject"], row["predicate"], payload(row))


def analyze_report(report: dict, dataset: EvalDataset, review: dict | None = None) -> dict:
    if report["metadata"]["fingerprints"]["dataset"] != dataset.fingerprint():
        raise ValueError("report and dataset fingerprints differ")
    turns = {t.turn_id: t for t in dataset.turns}
    decisions = [
        {**d, "candidate": decoded(d["candidate"]), "verification": decoded(d["verification"])}
        for d in report["decisions"]
    ]
    writes = report["gated"]["writes"]
    failed_turns = {e["turn_id"]: e for e in report["gated"].get("turn_errors", [])}
    actual = {key(w) for w in report["gated"]["current_assertions"]}
    unmatched = []
    for w in writes:
        sources = decoded(w["source_span"])["turn_ids"]
        labels = [label for tid in sources for label in turns[tid].labels]
        allowed = {
            assertion_key(label.subject, label.predicate, label.value)
            for label in labels
            if label.disposition in {"truth", "must_keep", "candidate"}
        }
        forbidden = {
            assertion_key(label.subject, label.predicate, label.value)
            for label in labels
            if label.disposition == "forbidden"
        }
        if key(w) not in allowed | forbidden:
            unmatched.append(w["seq"])
    review_by_seq = {item["seq"]: item for item in (review or {}).get("items", [])}
    if review is not None:
        if len(review_by_seq) != len(review["items"]) or set(unmatched) != set(review_by_seq):
            raise ValueError("review must cover each unmatched write exactly once")
        if review["dataset_sha256"] != dataset.fingerprint():
            raise ValueError("review belongs to a different dataset")
        for w in writes:
            if w["seq"] in review_by_seq:
                item = review_by_seq[w["seq"]]
                if (
                    key(w) != key(item["assertion"])
                    or item["source"] != turns[item["turn_id"]].text
                    or item["turn_id"] not in decoded(w["source_span"])["turn_ids"]
                ):
                    raise ValueError("review assertion/source differs from saved evidence")
                allowed_targets = {
                    assertion_key(label.subject, label.predicate, label.value)
                    for label in turns[item["turn_id"]].labels
                    if label.disposition in {"truth", "must_keep"}
                }
                if any(key(t) not in allowed_targets for t in item.get("mapped_targets", [])):
                    raise ValueError("review mapped target is absent from source labels")
    expected, required = {}, set()
    for t in dataset.turns:
        for label in t.labels:
            identity = assertion_key(label.subject, label.predicate, label.value)[0]
            if label.disposition in {"truth", "must_keep"}:
                expected[identity] = (t, label)
            if label.disposition == "must_keep":
                required.add(identity)
    misses = []
    for identity in sorted(required):
        turn, label = expected[identity]
        target = assertion_key(label.subject, label.predicate, label.value)
        if target in actual:
            continue
        ds = [d for d in decisions if d["turn_id"] == turn.turn_id]
        exact = [d for d in ds if d["candidate"] and key(d["candidate"]) == target]
        source_writes = [w for w in writes if turn.turn_id in decoded(w["source_span"])["turn_ids"]]
        last_exact = [w for w in writes if key(w) == target]
        if last_exact:
            later = [
                w for w in writes if w["seq"] > last_exact[-1]["seq"] and key(w)[0] == identity
            ]
            cause = "overwritten_after_correct_write" if later else "visibility_unresolved"
        elif exact:
            later = []
            cause = (
                "verification_rejection"
                if any(d["verification"] and not d["verification"]["accepted"] for d in exact)
                else "gate_or_write_loss"
            )
        elif any(d["candidate"] for d in ds):
            later = []
            cause = "extraction_fidelity_or_label_mismatch"
        elif turn.turn_id in failed_turns:
            later = []
            cause = failed_turns[turn.turn_id]["stage"] + "_error"
        else:
            later = []
            cause = "extraction_omission"
        judgments = [review_by_seq[w["seq"]] for w in source_writes if w["seq"] in review_by_seq]
        # Source proximity does not establish which required assertion a reviewed
        # paraphrase represents. Require an explicit target mapping for attribution.
        target_judgments = [
            j for j in judgments if target in {key(t) for t in j.get("mapped_targets", [])}
        ]
        if any(j["category"] == "supported_equivalent" for j in target_judgments):
            cause = "label_equivalence"
        elif any(j["category"] == "ambiguous_source" for j in target_judgments):
            cause = "ambiguous_source"
        elif any(j["category"] == "supported_lossy" for j in target_judgments):
            cause = "extraction_identity_loss"
        misses.append(
            {
                "turn_id": turn.turn_id,
                "source": turn.text,
                "target": {
                    "subject": label.subject,
                    "predicate": label.predicate,
                    "object": label.value,
                },
                "cause": cause,
                "decisions": ds,
                "source_writes": source_writes,
                "later_same_identity_writes": later,
                "review": judgments,
                "target_review": target_judgments,
                "turn_error": failed_turns.get(turn.turn_id),
            }
        )
    return {
        "dataset": dataset.name,
        "original_metrics": {
            k: v for k, v in report["gated"].items() if k not in {"writes", "current_assertions"}
        },
        "unmatched_count": len(unmatched),
        "reviewed_count": len(review_by_seq),
        "review_categories": dict(Counter(i["category"] for i in review_by_seq.values())),
        "must_keep_targets": len(required),
        "strict_missing_must_keep": len(misses),
        "missing_by_cause": dict(Counter(m["cause"] for m in misses)),
        "misses": misses,
        "limitations": [
            "Source review is not independent human adjudication.",
            (
                "Saved retrieval probes describe their recorded query/session only; "
                "there is no simulated passage of time or TTL-expiry experiment."
                if report["gated"].get("retrieval_probes")
                else "No retrieval probes or TTL simulation exist in the saved baseline; "
                "do not attribute these misses to retrieval latency or expiry."
            ),
            "Original strict metrics are preserved. Source-scoped equivalences do not "
            "rewrite labels or inflate the published score.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--dataset", choices=("benchmark", "smoke", "naturalistic"), default="benchmark"
    )
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    review = json.loads(args.review.read_text()) if args.review else None
    if review and review["report_sha256"] != hashlib.sha256(args.report.read_bytes()).hexdigest():
        raise ValueError("review belongs to a different report")
    dataset = (
        load_naturalistic_dataset()
        if args.dataset == "naturalistic"
        else load_benchmark_dataset("all") if args.dataset == "benchmark" else load_smoke_dataset()
    )
    result = analyze_report(json.loads(args.report.read_text()), dataset, review)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {k: result[k] for k in ("unmatched_count", "review_categories", "missing_by_cause")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
