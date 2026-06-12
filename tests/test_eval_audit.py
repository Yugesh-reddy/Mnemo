"""Frozen evidence must remain attributable and cannot silently accept new labels."""

import copy
import json
from pathlib import Path

import pytest

from mnemo.eval_audit import analyze_report
from mnemo.eval_data import load_benchmark_dataset
from mnemo.eval_suite import load_cases

ROOT = Path(__file__).resolve().parents[1]


def test_review_accounts_for_every_saved_unmatched_write_and_must_keep_loss():
    report = json.loads((ROOT / "docs/evaluation/real-200.json").read_text())
    review = json.loads((ROOT / "docs/quality-v2/adjudication.json").read_text())
    result = analyze_report(report, load_benchmark_dataset("all"), review)
    assert result["reviewed_count"] == result["unmatched_count"] == 38
    assert sum(result["review_categories"].values()) == 38
    assert result["strict_missing_must_keep"] == 8
    emergency = next(m for m in result["misses"] if m["target"]["predicate"] == "emergency_contact")
    assert emergency["cause"] == "overwritten_after_correct_write"
    assert emergency["later_same_identity_writes"][-1]["object_text"] == "Alex Chen"
    for mutation in ("missing", "source"):
        bad = copy.deepcopy(review)
        if mutation == "missing":
            bad["items"].pop()
        else:
            bad["items"][0]["source"] = "Invented source"
        with pytest.raises(ValueError):
            analyze_report(report, load_benchmark_dataset("all"), bad)


def test_external_split_has_distinct_sessions_and_verified_source_hashes(tmp_path):
    manifest = ROOT / "mnemo/data/quality-v2/manifest.json"
    cases = load_cases(manifest, "dev")
    assert len(cases) == 3
    assert sum(len(ds.turns) for _, ds in cases) == 140
    data = json.loads(manifest.read_text())
    data["records"][1]["session_ids"] = data["records"][0]["session_ids"]
    altered = tmp_path / "manifest.json"
    altered.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="leakage"):
        load_cases(altered, "dev")
