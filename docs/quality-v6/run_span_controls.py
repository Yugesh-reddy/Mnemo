"""Run the eight predeclared parser/verifier controls with the experimental patch."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from mnemo.config import get_settings
from mnemo.eval_suite import policy_fingerprint
from mnemo.extraction import OllamaExtractor
from mnemo.quality import build_verifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("use a fresh output path")
    source = Path(__file__).with_name("span-targeted-controls.json")
    data = json.loads(source.read_text())
    settings = get_settings()
    policy = policy_fingerprint(settings)
    report = {
        "complete": False,
        "split": "dev",
        "scope": data["scope"],
        "policy_sha256": policy,
        "cases_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "verifier_sha256": hashlib.sha256(Path("mnemo/quality.py").read_bytes()).hexdigest(),
        "rows": [],
        "responses": [],
    }

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    verifier = build_verifier(settings)
    fallback = getattr(verifier, "fallback", verifier)
    request = getattr(fallback, "_request", None)
    if request is not None:

        def traced(prompt):
            response = request(prompt)
            report["responses"].append({"prompt": prompt, "response": response})
            save()
            return response

        fallback._request = traced
    save()
    try:
        for row in data["cases"]:
            candidate = OllamaExtractor._parse(
                json.dumps({"facts": [row["candidate"]]}), row["source"]
            )[0]
            verdict = verifier.verify(candidate, row["source"])
            report["rows"].append(
                {
                    **row,
                    "parsed_candidate": candidate.model_dump(mode="json"),
                    "verdict": verdict.model_dump(mode="json"),
                }
            )
            save()
    finally:
        if hasattr(verifier, "close"):
            verifier.close()
    if policy_fingerprint(settings) != policy:
        raise RuntimeError("policy changed during the run")
    report["counts"] = dict(
        Counter(
            (
                ("tp" if r["expected_accepted"] else "fp")
                if r["verdict"]["accepted"]
                else ("fn" if r["expected_accepted"] else "tn")
            )
            for r in report["rows"]
        )
    )
    report["complete"] = True
    save()
    print(report["counts"])


if __name__ == "__main__":
    main()
