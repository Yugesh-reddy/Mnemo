"""Execute each predeclared v9 phase exactly once (all local; no paid calls).

Phases:
  luna-off / luna-on   replay the frozen v8 Luna candidates, routing off / on
  qwen-on              replay the frozen v5 qwen candidates with routing on
  suite-off / suite-on the labeled identity cases, routing off / on
  eval                 make eval's scripted regression, routing off and on
  score                unchanged equivalence-v2 scorer and the frozen rule

Every phase verifies the frozen file hashes and gate configurations in the
manifest and refuses to overwrite earlier evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = HERE / "experiment-manifest.json"
DATA_MANIFEST = "mnemo/data/quality-v4/manifest.json"
V8 = "docs/quality-v8"
LUNA_PROBE = f"{V8}/extraction.json"
QWEN_PROBE = "docs/quality-v5/extraction-partial-dev.json"
CASES = "docs/quality-v9/identity-cases.json"


def sha(path: Path | str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def env_for(manifest: dict, routing: str) -> dict[str, str]:
    value = {"off": "off", "on": "contradiction"}[routing]
    return {**os.environ, **manifest["replay_environment"], "MNEMO_IDENTITY_ROUTING": value}


def preflight(manifest: dict) -> None:
    from mnemo.audit import gate_snapshot
    from mnemo.config import Settings

    changed = [p for p, digest in manifest["frozen_files_sha256"].items() if sha(p) != digest]
    if changed:
        raise SystemExit(f"frozen files changed since the manifest: {changed}")
    for routing, expected in manifest["configuration"].items():
        os.environ.update(env_for(manifest, routing))
        if gate_snapshot(Settings()) != expected:
            raise SystemExit(f"gate configuration for routing={routing} differs from manifest")


def fresh(*paths: Path) -> None:
    existing = [str(p.relative_to(ROOT)) for p in paths if p.exists()]
    if existing:
        raise SystemExit(f"phase already attempted; evidence exists: {existing}")


def run(name: str, command: list[str], env: dict[str, str], output: Path | None) -> int:
    log, journal_path = HERE / f"{name}.log", HERE / f"{name}-run.json"
    fresh(*(p for p in (output, log, journal_path) if p is not None))
    journal = {
        "phase": name,
        "command": command[1:],
        "identity_routing": env["MNEMO_IDENTITY_ROUTING"],
        "started_at_utc": now(),
        "manifest_sha256": sha(MANIFEST),
        "complete": False,
    }
    write_json(journal_path, journal)
    started = time.perf_counter()
    with log.open("w") as handle:
        result = subprocess.run(
            command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False
        )
    journal.update(
        complete=True,
        exit_code=result.returncode,
        elapsed_seconds=time.perf_counter() - started,
        finished_at_utc=now(),
    )
    write_json(journal_path, journal)
    print(json.dumps(journal, indent=2))
    return result.returncode


def replay(name: str, probe: str, routing: str, manifest: dict) -> int:
    output = HERE / f"{name}.json"
    command = [
        sys.executable,
        "scripts/replay_extraction_probe.py",
        "--probe",
        probe,
        "--manifest",
        DATA_MANIFEST,
        "--output",
        str(output.relative_to(ROOT)),
    ]
    return run(name, command, env_for(manifest, routing), output)


def suite(name: str, routing: str, manifest: dict) -> int:
    output = HERE / f"{name}.json"
    command = [
        sys.executable,
        "scripts/identity_suite.py",
        "--cases",
        CASES,
        "--output",
        str(output.relative_to(ROOT)),
    ]
    return run(name, command, env_for(manifest, routing), output)


def gated_line(path: Path) -> str:
    """make eval's GATED summary without its wall-clock latency."""
    line = next(x for x in path.read_text().splitlines() if x.strip().startswith("GATED"))
    return re.sub(r"\|\s*[\d.]+ ms", "", line).strip()


def restatement_only(score: dict) -> set[str]:
    """Targets retrieved only through a restatement-duplicate link (reported separately)."""
    credited: dict[str, list[bool]] = {}
    for entry in score["targets"]:
        evidence = entry["stage_evidence"]
        if not evidence["complete_credit"]["retrieved"]:
            continue
        linked = [d for d in evidence["decisions"] if d]
        credited.setdefault(entry["logical_target_id"], []).append(
            bool(linked) and all("identity=restatement" in d["reason"] for d in linked)
        )
    return {target for target, flags in credited.items() if all(flags)}


def score(manifest: dict) -> int:
    from mnemo.eval_equivalence import score as equivalence

    names = ("luna-off-score", "luna-on-score", "qwen-on-score", "decision")
    paths = {n: HERE / f"{n}.json" for n in names}
    fresh(*paths.values())
    scores = {
        "luna-off": equivalence(
            ROOT / LUNA_PROBE,
            ROOT / V8 / "mustkeep-review.json",
            ROOT / V8 / "source-review.json",
            HERE / "luna-off.json",
        ),
        "luna-on": equivalence(
            ROOT / LUNA_PROBE,
            ROOT / V8 / "mustkeep-review.json",
            ROOT / V8 / "source-review.json",
            HERE / "luna-on.json",
        ),
        "qwen-on": equivalence(
            ROOT / QWEN_PROBE,
            ROOT / "docs/quality-v6/mustkeep-review.json",
            ROOT / "docs/quality-v6/independent-review.json",
            HERE / "qwen-on.json",
        ),
    }
    for name, value in scores.items():
        write_json(paths[f"{name}-score"], value)
    retrieved = {n: s["unique_complete_by_stage"]["retrieved"] for n, s in scores.items()}
    linked_only = restatement_only(scores["luna-on"])
    suites = {n: json.loads((HERE / f"suite-{n}.json").read_text()) for n in ("off", "on")}
    rule = manifest["decision_rule"]
    checks = {
        "luna_on_retrieved_above_v8": retrieved["luna-on"] > rule["v8_retrieved"],
        "luna_on_not_below_luna_off": retrieved["luna-on"] >= retrieved["luna-off"],
        "luna_on_above_v8_without_restatement_links": retrieved["luna-on"] - len(linked_only)
        > rule["v8_retrieved"],
        "luna_on_zero_unsupported_writes": scores["luna-on"]["historical_write_review"][
            "unsupported"
        ]
        == 0,
        "qwen_on_not_below_control": retrieved["qwen-on"] >= rule["qwen_control_retrieved"],
        "suite_on_all_cases_pass": suites["on"]["passed"] == suites["on"]["total"],
        "make_eval_unchanged_with_routing": gated_line(HERE / "eval-on.log")
        == gated_line(HERE / "eval-off.log"),
    }
    summary = {
        name: {
            "unique_complete_by_stage": s["unique_complete_by_stage"],
            "denominator": s["unique_target_denominator"],
            "historical_write_review": {
                k: v for k, v in s["historical_write_review"].items() if k != "supported_precision"
            },
            "loss_counts_latest_occurrence": s["loss_counts_latest_occurrence"],
        }
        for name, s in scores.items()
    }
    decision = {
        "decided_at_utc": now(),
        "rule": rule,
        "checks": checks,
        "outcome": "success" if all(checks.values()) else "failure",
        "summary": summary,
        "restatement_only_targets": sorted(linked_only),
        "suite": {
            n: {k: s[k] for k in ("passed", "total", "false_replacements", "extra_current_values")}
            | {"failed_cases": [r["id"] for r in s["results"] if not r["passed"]]}
            for n, s in suites.items()
        },
        "make_eval_gated": {n: gated_line(HERE / f"eval-{n}.log") for n in ("off", "on")},
    }
    write_json(paths["decision"], decision)
    print(json.dumps({k: decision[k] for k in ("outcome", "checks", "suite")}, indent=2))
    return 0


def main() -> None:
    phases = ("luna-off", "luna-on", "qwen-on", "suite-off", "suite-on", "eval", "score")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=phases)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    preflight(manifest)
    if args.phase in ("luna-off", "luna-on"):
        code = replay(args.phase, LUNA_PROBE, args.phase.split("-")[1], manifest)
    elif args.phase == "qwen-on":
        code = replay(args.phase, QWEN_PROBE, "on", manifest)
    elif args.phase.startswith("suite-"):
        code = suite(args.phase, args.phase.split("-")[1], manifest)
    elif args.phase == "eval":
        command = [sys.executable, "-m", "mnemo.eval"]
        code = run("eval-off", command, env_for(manifest, "off"), None) or run(
            "eval-on", command, env_for(manifest, "on"), None
        )
    else:
        code = score(manifest)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
