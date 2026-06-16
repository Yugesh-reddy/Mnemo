"""Source-bound, provisional equivalence review; never changes strict labels."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from mnemo.eval_audit import decoded, payload
from mnemo.eval_normalization import assertion_key


def complete_credit(entry: dict, available: set[str], support: dict[str, str]) -> bool:
    refs = entry["candidate_refs"]
    return (
        bool(refs)
        and entry["coverage"] == "complete"
        and all(ref in available and support.get(ref) == "supported" for ref in refs)
    )


def coverage_counts(entries: list[dict], available: set[str], support: dict[str, str]) -> dict:
    covered = [e for e in entries if complete_credit(e, available, support)]
    return {
        "complete_occurrences": len(covered),
        "occurrences": len(entries),
        "complete_unique_targets": len({e["logical_target_id"] for e in covered}),
        "unique_targets": len({e["logical_target_id"] for e in entries}),
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score(probe_path: Path, review_path: Path, support_path: Path, replay_path: Path) -> dict:
    probe, review, judgments, replay = [
        json.loads(p.read_text()) for p in (probe_path, review_path, support_path, replay_path)
    ]
    digest = _sha(probe_path)
    if any(
        value != digest
        for value in (
            review["probe_sha256"],
            judgments["input_sha256"],
            replay["probe_sha256"],
        )
    ):
        raise ValueError("review/replay belongs to a different candidate snapshot")
    if not replay["complete"] or replay["split"] != "dev":
        raise ValueError("requires a complete development replay")
    candidates, support, by_turn, raw_rejections = {}, {}, {}, {}
    for index, row in enumerate(probe["rows"]):
        judged = judgments["rows"][str(index)]
        if judged["source"] != row["source"] or judged["turn_id"] != row["turn_id"]:
            raise ValueError("review source mismatch")
        if len(judged["candidates"]) != len(row["after"]):
            raise ValueError("every candidate needs a source review")
        by_turn[(row["case"], row["turn_id"])] = row
        for j, rejected in enumerate(row.get("rejections", []), start=len(row["after"])):
            ref = f"{row['case']}/{row['turn_id']}/candidate{j}"
            raw_rejections[ref] = rejected
        for j, candidate in enumerate(row["after"]):
            ref = f"{row['case']}/{row['turn_id']}/candidate{j}"
            judgment = judged["candidates"][j]
            if judgment["candidate_index"] != j or judgment["assertion"] != {
                k: candidate[k] for k in ("subject", "predicate", "object")
            }:
                raise ValueError("candidate review assertion mismatch")
            candidates[ref], support[ref] = candidate, judgment["source_support"]
            if support[ref] not in {"supported", "unsupported", "ambiguous"}:
                raise ValueError("unknown source-support judgment")
    entries = review["entries"]
    expected = {
        (r["case"], r["turn_id"], i)
        for r in probe["rows"]
        for i, label in enumerate(r["labels"])
        if label["disposition"] == "must_keep"
    }
    if (
        len(entries) != len(expected)
        or {(e["case"], e["turn_id"], e["label_index"]) for e in entries} != expected
    ):
        raise ValueError("review must cover every target occurrence exactly once")
    decisions, event_by_ref, writes, seen_raw = {}, {}, [], set()
    for case, report in replay["cases"].items():
        if report["metadata"]["cleanup_errors"]:
            raise ValueError("replay has cleanup errors")
        for decision in report["decisions"]:
            ref = f"{case}/{decision['turn_id']}/candidate{decision['candidate_index']}"
            if ref in raw_rejections:
                rejected = raw_rejections[ref]
                if (
                    decoded(decision["candidate"]) != rejected["candidate"]
                    or decision["reason"] != rejected["reason"]
                    or decision["outcome"] != "rejected"
                    or decoded(decision["verification"]) is not None
                    or ref in seen_raw
                ):
                    raise ValueError("raw extraction rejection differs from saved evidence")
                seen_raw.add(ref)
                continue
            if ref not in candidates:
                continue  # Empty-batch/error sentinels have no candidate assertion.
            if ref in decisions:
                raise ValueError("duplicate candidate decision")
            if decoded(decision["candidate"]) != candidates[ref]:
                raise ValueError("replay survivor differs from reviewed candidate")
            decisions[ref] = decision
            if decision["event_id"]:
                event_by_ref[ref] = decision["event_id"]
        events = [w["event_id"] for w in report["gated"]["writes"]]
        decision_events = {d["event_id"] for d in report["decisions"] if d["event_id"]}
        if (
            len(events) != report["gated"]["total_events"]
            or len(set(events)) != len(events)
            or set(events) != decision_events
        ):
            raise ValueError("incomplete historical write evidence")
        for write in report["gated"]["writes"]:
            sources = decoded(write["source_span"])["turn_ids"]
            refs = [
                ref
                for ref, event in event_by_ref.items()
                if event == write["event_id"]
                and ref.startswith(case + "/")
                and decisions[ref]["turn_id"] in sources
            ]
            if not refs:
                raise ValueError("historical write lacks a reviewed source candidate")
            for ref in refs:
                candidate = candidates[ref]
                if (write["subject"], write["predicate"], payload(write)) != (
                    candidate["subject"],
                    candidate["predicate"],
                    candidate["object"],
                ):
                    raise ValueError("write differs from reviewed assertion")
            statuses = {support[ref] for ref in refs}
            status = (
                "unsupported"
                if "unsupported" in statuses
                else ("ambiguous" if "ambiguous" in statuses else "supported")
            )
            writes.append(
                {"case": case, "write": write, "candidate_refs": refs, "source_support": status}
            )
    if seen_raw != set(raw_rejections):
        raise ValueError("incomplete raw extraction rejection evidence")
    if set(decisions) != set(candidates):
        raise ValueError("replay is missing reviewed candidate decisions")
    stage_entries = []
    latest = {e["logical_target_id"]: e for e in entries}
    for entry in entries:
        source = by_turn[(entry["case"], entry["turn_id"])]
        label = entry["label"]
        logical_id = (
            entry["case"]
            + "/"
            + repr(assertion_key(label["subject"], label["predicate"], label["value"]))
        )
        if entry["logical_target_id"] != logical_id:
            raise ValueError("logical target identity differs from frozen scoring")
        if (
            entry["label"] != source["labels"][entry["label_index"]]
            or entry["source"] != source["source"]
        ):
            raise ValueError("target review changed original source or label")
        refs = entry["candidate_refs"]
        if any(
            ref not in candidates or not ref.startswith(f"{entry['case']}/{entry['turn_id']}/")
            for ref in refs
        ):
            raise ValueError("target review has an unbound candidate")
        if entry["candidates"] != [candidates[ref] for ref in refs]:
            raise ValueError("target review candidate snapshot differs")
        case = replay["cases"][entry["case"]]
        current = {w["event_id"] for w in case["gated"]["current_assertions"]}
        final = latest[entry["logical_target_id"]]
        label = final["label"]
        query = f"{label['subject']} {label['predicate']} {label['value']}"
        matching = [p for p in case["gated"]["retrieval_probes"] if p["query"] == query]
        if len(matching) != 1:
            raise ValueError("missing or duplicate fixed-query probe")
        retrieval = matching[0]
        visible = set(retrieval["visible_event_ids"])
        returned = {r["event_id"] for r in retrieval["results"] if r["source"] == "semantic"}
        available = {
            "candidate": set(candidates),
            "historical": set(event_by_ref),
            "current": {ref for ref, eid in event_by_ref.items() if eid in current},
            "visible": {ref for ref, eid in event_by_ref.items() if eid in visible},
            "retrieved": {ref for ref, eid in event_by_ref.items() if eid in returned},
        }
        credit = {
            stage: complete_credit(entry, values, support) for stage, values in available.items()
        }
        if entry["coverage"] == "ambiguous":
            loss = "annotation_or_assertion_ambiguity"
        elif not credit["candidate"]:
            loss = (
                "extraction_omission_or_validation" if not refs else "extraction_binding_or_meaning"
            )
        elif not credit["historical"]:
            loss = "verification_or_representation_rejection"
        elif not credit["current"]:
            loss = "overwrite_or_visibility"
        elif not credit["visible"]:
            loss = "session_or_temporal_restriction"
        elif not credit["retrieved"]:
            loss = "retrieval"
        else:
            loss = "complete_equivalent_retrieved"
        stage_entries.append(
            {
                **entry,
                "stage_evidence": {
                    "decisions": [decisions.get(ref) for ref in refs],
                    "retrieval_probe": retrieval,
                    "complete_credit": credit,
                    "earliest_loss": loss,
                },
            }
        )
    unique = {e["logical_target_id"] for e in entries}
    stage_counts = {
        stage: len(
            {
                e["logical_target_id"]
                for e in stage_entries
                if e["stage_evidence"]["complete_credit"][stage]
            }
        )
        for stage in ("candidate", "historical", "current", "visible", "retrieved")
    }
    last_rows = {e["logical_target_id"]: e for e in stage_entries}
    counts = Counter(w["source_support"] for w in writes)
    return {
        "review_scoring_version": "equivalence-v1",
        "scorer_sha256": _sha(Path(__file__)),
        "scope": replay["scope"],
        "review_status": "provisional agent judgments; no human gold",
        "input_sha256": {
            str(p): _sha(p) for p in (probe_path, review_path, support_path, replay_path)
        },
        "strict_scores_unchanged": {
            case: {
                arm: {
                    k: report[arm][k]
                    for k in ("precision", "recall", "must_keep_recall", "false_writes")
                }
                for arm in ("naive", "gated")
            }
            for case, report in replay["cases"].items()
        },
        "candidate_coverage": coverage_counts(entries, set(candidates), support),
        "unique_target_denominator": len(unique),
        "unique_complete_by_stage": stage_counts,
        "historical_write_review": {
            "total": len(writes),
            **{status: counts[status] for status in ("supported", "unsupported", "ambiguous")},
            "supported_precision": counts["supported"] / len(writes) if writes else 0.0,
        },
        "loss_counts_latest_occurrence": dict(
            Counter(e["stage_evidence"]["earliest_loss"] for e in last_rows.values())
        ),
        "writes": writes,
        "targets": stage_entries,
        "limitations": [
            "Selected 20-turn subset; omitted turns may introduce additional interference.",
            "Fixed label-derived queries only; later-session and post-TTL access unmeasured.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("probe", "review", "support", "replay", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("preserve prior evidence; choose a fresh output")
    result = score(args.probe, args.review, args.support, args.replay)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("writes", "targets")}, indent=2))


if __name__ == "__main__":
    main()
