"""Rejected raw members stay evidence, without becoming candidate coverage."""

import json

import pytest

from mnemo.eval_audit import analyze_report
from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn
from scripts.probe_extraction_dev import coverage


@pytest.mark.parametrize("raw", [{"subject": "user"}, {"raw": 17}, {"raw": {"subject": "user"}}])
def test_raw_rejections_do_not_crash_miss_analysis_or_receive_coverage(raw):
    label = AtomicLabel("location", "Oslo", "must_keep")
    dataset = EvalDataset(
        name="rejected-members",
        version="1",
        split="dev",
        turns=(EvalTurn("t", "user", "I live in Oslo.", "s", labels=(label,)),),
    )
    decision = {
        "turn_id": "t",
        "candidate": json.dumps(raw),
        "outcome": "rejected",
        "verification": None,
        "reason": "malformed candidate",
    }
    report = {
        "metadata": {"fingerprints": {"dataset": dataset.fingerprint()}},
        "gated": {"writes": [], "current_assertions": []},
        "decisions": [decision],
    }
    audit = analyze_report(report, dataset)
    assert audit["strict_missing_must_keep"] == 1
    assert audit["missing_by_cause"] == {"extraction_validation_rejection": 1}
    assert audit["misses"][0]["decisions"][0]["candidate"] == raw
    assert coverage([raw], [vars(label)]) == {
        "strict_target_matches": 0,
        "targets": 1,
        "strict_must_keep_matches": 0,
        "must_keep_targets": 1,
    }


def test_replay_retains_raw_rejections_without_validating_them_as_facts():
    from mnemo.eval_audit import replay_candidates

    good = {"subject": "user", "predicate": "location", "object": "Oslo"}
    # A complete-looking assertion with a fabricated quote must still stay rejected.
    bad = {"raw": {**good, "evidence": "fabricated"}}
    decisions = [
        {
            "candidate": json.dumps(good),
            "outcome": "rejected",
            "verification": {"accepted": False},
            "reason": "neutral",
        },
        {
            "candidate": json.dumps(bad),
            "outcome": "rejected",
            "verification": None,
            "reason": "candidate 1 evidence is absent from source",
        },
    ]
    batch = replay_candidates(decisions)
    assert [f.object for f in batch] == ["Oslo"]
    assert batch.rejections == [{"candidate": bad, "reason": decisions[1]["reason"]}]
    decisions[1]["outcome"] = "accepted"
    with pytest.raises(ValueError):
        replay_candidates(decisions)


def test_replay_detects_conflicting_rejection_evidence_for_same_source():
    from mnemo.eval import _MaterializedExtractor
    from mnemo.extraction import ExtractionBatch

    turns = [EvalTurn("a", "user", "Same source", "s"), EvalTurn("b", "user", "Same source", "s")]
    batches = [
        ExtractionBatch([], [{"candidate": {"raw": n}, "reason": "malformed"}]) for n in (1, 2)
    ]
    with pytest.raises(ValueError, match="conflicting"):
        _MaterializedExtractor(turns, batches)
