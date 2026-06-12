"""Run source-reviewed development regressions against the current verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from mnemo.config import get_settings
from mnemo.eval_suite import policy_fingerprint
from mnemo.models import ExtractedFact
from mnemo.quality import build_verifier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("preserve existing development evidence; use a new output path")
    data = json.loads(args.cases.read_text())
    if data["split"] != "dev":
        raise ValueError("this diagnostic command only accepts development cases")
    settings = get_settings()
    fingerprint = policy_fingerprint(settings)
    report = {
        "split": "dev",
        "complete": False,
        "policy_sha256": fingerprint,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "scope": data["scope"],
        "rows": [],
    }
    verifier = build_verifier(settings)
    try:
        for case in data["cases"]:
            verdict = verifier.verify(ExtractedFact(**case["candidate"]), case["source"])
            report["rows"].append({**case, "after": verdict.model_dump(mode="json")})
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if hasattr(verifier, "close"):
            verifier.close()
    if policy_fingerprint(settings) != fingerprint:
        raise RuntimeError("policy changed during the run; partial results retained")
    report["counts"] = {
        name: dict(
            Counter(
                (
                    ("tp" if r["expected_accepted"] else "fp")
                    if r[field]["accepted"]
                    else ("fn" if r["expected_accepted"] else "tn")
                )
                for r in report["rows"]
            )
        )
        for name, field in (("before", "baseline_verdict"), ("after", "after"))
    }
    report["complete"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["counts"], indent=2))


if __name__ == "__main__":
    main()
