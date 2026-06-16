"""Reviewed scores must not reward partial, unsupported, or duplicate assertions."""

import json
from pathlib import Path

import pytest

from mnemo.eval_equivalence import complete_credit, coverage_counts


def test_complete_credit_requires_supported_complete_bound_candidates():
    entry = {"coverage": "complete", "candidate_refs": ["a", "b"]}
    support = {"a": "supported", "b": "supported"}
    assert complete_credit(entry, {"a", "b"}, support)
    assert not complete_credit(entry, {"a"}, support)
    assert not complete_credit({**entry, "coverage": "partial"}, {"a", "b"}, support)
    assert not complete_credit(entry, {"a", "b"}, {**support, "b": "unsupported"})
    assert not complete_credit(entry, {"a", "b"}, {"a": "supported"})
    assert not complete_credit({**entry, "candidate_refs": []}, set(), support)


def test_unique_coverage_never_multiplies_repeated_mentions():
    entries = [
        {"logical_target_id": "cake", "coverage": "complete", "candidate_refs": ["a"]},
        {"logical_target_id": "cake", "coverage": "complete", "candidate_refs": ["b"]},
        {"logical_target_id": "date", "coverage": "partial", "candidate_refs": ["c"]},
    ]
    result = coverage_counts(entries, {"a", "b", "c"}, dict.fromkeys("abc", "supported"))
    assert result == {
        "complete_occurrences": 2,
        "occurrences": 3,
        "complete_unique_targets": 1,
        "unique_targets": 2,
    }


def test_complete_saved_baseline_scores_all_targets_and_all_historical_writes(tmp_path):
    from mnemo.eval_equivalence import score

    root = Path(__file__).resolve().parents[1]
    paths = [
        root / name
        for name in (
            "docs/quality-v5/extraction-partial-dev.json",
            "docs/quality-v6/mustkeep-review.json",
            "docs/quality-v6/independent-review.json",
            "docs/quality-v6/baseline-complete.json",
        )
    ]
    result = score(*paths)
    assert len(result["targets"]) == 24
    assert result["unique_target_denominator"] == 23
    assert result["historical_write_review"] == {
        "total": 51,
        "supported": 50,
        "unsupported": 0,
        "ambiguous": 1,
        "supported_precision": 50 / 51,
    }
    assert result["unique_complete_by_stage"]["retrieved"] == 16
    replay = json.loads(paths[-1].read_text())
    replay["cases"]["88432d0a"]["decisions"] = [
        d for d in replay["cases"]["88432d0a"]["decisions"] if d["outcome"] != "rejected"
    ]
    changed = tmp_path / "missing-rejections.json"
    changed.write_text(json.dumps(replay))
    with pytest.raises(ValueError, match="missing reviewed candidate decisions"):
        score(*paths[:-1], changed)


@pytest.mark.parametrize("mutation", ["source", "label", "duplicate", "missing"])
def test_probe_replay_refuses_changed_or_incomplete_inputs(tmp_path, mutation):
    from scripts.replay_extraction_probe import load_probe

    root = Path(__file__).resolve().parents[1]
    path = root / "docs/quality-v5/extraction-partial-dev.json"
    manifest = root / "mnemo/data/quality-v4/manifest.json"
    cases = load_probe(path, manifest)
    assert sum(len(batch) for _, _, batches in cases for batch in batches) == 60
    assert sum(len(batch.rejections) for _, _, batches in cases for batch in batches) == 4
    data = json.loads(path.read_text())
    if mutation == "source":
        data["rows"][0]["source"] = "another source"
    elif mutation == "label":
        data["rows"][0]["labels"][0]["value"] = "another value"
    elif mutation == "duplicate":
        data["rows"].append(data["rows"][0])
    else:
        data["rows"].pop()
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_probe(altered, manifest)
