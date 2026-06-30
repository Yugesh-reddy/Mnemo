"""Execute each predeclared v12 phase exactly once.

Routing is on in every phase; only the judge for its two checks differs.
  suite-local / suite-azure   the v9 identity cases, local judge vs Azure gpt-5.6-luna
  luna-local / luna-azure     the frozen v8 Luna candidates, local judge vs Azure judge
  score                       unchanged equivalence-v2 scorer and the frozen rule
Only the *-azure phases make paid calls, each capped at 150 requests / 200k tokens.

Every phase verifies the frozen file hashes and gate configurations in the
manifest and refuses to overwrite earlier evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
CASES = "docs/quality-v9/identity-cases.json"  # unchanged, reused


def sha(path: Path | str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def env_for(manifest: dict, routing: str = "on") -> dict[str, str]:
    return {**os.environ, **manifest["replay_environment"]}


def preflight(manifest: dict) -> None:
    from mnemo.audit import gate_snapshot
    from mnemo.config import Settings

    changed = [p for p, digest in manifest["frozen_files_sha256"].items() if sha(p) != digest]
    if changed:
        raise SystemExit(f"frozen files changed since the manifest: {changed}")
    os.environ.update(env_for(manifest))
    if gate_snapshot(Settings()) != manifest["configuration"]:
        raise SystemExit("gate configuration differs from the manifest")


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
        "identity_judge": command[command.index("--identity-judge") + 1],
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


def judge_of(name: str) -> str:
    return "azure" if name.endswith("-azure") else "verifier"


def replay(name: str, probe: str, manifest: dict) -> int:
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
        "--identity-judge",
        judge_of(name),
    ]
    return run(name, command, env_for(manifest), output)


def suite(name: str, manifest: dict) -> int:
    output = HERE / f"{name}.json"
    command = [
        sys.executable,
        "scripts/identity_suite.py",
        "--cases",
        CASES,
        "--output",
        str(output.relative_to(ROOT)),
        "--identity-judge",
        judge_of(name),
    ]
    return run(name, command, env_for(manifest), output)


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


def target_durability(score: dict, report: dict) -> list[str]:
    """Historically complete targets with no durable credited write."""
    tiers = {
        w["event_id"]: w["tier"] for c in report["cases"].values() for w in c["gated"]["writes"]
    }
    durable: dict[str, bool] = {}
    for entry in score["targets"]:
        evidence = entry["stage_evidence"]
        if not evidence["complete_credit"]["historical"]:
            continue
        events = [d["event_id"] for d in evidence["decisions"] if d and d["event_id"]]
        hit = any(tiers.get(e) == "durable" for e in events)
        durable[entry["logical_target_id"]] = durable.get(entry["logical_target_id"], False) or hit
    return sorted(t for t, ok in durable.items() if not ok)


def suite_summary(report: dict) -> dict:
    clean = [r for r in report["results"] if not r["gate_rejected_turns"]]
    return {
        "passed": report["passed"],
        "total": report["total"],
        "gate_clean": len(clean),
        "gate_clean_passed": sum(r["passed"] for r in clean),
        "gate_rejected_cases": [r["id"] for r in report["results"] if r["gate_rejected_turns"]],
        "failed_cases": [r["id"] for r in report["results"] if not r["passed"]],
        "false_replacements": report["false_replacements"],
        "extra_current_values": report["extra_current_values"],
        "judge": {
            k: v for k, v in (report.get("identity_judge") or {}).items() if k != "exchanges"
        },
    }


def score(manifest: dict) -> int:
    from mnemo.eval_equivalence import score as equivalence

    names = ("luna-local-score", "luna-azure-score", "decision")
    paths = {n: HERE / f"{n}.json" for n in names}
    fresh(*paths.values())
    reports, scores = {}, {}
    for arm in ("luna-local", "luna-azure"):
        reports[arm] = json.loads((HERE / f"{arm}.json").read_text())
        scores[arm] = equivalence(
            ROOT / LUNA_PROBE,
            ROOT / V8 / "mustkeep-review.json",
            ROOT / V8 / "source-review.json",
            HERE / f"{arm}.json",
        )
        write_json(paths[f"{arm}-score"], scores[arm])
    retrieved = {n: s["unique_complete_by_stage"]["retrieved"] for n, s in scores.items()}
    linked_only = restatement_only(scores["luna-azure"])
    suites = {
        n: suite_summary(json.loads((HERE / f"suite-{n}.json").read_text()))
        for n in ("local", "azure")
    }
    judge_stopped = [
        n
        for n, r in (("luna-azure", reports["luna-azure"].get("identity_judge") or {}),)
        if r.get("stopped")
    ] + [n for n in ("azure",) if suites[n]["judge"].get("stopped")]
    strict_false = sum(c["gated"]["false_writes"] for c in reports["luna-azure"]["cases"].values())
    rule = manifest["decision_rule"]
    azure_suite = suites["azure"]
    checks = {
        "suite_azure_gate_clean_cases_all_pass": azure_suite["gate_clean_passed"]
        == azure_suite["gate_clean"],
        "suite_azure_at_least_12_gate_clean": azure_suite["gate_clean"] >= rule["min_gate_clean"],
        "luna_azure_retrieved_above_v11": retrieved["luna-azure"] > rule["v11_retrieved"],
        "luna_azure_not_below_local_judge": retrieved["luna-azure"] >= retrieved["luna-local"],
        "luna_azure_above_v11_without_restatement_links": retrieved["luna-azure"] - len(linked_only)
        > rule["v11_retrieved"],
        "luna_azure_zero_unsupported_writes": scores["luna-azure"]["historical_write_review"][
            "unsupported"
        ]
        == 0,
        "luna_azure_zero_forbidden_writes": strict_false == 0,
        "luna_azure_written_targets_durable": not target_durability(
            scores["luna-azure"], reports["luna-azure"]
        ),
        "judge_finished_within_budget": not judge_stopped,
    }
    luna_judge = reports["luna-azure"].get("identity_judge") or {}
    decision = {
        "decided_at_utc": now(),
        "rule": rule,
        "checks": checks,
        "outcome": "success" if all(checks.values()) else "failure",
        "retrieved": retrieved,
        "unique_complete_by_stage": {n: s["unique_complete_by_stage"] for n, s in scores.items()},
        "write_review": {n: s["historical_write_review"] for n, s in scores.items()},
        "restatement_only_targets": sorted(linked_only),
        "suite": suites,
        "judge_usage": {
            "suite-azure": suites["azure"]["judge"].get("usage"),
            "luna-azure": luna_judge.get("usage"),
        },
    }
    write_json(paths["decision"], decision)
    print(
        json.dumps({k: decision[k] for k in ("outcome", "checks", "retrieved", "suite")}, indent=2)
    )
    return 0


def main() -> None:
    phases = ("suite-local", "suite-azure", "luna-local", "luna-azure", "score")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=phases)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    preflight(manifest)
    if args.phase.startswith("suite-"):
        code = suite(args.phase, manifest)
    elif args.phase.startswith("luna-"):
        code = replay(args.phase, LUNA_PROBE, manifest)
    else:
        code = score(manifest)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
